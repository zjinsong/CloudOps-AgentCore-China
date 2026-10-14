"""Validate deployment wire parameters against installed SDK models; no AWS calls."""
import json,unittest
from pathlib import Path
import boto3
from botocore.validate import validate_parameters
from scripts.deploy import TARGETS,zip_function

class DeploymentContracts(unittest.TestCase):
    def setUp(self):
        self.account='0'*12
        self.role='arn:aws-cn:iam::'+self.account+':role/cloudops-ref-runtime'
    def validate(self,service,operation,args):
        c=boto3.client(service,region_name='cn-northwest-1',aws_access_key_id='validation-only',aws_secret_access_key='validation-only')
        validate_parameters(args,c.meta.service_model.operation_model(operation).input_shape)
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
