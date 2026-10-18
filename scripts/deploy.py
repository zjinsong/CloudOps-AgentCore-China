"""Fresh-account China reference deployment. --apply is required for mutations.

Config/state is private. No access keys, existing demo ARNs or credentials are copied.
Stages are resumable; resources must carry this repository's ownership tags.
"""
import argparse,io,json,os,re,sys,time,zipfile
from pathlib import Path
import boto3
from botocore.validate import validate_parameters
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.permissions import READ_ACTIONS,API_READ,allow,document

OWNER='CloudOps-AgentCore-China'
TARGETS={'cost-explorer':'cost_explorer','cloudwatch':'cloudwatch','pricing':'pricing','cloudtrail-mcp':'cloudtrail',
         'terraform-mcp':'terraform','health-events':'health_events','aws-api':'aws_api','agentcore-mcp':'agentcore'}

def load(path='.state/deployment.json'):
    cfg=json.loads(Path(path).read_text(encoding='utf-8'))
    if not re.fullmatch(r'[a-z][a-z0-9-]{2,23}',cfg['project']):raise ValueError('project: 3–24 lowercase letters/digits/hyphens')
    if cfg['region']!='cn-northwest-1':raise ValueError('This deployment guide supports Ningxia; other regions require reviewed adaptations')
    for key in ('model_id','model_base_url','model_secret_arn','image_uri'):
        if not cfg.get(key) or '<' in cfg[key]:raise ValueError('Replace placeholder: '+key)
    if not cfg['model_base_url'].startswith('https://'):raise ValueError('Use an approved HTTPS model endpoint')
    return cfg

