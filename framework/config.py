"""读取并解析 hierarchy.json —— 单一事实来源。

借鉴原项目:agent 的"中层/叶子"身份是**算出来的**(凡作为别人 parent 的即中层),
不在配置里硬写,这样加 agent 只需改 JSON。
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

# hierarchy.json 位于 demo/ 根目录
_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "hierarchy.json"


@lru_cache(maxsize=1)
def load_hierarchy(path: str | None = None) -> dict[str, Any]:
    """加载 hierarchy.json(带缓存)。"""
    p = Path(path) if path else _DEFAULT_PATH
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def get_agents(path: str | None = None) -> dict[str, dict]:
    """返回 agents 映射 {name: config}。"""
    return load_hierarchy(path).get("agents", {})


def get_agent(name: str, path: str | None = None) -> dict:
    """返回单个 agent 配置,不存在则抛错。"""
    agents = get_agents(path)
    if name not in agents:
        raise KeyError(f"agent '{name}' 不在 hierarchy.json 中。可用: {list(agents)}")
    return agents[name]


def compute_agent_role(name: str, path: str | None = None) -> str:
    """算出 agent 的实际角色:
    - frontend:type 显式为 frontend(用户入口)
    - mid-level:作为其他 agent 的 parent(children 非空且被引用)
    - leaf:其余(真正干活的叶子)
    借鉴原项目:parent 集合用集合运算算出,而非硬编码。
    """
    agents = get_agents(path)
    cfg = agents[name]
    if cfg.get("type") == "frontend":
        return "frontend"
    if cfg.get("children"):
        return "mid-level"
    return "leaf"


def resolve_model_id(name: str, path: str | None = None) -> str:
    """agent 的模型:优先 agent.model,否则用全局 model_default。"""
    h = load_hierarchy(path)
    return os.environ.get("MODEL_ID") or get_agent(name, path).get("model") or h.get("model_default", "configured-model")


def get_region(path: str | None = None) -> str:
    return os.environ.get("AWS_REGION") or load_hierarchy(path).get("region", "cn-northwest-1")
