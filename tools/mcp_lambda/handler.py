"""Gateway Lambda adapter for persistent official read-only stdio MCP servers.

The expert Runtime only talks to Gateway. MCP dependencies, processes and AWS
read permissions belong to independent Lambda backends. One asyncio loop owns
each warm Lambda's MCP session; invocations never nest asyncio.run contexts.
"""
import asyncio
import concurrent.futures
import json
import os
import sys
import threading
import tempfile
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from tools.pricing.policy import READ_TOOLS, pricing_arguments
from tools.aws_api.policy import arguments as aws_api_arguments
from tools.agentcore.policy import READ_TOOLS as AGENTCORE_TOOLS, arguments as agentcore_arguments, redact

ALLOWED = {'pricing': READ_TOOLS, 'cloudtrail-mcp': frozenset({'lookup_events'}),
    'aws-api': frozenset({'call_aws'}), 'agentcore-mcp': AGENTCORE_TOOLS,
    'terraform-mcp': frozenset({'search_providers','get_provider_details','resolve_provider_doc_id',
        'search_modules','get_module_details','get_provider_capabilities','search_provider_resources'})}


def parameters(target, workdir=None):
    env = dict(os.environ)
    env.pop('AWS_PROFILE', None)
    env.pop('AWS_API_MCP_PROFILE_NAME', None)
    env.update(AWS_REGION='cn-northwest-1', AWS_DEFAULT_REGION='cn-northwest-1', ENABLE_TF_OPERATIONS='false',
        LOG_LEVEL='error', FASTMCP_LOG_LEVEL='ERROR', AWS_MAX_ATTEMPTS='2', AWS_RETRY_MODE='standard')
    if target == 'pricing':
        env['PYTHONPATH'] = '/opt/pricing-mcp'
        command, args = sys.executable, ['-m','awslabs.aws_pricing_mcp_server.server']
    elif target == 'cloudtrail-mcp':
        env['PYTHONPATH'] = '/opt/python'
        command, args = sys.executable, ['-m','awslabs.cloudtrail_mcp_server.server']
    elif target == 'terraform-mcp':
        command, args = '/opt/bin/terraform-mcp-server', ['stdio']
    elif target == 'aws-api':
        env.update(PYTHONPATH='/opt/aws-api', READ_OPERATIONS_ONLY='true',
                   AWS_API_MCP_ALLOW_UNRESTRICTED_LOCAL_FILE_ACCESS='no-access', AWS_API_MCP_TELEMETRY='false',
                   AWS_API_MCP_WORKING_DIR=workdir or '/tmp/aws-api-mcp', HOME=workdir or '/tmp/aws-api-mcp',
                   AWS_API_MCP_TRANSPORT='stdio', AWS_API_MCP_SUPPRESS_DEPRECATION_WARNING='true',
                   EXPERIMENTAL_AGENT_SCRIPTS='false', AWS_PAGER='')
        command, args = sys.executable, ['-m','awslabs.aws_api_mcp_server.server']
    elif target == 'agentcore-mcp':
        env.update(PYTHONPATH='/opt/agentcore-mcp', AGENTCORE_ENABLE_TOOLS='runtime,gateway,identity',
                   HOME='/tmp/agentcore-mcp')
        command, args = sys.executable, ['-m','awslabs.amazon_bedrock_agentcore_mcp_server.server']
    else:
        raise ValueError('Unsupported MCP backend')
    return StdioServerParameters(command=command,args=args,env=env)


class Bridge:
    def __init__(self, target, workdir=None):
        self.target, self.loop, self.client, self.error = target, None, None, None
        self.workdir = workdir
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.run,daemon=True)
        self.thread.start()

    def run(self):
        async def serve():
            self.loop = asyncio.get_running_loop()
            self.stop = asyncio.Event()
            try:
                async with stdio_client(parameters(self.target, self.workdir)) as streams:
                    async with ClientSession(*streams) as client:
                        await asyncio.wait_for(client.initialize(),20)
                        self.client = client
                        self.ready.set()
                        await self.stop.wait()
            except BaseException as error:
                self.error = error
            finally:
                self.client = None
                self.ready.set()
        asyncio.run(serve())

    def close(self):
        if self.loop and self.loop.is_running(): self.loop.call_soon_threadsafe(self.stop.set)

    def invoke(self, name, arguments, timeout=35):
        begun=time.monotonic()
        if not self.ready.wait(min(22,timeout)) or self.client is None:
            self.close()
            raise RuntimeError('Official MCP server initialization failed')
        future = asyncio.run_coroutine_threadsafe(self.client.call_tool(name,arguments=arguments),self.loop)
        try:
            return future.result(timeout=max(.1,timeout-(time.monotonic()-begun)))
        except concurrent.futures.TimeoutError:
            future.cancel()
            self.close()
            raise TimeoutError('Read-only MCP query deadline exceeded') from None