def private(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    path.write_text(json.dumps(value,indent=2,ensure_ascii=False,default=str),encoding='utf-8');path.chmod(0o600)

def state(cfg):private('.state/deployment.json',cfg)

def tags(cfg):return {'Project':cfg['project'],'Repository':OWNER}

def owned(values,cfg):
    if values.get('Project')!=cfg['project'] or values.get('Repository')!=OWNER:
        raise PermissionError('Existing resource is not owned by this reference deployment')

def wait_runtime(cp,rid):
    for _ in range(180):
        d=cp.get_agent_runtime(agentRuntimeId=rid)
        if d['status']=='READY':return d
        if d['status'].endswith('FAILED'):raise RuntimeError('Runtime failed; inspect private service failureReason')
        time.sleep(5)
    raise TimeoutError('Runtime provisioning exceeded 15 minutes')

def raw_fields(cp):
    # Old SDK response models silently omit platformVersion; retain raw metadata.
    def complete(http_response,parsed,**kwargs):
        raw=json.loads(http_response.content)
        if isinstance(parsed,dict) and 'platformVersion' in raw:parsed['platformVersion']=raw['platformVersion']
    cp.meta.events.register('after-call.bedrock-agentcore-control.GetAgentRuntime',complete)

def role(iam,cfg,suffix,principal,statements):
    name=cfg['project']+'-'+suffix
    trust=document([{'Effect':'Allow','Principal':{'AWS' if principal.startswith('arn:') else 'Service':principal},'Action':'sts:AssumeRole'}])
    if principal=='bedrock-agentcore.amazonaws.com':
        resource='runtime/'+cfg['project'].replace('-','_')+'_*' if suffix=='runtime' else 'gateway/*'
        trust['Statement'][0]['Condition']={'StringEquals':{'aws:SourceAccount':cfg['account']},
                                          'ArnLike':{'aws:SourceArn':f"arn:aws-cn:bedrock-agentcore:{cfg['region']}:{cfg['account']}:{resource}"}}
    try:
        value=iam.create_role(RoleName=name,AssumeRolePolicyDocument=json.dumps(trust),
                              Tags=[{'Key':k,'Value':v} for k,v in tags(cfg).items()])['Role']
    except iam.exceptions.EntityAlreadyExistsException:
        value=iam.get_role(RoleName=name)['Role'];owned({t['Key']:t['Value'] for t in value.get('Tags',[])},cfg)
        iam.update_assume_role_policy(RoleName=name,PolicyDocument=json.dumps(trust))
    iam.put_role_policy(RoleName=name,PolicyName='reference-scoped',PolicyDocument=json.dumps(document(statements)))
    return value['Arn']

def foundation(cfg):
    region=cfg['region'];account=boto3.client('sts',region_name=region).get_caller_identity()['Account']
    cfg['account']=account
    if not cfg['model_secret_arn'].startswith(f'arn:aws-cn:secretsmanager:{region}:{account}:secret:'):
        raise ValueError('Model secret must be in this China account/region')
    if not cfg['image_uri'].startswith(f'{account}.dkr.ecr.{region}.amazonaws.com.cn/'):
        raise ValueError('Image must be in this China account/region')
    ddb=boto3.client('dynamodb',region_name=region);s3=boto3.client('s3',region_name=region)
    table=cfg['project']+'-tasks';table_arn=f'arn:aws-cn:dynamodb:{region}:{account}:table/{table}'
    try:
        d=ddb.describe_table(TableName=table)['Table']
        owned({t['Key']:t['Value'] for t in ddb.list_tags_of_resource(ResourceArn=d['TableArn'])['Tags']},cfg)
    except ddb.exceptions.ResourceNotFoundException:
        ddb.create_table(TableName=table,BillingMode='PAY_PER_REQUEST',KeySchema=[{'AttributeName':'id','KeyType':'HASH'}],
            AttributeDefinitions=[{'AttributeName':n,'AttributeType':'S'} for n in ('id','kind','scope','order','active','timeline_scope','timeline_order')],
            GlobalSecondaryIndexes=[{'IndexName':n+'-index','KeySchema':[{'AttributeName':n,'KeyType':'HASH'},{'AttributeName':'order','KeyType':'RANGE'}],
                                    'Projection':{'ProjectionType':'ALL'}} for n in ('kind','scope','active')]+[
                {'IndexName':'task-timeline-index','KeySchema':[{'AttributeName':'timeline_scope','KeyType':'HASH'},{'AttributeName':'timeline_order','KeyType':'RANGE'}],
                 'Projection':{'ProjectionType':'INCLUDE','NonKeyAttributes':['summary']}}],
            Tags=[{'Key':k,'Value':v} for k,v in tags(cfg).items()])
        ddb.get_waiter('table_exists').wait(TableName=table)
    ttl=ddb.describe_time_to_live(TableName=table)['TimeToLiveDescription']['TimeToLiveStatus']
    if ttl=='DISABLED':ddb.update_time_to_live(TableName=table,TimeToLiveSpecification={'Enabled':True,'AttributeName':'expires_at'})
    for attempt in range(30):
        try:
            ddb.update_continuous_backups(TableName=table,PointInTimeRecoverySpecification={'PointInTimeRecoveryEnabled':True})
            break
        except ddb.exceptions.ContinuousBackupsUnavailableException:
            if attempt == 29:raise
            time.sleep(2)
    buckets={key:f"{cfg['project']}-{key}-{account}" for key in ('work','chat')}
    for key,bucket in buckets.items():
        try:
            s3.head_bucket(Bucket=bucket)
            owned({t['Key']:t['Value'] for t in s3.get_bucket_tagging(Bucket=bucket)['TagSet']},cfg)
        except s3.exceptions.ClientError as e:
            if e.response['Error']['Code'] not in ('404','NoSuchBucket'):raise
            s3.create_bucket(Bucket=bucket,CreateBucketConfiguration={'LocationConstraint':region})
            s3.put_bucket_tagging(Bucket=bucket,Tagging={'TagSet':[{'Key':k,'Value':v} for k,v in tags(cfg).items()]})
        s3.put_public_access_block(Bucket=bucket,PublicAccessBlockConfiguration={n:True for n in ('BlockPublicAcls','IgnorePublicAcls','BlockPublicPolicy','RestrictPublicBuckets')})
        s3.put_bucket_encryption(Bucket=bucket,ServerSideEncryptionConfiguration={'Rules':[{'ApplyServerSideEncryptionByDefault':{'SSEAlgorithm':'AES256'}}]})
        s3.put_bucket_policy(Bucket=bucket,Policy=json.dumps(document([{'Effect':'Deny','Principal':'*','Action':'s3:*',
                            'Resource':[f'arn:aws-cn:s3:::{bucket}',f'arn:aws-cn:s3:::{bucket}/*'],
                            'Condition':{'Bool':{'aws:SecureTransport':'false'}}}])))
        if key=='work':s3.put_bucket_versioning(Bucket=bucket,VersioningConfiguration={'Status':'Enabled'})
        prefix='users/' if key=='chat' else 'cloudops/users/'
        rules=[{'ID':'history-30-days','Status':'Enabled','Filter':{'Prefix':prefix},
                'Expiration':{'Days':30},'NoncurrentVersionExpiration':{'NoncurrentDays':30}}]
        if key=='work':
            rules.append({'ID':'session-checkpoints-30-days','Status':'Enabled','Filter':{'Prefix':'cloudops/sessions/'},
                          'Expiration':{'Days':30},'NoncurrentVersionExpiration':{'NoncurrentDays':30}})
            rules.append({'ID':'layer-builds-14-days','Status':'Enabled','Filter':{'Prefix':'build/layers/'},
                          'Expiration':{'Days':14},'NoncurrentVersionExpiration':{'NoncurrentDays':14}})
        s3.put_bucket_lifecycle_configuration(Bucket=bucket,LifecycleConfiguration={'Rules':rules})
    cfg['common']={'AWS_REGION':region,'AWS_DEFAULT_REGION':region,'CLOUDOPS_ACCOUNT':account,'CLOUDOPS_TABLE':table,
                   'CLOUDOPS_BUCKET':buckets['work'],'CLOUDOPS_CHAT_BUCKET':buckets['chat'],'CLOUDOPS_ENABLE_DEFAULT_ROUTINES':'false',
                   'CLOUDOPS_ENABLE_APPLY':'false','CLOUDOPS_ENABLE_TERRAFORM_PLAN':'false','MODEL_ID':cfg['model_id'],'MODEL_BASE_URL':cfg['model_base_url'],'MODEL_SECRET_ARN':cfg['model_secret_arn']}
    db_policy=allow(['dynamodb:GetItem','dynamodb:PutItem','dynamodb:UpdateItem','dynamodb:DeleteItem','dynamodb:Query','dynamodb:Scan','dynamodb:TransactWriteItems'],[table_arn,table_arn+'/index/*'])
    store_policy=[allow(['s3:ListBucket'],[f'arn:aws-cn:s3:::{b}' for b in buckets.values()]),
                  allow(['s3:GetObject','s3:GetObjectVersion','s3:PutObject','s3:DeleteObject'],[f'arn:aws-cn:s3:::{b}/*' for b in buckets.values()])]
    logs=allow(['logs:CreateLogGroup','logs:CreateLogStream','logs:PutLogEvents'],f'arn:aws-cn:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/{cfg["project"].replace("-","_")}_*')
    image=cfg['image_uri'].split('/',1)[1].split(':',1)[0]
    ecr=[allow(['ecr:GetAuthorizationToken'],'*'),allow(['ecr:BatchGetImage','ecr:GetDownloadUrlForLayer','ecr:BatchCheckLayerAvailability'],f'arn:aws-cn:ecr:{region}:{account}:repository/{image}')]
    runtime_prefix=f'arn:aws-cn:bedrock-agentcore:{region}:{account}:runtime/{cfg["project"].replace("-","_")}_*'
    common=[db_policy,*store_policy,allow(['secretsmanager:GetSecretValue'],cfg['model_secret_arn'])]
    iam=boto3.client('iam',region_name=region)
    cfg['runtime_role']=role(iam,cfg,'runtime','bedrock-agentcore.amazonaws.com',[*common,logs,*ecr,
        allow(['bedrock-agentcore:InvokeGateway'],f'arn:aws-cn:bedrock-agentcore:{region}:{account}:gateway/*'),
        allow(['xray:PutTraceSegments','xray:PutTelemetryRecords'],'*')])
    cfg['ec2_role']=role(iam,cfg,'web','ec2.amazonaws.com',[*common,
        allow(['bedrock-agentcore:GetAgentRuntime','bedrock-agentcore:InvokeAgentRuntime','bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream','bedrock-agentcore:StopRuntimeSession'],runtime_prefix),
        allow(['bedrock-agentcore:InvokeGateway'],f'arn:aws-cn:bedrock-agentcore:{region}:{account}:gateway/*'),
        allow(['cloudwatch:ListMetrics','cloudwatch:GetMetricStatistics','cloudwatch:GetMetricData'],'*'),
        allow(['logs:FilterLogEvents'],f'arn:aws-cn:logs:{region}:{account}:log-group:aws/spans:*'),
        allow(['xray:PutTraceSegments','xray:PutTelemetryRecords'],'*')])
    profile=cfg['project']+'-web'
    try:iam.create_instance_profile(InstanceProfileName=profile,Tags=[{'Key':k,'Value':v} for k,v in tags(cfg).items()])
    except iam.exceptions.EntityAlreadyExistsException:pass
    owned({t['Key']:t['Value'] for t in iam.list_instance_profile_tags(InstanceProfileName=profile)['Tags']},cfg)
    existing=iam.get_instance_profile(InstanceProfileName=profile)['InstanceProfile']['Roles']
    if existing and existing[0]['Arn']!=cfg['ec2_role']:raise PermissionError('Instance profile belongs to another role')
    if not existing:iam.add_role_to_instance_profile(InstanceProfileName=profile,RoleName=profile)
    cfg['instance_profile']=profile;state(cfg)
    print('Foundation ready; EC2 instance-profile name:',profile)

def zip_function(target):
    body=io.BytesIO()
    with zipfile.ZipFile(body,'w',zipfile.ZIP_DEFLATED) as z:
        if target in ('cost-explorer','cloudwatch'):
            z.write('tools/'+TARGETS[target]+'/handler.py','handler.py')
        elif target=='health-events':
            z.write('tools/health_events/lambda_entry.py','handler.py')
            z.write('tools/health_events/handler.py','tools/health_events/handler.py')
            z.write('control/store.py','control/store.py')
        else:
            z.write('tools/mcp_lambda/handler.py','handler.py')
            for folder in ('pricing','aws_api','agentcore'):z.write(f'tools/{folder}/policy.py',f'tools/{folder}/policy.py')
            z.write('tools/cloudtrail/pagination.py','tools/cloudtrail/pagination.py')
    return body.getvalue()

def gateway(cfg):
    region=cfg['region'];account=cfg['account'];iam=boto3.client('iam',region_name=region)
    lam=boto3.client('lambda',region_name=region);s3=boto3.client('s3',region_name=region);cp=boto3.client('bedrock-agentcore-control',region_name=region)
    layers={}
    for name in ('core','pricing','aws-api','agentcore-mcp'):
        path=Path('.state/mcp-lambda-layers')/(name+'.zip')
        if not path.exists():raise FileNotFoundError('Build MCP layers first: '+str(path))
        body=path.read_bytes();key='build/layers/'+name+'.zip'
        s3.put_object(Bucket=cfg['common']['CLOUDOPS_BUCKET'],Key=key,Body=body,ServerSideEncryption='AES256')
        layers[name]=lam.publish_layer_version(LayerName=cfg['project']+'-'+name,Content={'S3Bucket':cfg['common']['CLOUDOPS_BUCKET'],'S3Key':key},
                                             CompatibleRuntimes=['python3.12'],CompatibleArchitectures=['x86_64'])['LayerVersionArn']
    functions={}
    for target,folder in TARGETS.items():
        fn=cfg['project']+'-'+target
        log=f'arn:aws-cn:logs:{region}:{account}:log-group:/aws/lambda/{fn}'
        grants=[allow(['logs:CreateLogGroup','logs:CreateLogStream','logs:PutLogEvents'],[log,log+':*'])]
        if READ_ACTIONS[target]:
            read=allow(READ_ACTIONS[target],'*');read['Condition']={'StringEquals':{'aws:RequestedRegion':['cn-northwest-1','cn-north-1']}}
            grants.append(read)
        if target=='health-events':
            table=f'arn:aws-cn:dynamodb:{region}:{account}:table/{cfg["common"]["CLOUDOPS_TABLE"]}'
            grants.append(allow(['dynamodb:PutItem','dynamodb:GetItem','dynamodb:Query'],[table,table+'/index/kind-index']))
        arn=role(iam,cfg,'lambda-'+target,'lambda.amazonaws.com',grants)
        env={'MCP_TARGET':target,'CLOUDOPS_ACCOUNT':account,'FASTMCP_LOG_LEVEL':'ERROR','AWS_MAX_ATTEMPTS':'2'}
        if target=='health-events':env['CLOUDOPS_TABLE']=cfg['common']['CLOUDOPS_TABLE']
        attached=[] if target in ('cost-explorer','cloudwatch','health-events') else [layers['core']]
        if target=='pricing':attached.append(layers['pricing'])
        if target in ('aws-api','agentcore-mcp'):attached.append(layers[target])
        code=zip_function(target)
        try:
            current=lam.get_function(FunctionName=fn);owned(current.get('Tags',{}),cfg)
            lam.update_function_code(FunctionName=fn,ZipFile=code);lam.get_waiter('function_updated_v2').wait(FunctionName=fn)
            lam.update_function_configuration(FunctionName=fn,Role=arn,Runtime='python3.12',Handler='handler.handler',Timeout=70,MemorySize=1536,
                                              Layers=attached,Environment={'Variables':env})
        except lam.exceptions.ResourceNotFoundException:
            time.sleep(10)
            lam.create_function(FunctionName=fn,Role=arn,Runtime='python3.12',Handler='handler.handler',Timeout=70,MemorySize=1536,
                                Layers=attached,Environment={'Variables':env},Architectures=['x86_64'],Code={'ZipFile':code},Tags=tags(cfg))
        lam.get_waiter('function_active_v2').wait(FunctionName=fn);lam.get_waiter('function_updated_v2').wait(FunctionName=fn)
        functions[target]=lam.get_function_configuration(FunctionName=fn)['FunctionArn']
    gateway_role=role(iam,cfg,'gateway','bedrock-agentcore.amazonaws.com',[allow(['lambda:InvokeFunction'],list(functions.values()))])
    if not cfg.get('gateway_id'):
        time.sleep(10)
        result=cp.create_gateway(name=cfg['project']+'-gateway',roleArn=gateway_role,authorizerType='AWS_IAM',protocolType='MCP',tags=tags(cfg))
        cfg['gateway_id']=result['gatewayId'];state(cfg)
    for _ in range(120):
        g=cp.get_gateway(gatewayIdentifier=cfg['gateway_id'])
        if g['status']=='READY':break
        if g['status'].endswith('FAILED'):raise RuntimeError('Gateway failed; inspect private configuration')
        time.sleep(5)
    else:raise TimeoutError('Gateway not ready')
    if g['name']!=cfg['project']+'-gateway' or g['authorizerType']!='AWS_IAM':raise PermissionError('Unexpected Gateway configuration')
    owned(cp.list_tags_for_resource(resourceArn=g['gatewayArn'])['tags'],cfg)
    trust=document([{'Effect':'Allow','Principal':{'Service':'bedrock-agentcore.amazonaws.com'},'Action':'sts:AssumeRole',
                    'Condition':{'StringEquals':{'aws:SourceAccount':account},'ArnEquals':{'aws:SourceArn':g['gatewayArn']}}}])
    iam.update_assume_role_policy(RoleName=cfg['project']+'-gateway',PolicyDocument=json.dumps(trust))
    # Narrow invocation grants now that the Gateway ARN is known.
    for name in (cfg['project']+'-runtime',cfg['project']+'-web'):
        doc=iam.get_role_policy(RoleName=name,PolicyName='reference-scoped')['PolicyDocument']
        for statement in doc['Statement']:
            if statement.get('Action')==['bedrock-agentcore:InvokeGateway']:statement['Resource']=g['gatewayArn']
        iam.put_role_policy(RoleName=name,PolicyName='reference-scoped',PolicyDocument=json.dumps(doc))
    current={}
    for page in cp.get_paginator('list_gateway_targets').paginate(gatewayIdentifier=g['gatewayId']):current.update({t['name']:t for t in page['items']})
    for target,folder in TARGETS.items():
        args={'gatewayIdentifier':g['gatewayId'],'name':target,'targetConfiguration':{'mcp':{'lambda':{'lambdaArn':functions[target],
             'toolSchema':{'inlinePayload':json.loads(Path('tools',folder,'tools.json').read_text())}}}},
             'credentialProviderConfigurations':[{'credentialProviderType':'GATEWAY_IAM_ROLE'}]}
        if target in current:result=cp.update_gateway_target(targetId=current[target]['targetId'],**args)
        else:result=cp.create_gateway_target(**args)
        for _ in range(120):
            value=cp.get_gateway_target(gatewayIdentifier=g['gatewayId'],targetId=result['targetId'])
            if value['status']=='READY':break
            if value['status'].endswith('FAILED'):raise RuntimeError('Target failed: '+target)
            time.sleep(3)
        else:raise TimeoutError('Target not ready: '+target)
    events=boto3.client('events',region_name=region)
    rule=cfg['project']+'-health'
    try:
        existing_rule=events.describe_rule(Name=rule)
        owned({t['Key']:t['Value'] for t in events.list_tags_for_resource(ResourceARN=existing_rule['Arn'])['Tags']},cfg)
    except events.exceptions.ResourceNotFoundException:pass
    rule_arn=events.put_rule(Name=rule,EventPattern=json.dumps({'source':['aws.health'],'account':[account]}),State='ENABLED',
                             Tags=[{'Key':k,'Value':v} for k,v in tags(cfg).items()])['RuleArn']
    try:lam.add_permission(FunctionName=cfg['project']+'-health-events',StatementId='HealthEvents',Action='lambda:InvokeFunction',Principal='events.amazonaws.com',SourceArn=rule_arn,SourceAccount=account)
    except lam.exceptions.ResourceConflictException:pass
    result=events.put_targets(Rule=rule,Targets=[{'Id':'archive','Arn':functions['health-events']}]);assert result['FailedEntryCount']==0
    cfg.update(gateway_url=g['gatewayUrl'],gateway_arn=g['gatewayArn'],functions=functions,layers=layers)
    cfg['common']['GATEWAY_URL']=g['gatewayUrl'];state(cfg)
    print('Gateway and 8 read-only targets ready')

def runtimes(cfg,update=False):
    cp=boto3.client('bedrock-agentcore-control',region_name=cfg['region']);raw_fields(cp)
    catalog=json.loads(Path('hierarchy.json').read_text(encoding='utf-8'))['agents']
    web=json.loads(Path('.secrets/web.json').read_text());cfg.setdefault('runtimes',{})
    for name in catalog:
        env={**cfg['common'],'AGENT_NAME':name,'CLOUDOPS_TRACING_ENABLED':'true',
             'ENTRY_FILE':'agents/supervisor/ws_main.py' if name=='supervisor' else 'agents/leaf/a2a_main.py'}
        if name=='supervisor':env['CLOUDOPS_BRIDGE_SECRET']=web['CLOUDOPS_BRIDGE_SECRET']
        else:env['PORT']='9000'
        # DEFAULT follows updates. No repeated update for already-created resources.
        args={'agentRuntimeArtifact':{'containerConfiguration':{'containerUri':cfg['image_uri']}},'roleArn':cfg['runtime_role'],
              'networkConfiguration':{'networkMode':'PUBLIC'},'protocolConfiguration':{'serverProtocol':'HTTP' if name=='supervisor' else 'A2A'},
              'environmentVariables':env,'lifecycleConfiguration':{'idleRuntimeSessionTimeout':900,'maxLifetime':28800}}
        rid=cfg['runtimes'].get(name,{}).get('id')
        if rid:
            existing=cp.get_agent_runtime(agentRuntimeId=rid)
            if existing['agentRuntimeName']!=cfg['project'].replace('-','_')+'_'+name:raise PermissionError('Unexpected Runtime name')
            owned(cp.list_tags_for_resource(resourceArn=existing['agentRuntimeArn'])['tags'],cfg)
        operation='UpdateAgentRuntime' if rid else 'CreateAgentRuntime'
        if 'platformVersion' in cp.meta.service_model.operation_model(operation).input_shape.members:args['platformVersion']='V2'
        if not rid:
            result=cp.create_agent_runtime(agentRuntimeName=cfg['project'].replace('-','_')+'_'+name,tags=tags(cfg),**args)
            rid=result['agentRuntimeId'];cfg['runtimes'][name]={'id':rid};state(cfg)
        elif update:
            wait_runtime(cp,rid);cp.update_agent_runtime(agentRuntimeId=rid,**args)
        result=wait_runtime(cp,rid)
        if result.get('platformVersion')!='V2':raise RuntimeError('Expected platform V2; inspect raw response and SDK before continuing')
        cfg['runtimes'][name]={'id':rid,'arn':result['agentRuntimeArn'],'version':result['agentRuntimeVersion'],'platform':'V2'};state(cfg)
        print(name,'READY','platform V2','configuration version',result['agentRuntimeVersion'])
    os.environ.update({k:str(v) for k,v in cfg['common'].items()})
    from control import domain,registry,capabilities
    domain.seed()
    for name,meta in cfg['runtimes'].items():registry.register(name,catalog[name],meta['arn'],meta['version'])
    from framework.gateway import get_gateway_mcp_client,load_gateway_tools
    with get_gateway_mcp_client(cfg['gateway_url']) as client:
        names=[t.tool_name for t in load_gateway_tools(client,list(registry.TOOLS))]
    capabilities.record_inventory(registry.store,names)
    missing=[a['id'] for a in capabilities.catalog(registry.active(),registry.store) if not a['available'] and a['id']!='prepare_terraform_plan']
    if missing:raise RuntimeError('Capabilities missing tools: '+', '.join(missing))
    env={**cfg['common'],**web,'CLOUDOPS_WS_RUNTIME':cfg['runtimes']['supervisor']['arn'],'CLOUDOPS_TRACING_ENABLED':'true','COOKIE_SECURE':'false'}
    path=Path('.secrets/control.env');path.write_text('\n'.join(k+'='+str(v) for k,v in env.items())+'\n');path.chmod(0o600)
    print('6 Runtime registrations and Gateway inventory verified; private control.env written')

def main():
    p=argparse.ArgumentParser();p.add_argument('stage',choices=['foundation','gateway','runtimes']);p.add_argument('--apply',action='store_true');p.add_argument('--update',action='store_true')
    a=p.parse_args();cfg=load()
    if not a.apply:
        print('Plan only:',a.stage,'project:',cfg['project'],'region:',cfg['region']);return
    if a.stage=='foundation':foundation(cfg)
    elif a.stage=='gateway':gateway(cfg)
    else:runtimes(cfg,a.update)

if __name__=='__main__':main()
