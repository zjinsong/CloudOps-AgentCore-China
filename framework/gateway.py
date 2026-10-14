"""连接 AgentCore Gateway(MCP over streamable-HTTP, AWS_IAM 鉴权)。

借鉴原项目:
- 用 botocore SigV4Auth 签一个"影子" AWSRequest,把签名头回写进 httpx 请求
  (service=bedrock-agentcore),从而用 IAM 角色而非 OAuth 访问 Gateway。
- 工具加载必须循环 paginationToken 排干(Gateway 每页约 30 个),
  并按 "<target>___" 前缀过滤出该 agent 被授权的工具(allowed_tools 来自 hierarchy.json)。
"""
from __future__ import annotations

import os

import boto3
import httpx
from botocore.auth import SigV4Auth as _BotoSigV4Auth
from botocore.awsrequest import AWSRequest

_REGION = os.environ.get("AWS_REGION", "cn-northwest-1")
_SERVICE = "bedrock-agentcore"


class SigV4Auth(httpx.Auth):
    """httpx 鉴权插件:对每个请求做 SigV4 签名。"""

    requires_request_body = True

    def __init__(self, region: str = _REGION, service: str = _SERVICE):
        self._session = boto3.Session()
        self._region = region
        self._service = service

    def auth_flow(self, request: httpx.Request):
        creds = self._session.get_credentials().get_frozen_credentials()
        headers={"Content-Type": request.headers.get("Content-Type", "application/json")}
        from opentelemetry import trace
        context=trace.get_current_span().get_span_context()
        if context.is_valid:
            headers['traceparent']=f'00-{context.trace_id:032x}-{context.span_id:016x}-01'
            trace_id=f'{context.trace_id:032x}'
            headers['X-Amzn-Trace-Id']='Root=1-'+trace_id[:8]+'-'+trace_id[8:]+';Parent='+f'{context.span_id:016x}'+';Sampled=1'
        # 用 botocore 签一个"影子"请求,拿到签名头回写进 httpx 请求
        aws_req = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers=headers,
        )
        _BotoSigV4Auth(creds, self._service, self._region).add_auth(aws_req)
        for k, v in aws_req.headers.items():
            request.headers[k] = v
        yield request


def get_gateway_mcp_client(gateway_url: str):
    """返回连到 Gateway MCP 端点的 Strands MCPClient。

    新版 mcp 的 streamable http client 接受一个 httpx client(http_client),
    我们传入带 SigV4 Auth 的 AsyncClient 实现 IAM 鉴权;
    旧版接受 auth 参数,做兼容。
    """
    import httpx
    from strands.tools.mcp import MCPClient

    auth = SigV4Auth()

    try:
        # 新版:streamable_http_client(url, http_client=...)
        from mcp.client.streamable_http import streamable_http_client

        def _factory():
            return streamable_http_client(
                gateway_url, http_client=httpx.AsyncClient(auth=auth, timeout=60)
            )
    except ImportError:
        # 旧版:streamablehttp_client(url, auth=...)
        from mcp.client.streamable_http import streamablehttp_client

        def _factory():
            return streamablehttp_client(gateway_url, auth=auth)

    return MCPClient(_factory)


def load_gateway_tools(mcp_client, allowed_prefixes: list[str]) -> list:
    """从 Gateway 拉取工具,按 <target>___ 前缀过滤出被授权的工具。

    Args:
        mcp_client: 已进入上下文的 MCPClient。
        allowed_prefixes: hierarchy.json 中该 agent 的 tools 列表(即 target 名)。

    Returns:
        过滤后的工具列表,可直接传给 Strands Agent(tools=...)。
    """
    all_tools = []
    pagination_token = None
    # 循环排干分页(Gateway 每页约 30 个)
    while True:
        result = mcp_client.list_tools_sync(pagination_token=pagination_token)
        # 兼容不同返回形态:可能是列表,或带 .tools / 分页 token 的对象
        tools = getattr(result, "tools", result)
        all_tools.extend(tools)
        pagination_token = getattr(result, "pagination_token", None) or getattr(
            result, "nextToken", None
        )
        if not pagination_token:
            break

    if not allowed_prefixes:
        return []

    def _name(t):
        return getattr(t, "tool_name", None) or getattr(t, "name", "")

    return [t for t in all_tools if any(_name(t).startswith(f"{p}___") for p in allowed_prefixes)]