_bridge = None


def bounded_arguments(target,name,arguments):
    if name not in ALLOWED.get(target,frozenset()):raise PermissionError('Unsupported read-only MCP tool')
    args=dict(arguments)
    if target=='aws-api':args=aws_api_arguments(name,args)
    if target=='agentcore-mcp':args=agentcore_arguments(name,args)
    if target=='pricing':args=pricing_arguments(name,args)
    if target=='cloudtrail-mcp':
        args['region']=args.get('region') if args.get('region') in ('cn-northwest-1','cn-north-1') else 'cn-northwest-1'
        if 'max_results' in args:args['max_results']=min(50,max(1,int(args['max_results'])))
    return args


def tool_name(context):
    full = context.client_context.custom['bedrockAgentCoreToolName']
    return full.split('___',1)[-1]


def decode_result(result):
    structured=getattr(result,'structuredContent',None)
    texts='\n'.join(getattr(c,'text','') for c in result.content if getattr(c,'type','')=='text')
    if structured:payload=structured.get('result',structured)
    else:
        try:payload=json.loads(texts)
        except (ValueError,TypeError):payload={'text':texts[:48000],'truncated':len(texts)>48000}
    if result.isError or isinstance(payload,dict) and (payload.get('status')=='error' or payload.get('error')):
        raise RuntimeError('官方 MCP 只读查询失败，未取得可核验数据；请检查筛选条件和后台权限。')
    return payload


def _handler(event, context):
    global _bridge
    target = os.environ['MCP_TARGET']
    name = tool_name(context)
    if name not in ALLOWED.get(target, frozenset()):
        raise PermissionError('Unsupported read-only MCP tool')
    args = bounded_arguments(target,name,event)
    # AWS API upstream is single-user: never reuse its process/files across
    # requests. Each call uses one dedicated service identity and fresh directory.
    isolated = tempfile.TemporaryDirectory(prefix='aws-api-', dir='/tmp') if target=='aws-api' else None
    bridge = Bridge(target, isolated.name) if isolated else None
    if not isolated:
        if _bridge is None or not _bridge.thread.is_alive(): _bridge=Bridge(target)
        bridge = _bridge
    try:
        if target=='cloudtrail-mcp':
            from tools.cloudtrail.pagination import lookup
            return lookup(args,lambda query,timeout:decode_result(bridge.invoke(name,query,timeout=timeout)))
        result = bridge.invoke(name,args)
    except Exception:
        bridge.close()
        if not isolated: _bridge=None
        raise RuntimeError('官方 MCP 只读查询失败，未取得可核验数据；请检查后台权限和日志。') from None
    finally:
        if isolated:
            bridge.close()
            bridge.thread.join(timeout=5)
            isolated.cleanup()
    payload=decode_result(result)
    if target=='aws-api':
        rows=payload if isinstance(payload,list) else [payload]
        for row in rows:
            response=row.get('response',{}) if isinstance(row,dict) else {}
            if response.get('error') or response.get('error_code') or (response.get('status_code') or 200)>=400:
                raise RuntimeError('AWS API 查询失败：'+str(response.get('error_code') or response.get('status_code') or 'upstream error'))
            if name=='call_aws' and 'lambda list-functions' in args['cli_command'] and response.get('as_json'):
                data=json.loads(response['as_json'])
                fields={'FunctionName','FunctionArn','Runtime','Description','Timeout','MemorySize','LastModified','PackageType','Architectures'}
                if 'Functions' in data:
                    data['Functions']=[{k:v for k,v in fn.items() if k in fields} for fn in data['Functions']]
                    response['as_json']=json.dumps(data,ensure_ascii=False)
    return redact(payload) if target in ('aws-api','agentcore-mcp') else payload


def handler(event, context):
    if os.environ['MCP_TARGET'] != 'aws-api':
        return _handler(event, context)
    # Lambda exceptions become opaque Gateway "internal error" messages.
    # Return a bounded, explicit adapter error so a denied CLI command cannot
    # be mistaken for an AWS service outage.
    try:
        return _handler(event, context)
    except (PermissionError, ValueError, TypeError, KeyError) as error:
        return {'status':'error','error':{'code':'CommandRejected','source':'mcp-adapter',
                'message':str(error)[:300]},'retryable':False}
    except (RuntimeError, TimeoutError) as error:
        return {'status':'error','error':{'code':'QueryFailed','source':'mcp-adapter',
                'message':str(error)[:300]},'retryable':False}
