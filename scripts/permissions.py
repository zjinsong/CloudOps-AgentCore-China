"""Explicit business read actions. Deployment IAM is a separate identity."""
API_READ=[
 'lambda:ListFunctions','lambda:GetFunctionConfiguration','lambda:ListAliases','lambda:ListVersionsByFunction','lambda:ListTags','lambda:GetPolicy','lambda:GetFunctionConcurrency',
 'cloudwatch:ListMetrics','cloudwatch:GetMetricData','cloudwatch:GetMetricStatistics','cloudwatch:DescribeAlarms','cloudwatch:DescribeAlarmsForMetric','cloudwatch:DescribeAlarmHistory',
 'logs:DescribeLogGroups','logs:DescribeLogStreams','logs:GetLogEvents','logs:FilterLogEvents',
 'ec2:DescribeInstances','ec2:DescribeInstanceStatus','ec2:DescribeVolumes','ec2:DescribeVpcs','ec2:DescribeSubnets','ec2:DescribeSecurityGroups',
 'elasticloadbalancing:DescribeLoadBalancers','elasticloadbalancing:DescribeTargetGroups','elasticloadbalancing:DescribeTargetHealth',
 'rds:DescribeDBInstances','rds:DescribeDBClusters','rds:DescribeEvents']
AGENTCORE_READ=['bedrock-agentcore:'+name for name in (
 'ListAgentRuntimes','GetAgentRuntime','ListAgentRuntimeVersions','ListAgentRuntimeEndpoints','GetAgentRuntimeEndpoint',
 'ListGateways','GetGateway','ListGatewayTargets','GetGatewayTarget','GetResourcePolicy','ListWorkloadIdentities','GetWorkloadIdentity',
 'ListOauth2CredentialProviders','GetOauth2CredentialProvider','ListApiKeyCredentialProviders','GetApiKeyCredentialProvider','GetTokenVault')]
READ_ACTIONS={'cost-explorer':['ce:GetCostAndUsage','ce:GetCostForecast'],
 'cloudwatch':[a for a in API_READ if a.startswith('cloudwatch:')],
 'pricing':['pricing:DescribeServices','pricing:GetAttributeValues','pricing:GetProducts','pricing:ListPriceLists','pricing:GetPriceListFileUrl'],
 'cloudtrail-mcp':['cloudtrail:LookupEvents'],'aws-api':API_READ,'agentcore-mcp':AGENTCORE_READ,'terraform-mcp':[],'health-events':[]}

def allow(actions,resources):
    return {'Effect':'Allow','Action':actions,'Resource':resources}

def document(statements):
    return {'Version':'2012-10-17','Statement':statements}
