"""Validate deployment wire parameters against installed SDK models; no AWS calls."""
import json,unittest
from pathlib import Path
import boto3
from unittest.mock import Mock, patch
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters
from scripts.deploy import TARGETS,zip_function,foundation,create_gateway_target

class DeploymentContracts(unittest.TestCase):
    def setUp(self):
        self.account='0'*12
        self.role='arn:aws-cn:iam::'+self.account+':role/cloudops-ref-runtime'
    def validate(self,service,operation,args):
        c=boto3.client(service,region_name='cn-northwest-1',aws_access_key_id='validation-only',aws_secret_access_key='validation-only')
        validate_parameters(args,c.meta.service_model.operation_model(operation).input_shape)
    def test_foundation_permissions_cover_retained_web_features(self):
        cfg={'project':'cloudops-ref','region':'cn-northwest-1','model_id':'test',
             'model_base_url':'https://model.example.com/v1',
             'model_secret_arn':'arn:aws-cn:secretsmanager:cn-northwest-1:'+self.account+':secret:test',
             'image_uri':self.account+'.dkr.ecr.cn-northwest-1.amazonaws.com.cn/cloudops-ref-runtime:test'}
        tags=[{'Key':'Project','Value':cfg['project']},{'Key':'Repository','Value':'CloudOps-AgentCore-China'}]
        clients={name:Mock() for name in ('sts','dynamodb','s3','iam')}
        clients['sts'].get_caller_identity.return_value={'Account':self.account}
        clients['dynamodb'].describe_table.return_value={'Table':{'TableArn':'test-table'}}
        clients['dynamodb'].list_tags_of_resource.return_value={'Tags':tags}
        clients['dynamodb'].describe_time_to_live.return_value={'TimeToLiveDescription':{'TimeToLiveStatus':'ENABLED'}}
        clients['s3'].get_bucket_tagging.return_value={'TagSet':tags}
        clients['iam'].list_instance_profile_tags.return_value={'Tags':tags}
        web_arn='arn:aws-cn:iam::'+self.account+':role/cloudops-ref-web'
        clients['iam'].get_instance_profile.return_value={'InstanceProfile':{'Roles':[{'Arn':web_arn}]}}
        def role_arn(iam,config,suffix,principal,statements):
            return 'arn:aws-cn:iam::'+self.account+':role/cloudops-ref-'+suffix
        with patch('scripts.deploy.boto3.client',side_effect=lambda name,**kw:clients[name]), \
             patch('scripts.deploy.role',side_effect=role_arn) as roles,patch('scripts.deploy.state'):
            foundation(cfg)
        web=next(call.args[4] for call in roles.call_args_list if call.args[2]=='web')
        reads=[statement for statement in web if 'bedrock-agentcore:GetAgentRuntime' in statement['Action']]
        self.assertEqual(len(reads),1)
        self.assertEqual(reads[0]['Resource'],
                         'arn:aws-cn:bedrock-agentcore:cn-northwest-1:'+self.account+':runtime/cloudops_ref_*')
        runtime=next(call.args[4] for call in roles.call_args_list if call.args[2]=='runtime')
        self.assertTrue(any('transcribe:StartStreamTranscriptionWebSocket' in s['Action'] for s in runtime))
        cors=clients['s3'].put_bucket_cors.call_args.kwargs
        self.validate('s3','PutBucketCors',cors)
        self.assertEqual(cors['Bucket'],'cloudops-ref-work-'+self.account)
        rule=cors['CORSConfiguration']['CORSRules'][0]
        self.assertIn('POST',rule['AllowedMethods'])
        self.assertEqual(set(rule['AllowedOrigins']),{'http://localhost:3100','http://127.0.0.1:3100'})
        clients['s3'].put_bucket_cors.assert_called_once()
    def test_gateway_target_retries_role_propagation(self):
        cp=Mock()
        transient=ClientError({'Error':{'Code':'ValidationException','Message':'The execution role lacks permission to invoke the function'}},'CreateGatewayTarget')
        cp.create_gateway_target.side_effect=[transient,{'targetId':'ready'}]
        with patch('scripts.deploy.time.sleep') as sleep:
            self.assertEqual(create_gateway_target(cp,{'name':'test'}),{'targetId':'ready'})
        self.assertEqual(cp.create_gateway_target.call_count,2)
        sleep.assert_called_once_with(5)
    def test_gateway_target_retry_stops_after_limit(self):
        cp=Mock()
        cp.create_gateway_target.side_effect=ClientError({'Error':{'Code':'ValidationException','Message':'execution role lacks permission'}},'CreateGatewayTarget')
        with patch('scripts.deploy.time.sleep') as sleep:
            with self.assertRaises(ClientError):create_gateway_target(cp,{'name':'test'})
        self.assertEqual(cp.create_gateway_target.call_count,8)
        self.assertEqual(sleep.call_count,7)
    def test_gateway_target_does_not_retry_other_failures(self):
        cp=Mock()
        cp.create_gateway_target.side_effect=ClientError({'Error':{'Code':'AccessDeniedException','Message':'Caller is not authorized'}},'CreateGatewayTarget')
        with patch('scripts.deploy.time.sleep') as sleep:
            with self.assertRaises(ClientError):create_gateway_target(cp,{'name':'test'})
        cp.create_gateway_target.assert_called_once()
        sleep.assert_not_called()
    def test_gateway_request(self):
        self.validate('bedrock-agentcore-control','CreateGateway',{'name':'cloudops-ref-gateway','roleArn':self.role,'authorizerType':'AWS_IAM','protocolType':'MCP',
                      'tags':{'Project':'cloudops-ref','Repository':'CloudOps-AgentCore-China'}})
    def test_all_lambda_target_schemas(self):
        for name,folder in TARGETS.items():
            self.validate('bedrock-agentcore-control','CreateGatewayTarget',{'gatewayIdentifier':'cloudops-ref-abcdefghij','name':name,
                'targetConfiguration':{'mcp':{'lambda':{'lambdaArn':'arn:aws-cn:lambda:cn-northwest-1:'+self.account+':function:cloudops-ref-'+name,
                 'toolSchema':{'inlinePayload':json.loads(Path('tools',folder,'tools.json').read_text())}}}},
                 'credentialProviderConfigurations':[{'credentialProviderType':'GATEWAY_IAM_ROLE'}]})
    def test_runtime_protocols(self):
        for name,protocol,port in [('supervisor','HTTP','8080'),('monitoring','A2A','9000')]:
            self.validate('bedrock-agentcore-control','CreateAgentRuntime',{'agentRuntimeName':'cloudops_ref_'+name,'roleArn':self.role,
              'agentRuntimeArtifact':{'containerConfiguration':{'containerUri':self.account+'.dkr.ecr.cn-northwest-1.amazonaws.com.cn/cloudops-ref-runtime:test'}},
              'networkConfiguration':{'networkMode':'PUBLIC'},'protocolConfiguration':{'serverProtocol':protocol},
              'environmentVariables':{'PORT':port},'lifecycleConfiguration':{'idleRuntimeSessionTimeout':900,'maxLifetime':28800}})
    def test_all_lambda_packages_and_parameters(self):
        for name in TARGETS:
            self.validate('lambda','CreateFunction',{'FunctionName':'cloudops-ref-'+name,'Role':self.role,'Runtime':'python3.12',
             'Handler':'handler.handler','Timeout':70,'MemorySize':1536,'Layers':[],
             'Environment':{'Variables':{'MCP_TARGET':name}},'Architectures':['x86_64'],'Code':{'ZipFile':zip_function(name)}})

if __name__=='__main__':unittest.main()
