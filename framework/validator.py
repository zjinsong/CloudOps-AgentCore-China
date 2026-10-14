"""hierarchy.json 规范校验 —— Harness agent 与开发期 CI 共用的核心逻辑。

校验三类规范:
1. 结构合法性:必填字段、type 合法、children 引用存在、无环。
2. 中国区合规:不得依赖中国区不可用能力(AgentCore Memory 组件、Cognito 入站、inference target)。
3. 安全/质量:worker 应声明 tools(fail-closed 原则);写操作 agent 应声明 approval_required。
"""
from __future__ import annotations

from typing import Any

_VALID_TYPES = {"frontend", "worker", "capability"}
_VALID_MEMORY = {"dynamodb", None}  # 中国区:记忆只能自建(dynamodb),不能用 AgentCore Memory 组件
_WRITE_HINT_TOOLS = ("cdk-deploy", "delete", "destroy", "put", "create", "update")


def validate_hierarchy(hierarchy: dict[str, Any]) -> dict[str, Any]:
    """校验一份 hierarchy 配置,返回 {valid, errors, warnings}。"""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(hierarchy, dict):
        return {"valid": False, "errors": ["配置必须是对象"], "warnings": []}
    agents = hierarchy.get("agents")
    if not isinstance(agents, dict) or not agents:
        return {"valid": False, "errors": ["缺少 agents 或为空"], "warnings": []}

    names = set(agents)

    # 1) 结构
    for name, cfg in agents.items():
        if not isinstance(cfg, dict):
            errors.append(f"{name}: 配置必须是对象")
            continue
        if not cfg.get("description"):
            errors.append(f"{name}: 缺少 description(路由依赖它)")
        atype = cfg.get("type")
        if atype not in _VALID_TYPES:
            errors.append(f"{name}: type '{atype}' 非法,应为 {_VALID_TYPES}")
        for field in ('children', 'tools', 'approval_required'):
            if field in cfg and (not isinstance(cfg[field], list) or any(not isinstance(x, str) for x in cfg[field])):
                errors.append(f"{name}: {field} 必须是字符串列表")
        children = cfg.get('children', [])
        for child in children if isinstance(children, list) else []:
            if not isinstance(child, str):
                continue
            if child not in names:
                errors.append(f"{name}: children 引用了不存在的 agent '{child}'")

    # 环检测(children 构成的有向图)
    if _has_cycle(agents):
        errors.append("agent 层级存在环(children 形成循环)")

    # 2) 中国区合规
    for name, cfg in agents.items():
        if not isinstance(cfg, dict):
            continue
        mem = cfg.get("memory")
        if not isinstance(mem, (str, type(None))) or mem not in _VALID_MEMORY:
            errors.append(
                f"{name}: memory='{mem}' 在中国区不可用;AgentCore Memory 组件缺失,只能用 'dynamodb' 自建"
            )
        auth = str(cfg.get("gateway_inbound_auth") or "").lower()
        if auth in ("cognito", "cognito_user_pool"):
            errors.append(f"{name}: 中国区 Gateway 不支持 Cognito user pool 入站鉴权,请用 AWS_IAM 或 CUSTOM_JWT")
        if cfg.get("inference_target"):
            errors.append(f"{name}: inference target 在中国区不可用")

    # 3) 安全/质量
    for name, cfg in agents.items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("type") == "worker" and not cfg.get("children") and not cfg.get("tools"):
            warnings.append(f"{name}: worker 未声明 tools,将因 fail-closed 无法工作(除非它是纯 LLM 叶子)")
        tools = cfg.get("tools", []) or []
        tools = tools if isinstance(tools, list) else []
        has_write = any(any(h in str(t).lower() for h in _WRITE_HINT_TOOLS) for t in tools)
        if has_write and not cfg.get("approval_required"):
            warnings.append(f"{name}: 含疑似写操作工具但未声明 approval_required,建议加人工审批护栏")

    return {"valid": not errors, "errors": errors, "warnings": warnings}


def _has_cycle(agents: dict[str, Any]) -> bool:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in agents}

    def dfs(n: str) -> bool:
        color[n] = GRAY
        cfg = agents.get(n)
        children = cfg.get('children', []) if isinstance(cfg, dict) else []
        for c in children if isinstance(children, list) else []:
            if not isinstance(c, str):
                continue
            if c not in color:
                continue
            if color[c] == GRAY:
                return True
            if color[c] == WHITE and dfs(c):
                return True
        color[n] = BLACK
        return False

    return any(color[n] == WHITE and dfs(n) for n in agents)


def generate_agent_config(
    name: str, role: str, description: str, tools: list[str] | None = None,
    model: str = "deepseek-v4-pro", prompt: str = "",
) -> dict[str, Any]:
    """按需求生成一个合规的 agent 配置片段(中国区默认值)。"""
    cfg: dict[str, Any] = {
        "type": role if role in _VALID_TYPES else "worker",
        "model": model,
        "protocol": "HTTP",
        "description": description,
        "prompt": prompt or f"你是 {name} 专家。",
    }
    if tools:
        cfg["tools"] = tools
    return {name: cfg}
