"""Bound monitoring CLI requests to read APIs in AWS China, without local files."""
import shlex

OPERATIONS = {
    'lambda': {'list-functions', 'get-function-configuration', 'list-aliases', 'list-versions-by-function',
               'list-tags', 'get-policy', 'get-function-concurrency'},
    'cloudwatch': {'list-metrics', 'get-metric-data', 'get-metric-statistics', 'describe-alarms',
                   'describe-alarms-for-metric', 'describe-alarm-history'},
    'logs': {'describe-log-groups', 'describe-log-streams', 'get-log-events', 'filter-log-events'},
    'ec2': {'describe-instances', 'describe-instance-status', 'describe-volumes', 'describe-vpcs',
            'describe-subnets', 'describe-security-groups'},
    'elasticloadbalancing': {'describe-load-balancers', 'describe-target-groups', 'describe-target-health'},
    'rds': {'describe-db-instances', 'describe-db-clusters', 'describe-events'},
}
REGIONS = {'cn-northwest-1', 'cn-north-1'}
BLOCKED = {'--profile', '--endpoint-url', '--no-sign-request', '--cli-input-json', '--cli-input-yaml',
           '--debug', '--ca-bundle', '--no-verify-ssl', '--cli-auto-prompt', '--generate-cli-skeleton',
           '--cli-read-timeout', '--cli-connect-timeout'}


def command(value):
    tokens = shlex.split(value)
    # Official call_aws accepts service-first commands; keep the same contract.
    if tokens and tokens[0] in OPERATIONS:
        tokens.insert(0, 'aws')
    if len(tokens) < 3 or tokens[0] != 'aws' or tokens[2] not in OPERATIONS.get(tokens[1], set()):
        raise PermissionError('只允许已授权的监控只读 AWS API')
    if any(t.split('=', 1)[0] in BLOCKED or t in {';', '|', '&&', '>', '<', '&'}
           or 'file://' in t or 'fileb://' in t or '$(' in t or '`' in t for t in tokens[3:]):
        raise PermissionError('不允许更换凭证、端点、文件访问或命令拼接')
    # Configuration responses may contain application secrets. Strip output
    # projections before execution so redaction always sees complete fields.
    if tokens[1:3] in (['lambda','get-function-configuration'],['lambda','list-functions'],['lambda','list-versions-by-function']):
        safe=tokens[:3];i=3
        while i<len(tokens):
            if tokens[i] in {'--query','--output'}:
                if i+1==len(tokens):raise ValueError('缺少 CLI 参数值')
                i+=2;continue
            if tokens[i].split('=',1)[0] in {'--query','--output'}:
                i+=1;continue
            safe.append(tokens[i]);i+=1
        tokens=safe
    region = None
    for i, token in enumerate(tokens[3:], 3):
        if token == '--region':
            if i + 1 == len(tokens): raise ValueError('缺少中国区 region')
            region = tokens[i+1]
            if region not in REGIONS: raise PermissionError('仅支持 AWS 中国区')
        elif token.startswith('--region='):
            region = token.split('=',1)[1]
            if region not in REGIONS: raise PermissionError('仅支持 AWS 中国区')
    if region is None: tokens += ['--region', 'cn-northwest-1']
    return shlex.join(tokens)


def arguments(name, args):
    if name != 'call_aws': raise PermissionError('仅开放 call_aws 只读执行工具')
    if set(args) - {'cli_command','max_results'} or 'cli_command' not in args: raise ValueError('参数无效')
    cli = command(args['cli_command'])
    tokens = shlex.split(cli)
    limit = min(100,max(1,int(args.get('max_results',50))))
    # ListMetrics has no MaxResults API parameter. Models often put the tool's
    # result limit into the CLI; move it back to the official MCP argument.
    if tokens[1:3] == ['cloudwatch','list-metrics']:
        safe=tokens[:3];i=3
        while i<len(tokens):
            if tokens[i]=='--max-results':
                if i+1==len(tokens):raise ValueError('缺少 max-results 参数值')
                limit=min(limit,max(1,int(tokens[i+1])));i+=2;continue
            if tokens[i].startswith('--max-results='):
                limit=min(limit,max(1,int(tokens[i].split('=',1)[1])));i+=1;continue
            safe.append(tokens[i]);i+=1
        cli=shlex.join(safe)
    return {'cli_command':cli,'max_results':limit}
