"""Experts discover business tools exclusively through the IAM MCP Gateway."""
import os
from contextlib import contextmanager
from framework.gateway import get_gateway_mcp_client, load_gateway_tools

@contextmanager
def tools_for(cfg):
    targets = cfg.get('tools', [])
    if not targets:
        yield []
        return
    with get_gateway_mcp_client(os.environ['GATEWAY_URL']) as client:
        loaded = load_gateway_tools(client, targets)
        for target in targets:
            if not any(t.tool_name.startswith(target + '___') for t in loaded):
                raise RuntimeError('声明工具未加载：' + target)
        yield loaded
