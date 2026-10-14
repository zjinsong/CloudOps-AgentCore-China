"""One-time task summary index + SDK span destination. Run with deployment credentials.

python scripts/provision_task_observability.py --table <table> --roles <role> [<role> ...]
No credentials are printed. Region defaults to Ningxia; outputs only resource status.
"""
import argparse
import json
import time
import boto3


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--table',required=True)
    p.add_argument('--roles',nargs='+',required=True)
    p.add_argument('--users',nargs='*',default=[])
    p.add_argument('--region',default='cn-northwest-1')
    a=p.parse_args()
    group='aws/spans'
    account=boto3.client('sts',region_name=a.region).get_caller_identity()['Account']
    logs=boto3.client('logs',region_name=a.region)
    # aws/spans is service-created; names beginning AWS/ are reserved.
    policy={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'xray.amazonaws.com'},
        'Action':'logs:PutLogEvents','Resource':[f'arn:aws-cn:logs:{a.region}:{account}:log-group:{name}:*' for name in
            (group,'/aws/application-signals/data')],
        'Condition':{'StringEquals':{'aws:SourceAccount':account},'ArnLike':{'aws:SourceArn':f'arn:aws-cn:xray:{a.region}:{account}:*'}}}]}
    logs.put_resource_policy(policyName='CloudOpsTransactionSearch',policyDocument=json.dumps(policy))
    xray=boto3.client('xray',region_name=a.region)
    # Account/region setting required by the official AgentCore observability setup.
    destination=xray.get_trace_segment_destination()
    if destination.get('Destination')!='CloudWatchLogs' and destination.get('Status')!='PENDING':
        xray.update_trace_segment_destination(Destination='CloudWatchLogs')
    elif destination.get('Destination')!='CloudWatchLogs':
        raise RuntimeError('Another trace destination update is pending; retry after it completes')
    try:
        logs.put_retention_policy(logGroupName=group,retentionInDays=30)
    except logs.exceptions.ResourceNotFoundException:
        print('aws/spans is service-created after first telemetry; rerun this script after the SDK smoke test to set 30-day retention.')
    iam=boto3.client('iam')
    role_policy={'Version':'2012-10-17','Statement':[
        {'Effect':'Allow','Action':['xray:PutTraceSegments','xray:PutTelemetryRecords'],'Resource':'*'},
        {'Effect':'Allow','Action':['logs:FilterLogEvents'],'Resource':f'arn:aws-cn:logs:{a.region}:{account}:log-group:{group}:*'},
        {'Effect':'Allow','Action':['dynamodb:Query'],'Resource':f'arn:aws-cn:dynamodb:{a.region}:{account}:table/{a.table}/index/task-timeline-index'}]}
    for role in set(a.roles):iam.put_role_policy(RoleName=role,PolicyName='CloudOpsTaskObservability',PolicyDocument=json.dumps(role_policy))
    for user in set(a.users):iam.put_user_policy(UserName=user,PolicyName='CloudOpsTaskObservability',PolicyDocument=json.dumps(role_policy))
    db=boto3.client('dynamodb',region_name=a.region)
    desc=db.describe_table(TableName=a.table)['Table']
    if not any(i['IndexName']=='task-timeline-index' for i in desc.get('GlobalSecondaryIndexes',[])):
        db.update_table(TableName=a.table,AttributeDefinitions=[{'AttributeName':n,'AttributeType':'S'} for n in ('timeline_scope','timeline_order')],
            GlobalSecondaryIndexUpdates=[{'Create':{'IndexName':'task-timeline-index','KeySchema':[
                {'AttributeName':'timeline_scope','KeyType':'HASH'},{'AttributeName':'timeline_order','KeyType':'RANGE'}],
                'Projection':{'ProjectionType':'INCLUDE','NonKeyAttributes':['summary']}}}])
    for _ in range(120):
        index=next(i for i in db.describe_table(TableName=a.table)['Table']['GlobalSecondaryIndexes'] if i['IndexName']=='task-timeline-index')
        if index['IndexStatus']=='ACTIVE':break
        time.sleep(5)
    else:raise RuntimeError('Task index is not ACTIVE yet')
    print('Task index ACTIVE; ADOT destination configured.')


if __name__=='__main__':main()
