"""Official AgentCore MCP inspection tools; no lifecycle writes or token retrieval."""
READ_TOOLS = frozenset({
    'list_agent_runtimes', 'get_agent_runtime', 'list_agent_runtime_versions',
    'list_agent_runtime_endpoints', 'get_agent_runtime_endpoint',
    'gateway_list', 'gateway_get', 'gateway_target_list', 'gateway_target_get', 'gateway_resource_policy_get',
    'identity_list_workload_identities', 'identity_get_workload_identity',
    'identity_list_oauth2_providers', 'identity_get_oauth2_provider',
    'identity_list_api_key_providers', 'identity_get_api_key_provider',
    'identity_get_token_vault', 'identity_get_resource_policy',
    'get_runtime_guide', 'get_gateway_guide', 'get_identity_guide',
    'search_agentcore_docs', 'fetch_agentcore_doc',
})


def arguments(name, args):
    if name not in READ_TOOLS: raise PermissionError('仅开放 AgentCore 查询和文档工具')
    value = dict(args)
    for key, item in value.items():
        if key in ('region', 'region_name', 'aws_region') and item != 'cn-northwest-1':
            raise PermissionError('AgentCore 专家限定宁夏中国区')
        if isinstance(item, str) and item.startswith('arn:') and not item.startswith('arn:aws-cn:'):
            raise PermissionError('仅接受中国区资源 ARN')
    if 'max_results' in value: value['max_results'] = min(50, max(1, int(value['max_results'])))
    if name == 'search_agentcore_docs': value['k'] = min(3, max(1, int(value.get('k', 3))))
    return value


SENSITIVE = {'environmentvariables', 'environment', 'variables', 'clientsecret', 'apikey',
             'secretaccesskey', 'accesskeyid', 'sessiontoken', 'refreshtoken', 'accesstoken',
             'authorization', 'password', 'token', 'code', 'codeconfiguration'}


def redact(value):
    import json
    if isinstance(value, dict):
        return {k: '[REDACTED]' if k.lower().replace('_', '').replace('-', '') in SENSITIVE else redact(v)
                for k,v in value.items()}
    if isinstance(value, list): return [redact(v) for v in value]
    if isinstance(value, str):
        try: parsed = json.loads(value)
        except (ValueError, TypeError): return value
        if isinstance(parsed, (dict, list)): return json.dumps(redact(parsed), ensure_ascii=False)
    return value
