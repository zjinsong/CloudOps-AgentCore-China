"""Single registry consumed by sidebar, mentions and Supervisor dispatch."""
import re
from control.store import store, Conflict

TOOLS = {'cost-explorer', 'pricing', 'cloudwatch', 'health-events', 'cloudtrail-mcp', 'terraform-mcp', 'aws-api', 'agentcore-mcp'}
LABELS = {'supervisor': 'CloudOps 协调员', 'finops': 'FinOps 成本专家', 'monitoring': 'Monitoring 监控专家',
          'cloudtrail': 'CloudTrail 审计专家', 'terraform': 'Terraform 基础设施专家', 'agentcore': 'AgentCore 平台专家'}
INTROS = {'supervisor': '我是 CloudOps 协调员。告诉我目标，我会安排专家、跟进任务并汇总结果；也可以用 @ 指定专家。',
          'finops': '我是成本专家，查询实际费用与公开服务价格、分析变化并提出优化建议。',
          'monitoring': '我是监控专家，检查 CloudWatch 告警和已接入的 Health 事件。',
          'cloudtrail': '我是审计专家，通过 CloudTrail MCP 查询操作记录、分析重要变更。',
          'terraform': '我是 Terraform 专家，生成脚本和变更预览；你确认具体 plan 后，我再部署。',
          'agentcore': '我是 AgentCore 平台专家，通过官方 MCP 检查中国区 Runtime、Gateway 和 Identity 配置，结合官方文档诊断问题。'}
DEFAULT_ROUTINES = {
    'finops': ('每日费用巡检', '查询最近七天费用，比较最近两个完整自然日，列出费用变化最大的服务和异常增长；标注日期、货币与估算状态。'),
    'monitoring': ('每日告警与健康巡检', '检查当前 CloudWatch 告警和接入后收到的 Health 事件，说明异常及建议；没有收到 Health 事件不代表没有健康问题。'),
    'cloudtrail': ('每日重要操作巡检', '通过 CloudTrail MCP 查询最近24小时管理事件，关注删除、权限与安全配置变更及失败操作；列出时间、操作者、事件和调查建议。仅只读调查。'),
}

def active():
    return [a for a in store.all('agent') if a.get('enabled') and a['name'] not in ('harness', 'cdk')]

def validate(name, cfg):
    if not re.fullmatch(r'[a-z][a-z0-9_-]{1,40}', name) or name in ('harness', 'cdk'):
        raise ValueError('专家名称无效或已退出本阶段')
    if not cfg.get('description') or not cfg.get('prompt'):
        raise ValueError('需要角色说明和提示词')
    if set(cfg.get('tools', [])) - TOOLS:
        raise ValueError('未知工具，需要先实现并验证适配器')
    if name != 'supervisor' and cfg.get('type', 'worker') != 'worker':
        raise ValueError('新增专家必须为 worker')

def register(name, cfg, runtime_arn='', runtime_version=None):
    validate(name, cfg)
    old = store.get('agent-' + name)
    value = {'id': 'agent-' + name, 'kind': 'agent', 'name': name, 'display_name': cfg.get('display_name', LABELS.get(name, name)),
             'intro': INTROS.get(name, cfg['description']), 'config': cfg, 'runtime_arn': runtime_arn or (old or {}).get('runtime_arn', ''),
             'runtime_version': runtime_version, 'protocol': 'HTTP' if name == 'supervisor' else 'A2A', 'enabled': True}
    store.put({**(old or {}), **value}, old['version'] if old else None)
    cid = 'cloudops' if name == 'supervisor' else 'direct-' + name
    c = store.get(cid)
    conversation = {'id': cid, 'kind': 'conversation', 'name': value['display_name'], 'group': False, 'fixed': True,
                    'owner': name, 'user_id': 'admin', 'customer_id': 'customer-default', 'members': [name], 'file_ids': [], 'archived': False}
    if not c:
        store.put(conversation)
    else:
        store.update(cid, lambda x: {**x, 'name': value['display_name'], 'group': False, 'archived': False})
    refresh_team()
    return store.get('agent-' + name)


def refresh_team():
    members = [a['name'] for a in active()]
    for conversation in store.all('conversation'):
        if conversation.get('owner') == 'supervisor' and not conversation.get('deleted_at') and not conversation.get('archived'):
            if set(conversation.get('members', [])) != set(members):
                store.update(conversation['id'], lambda x: {**x, 'members': members})

def migrate():
    """Keep historical records, disable removed experts and their future schedules."""
    for name in ('harness', 'cdk'):
        if store.get('agent-' + name):
            store.update('agent-' + name, lambda x: {**x, 'enabled': False})
    for c in store.all('conversation'):
        if not c.get('archived') and (c['id'] == 'direct-supervisor' or (c.get('group') and c['id'] != 'cloudops') or c.get('owner') in ('harness', 'cdk')):
            store.update(c['id'], lambda x: {**x, 'archived': True})
    for r in store.all('routine'):
        if r.get('enabled') and r.get('owner') not in [a['name'] for a in active()]:
            store.update(r['id'], lambda x: {**x, 'enabled': False})

def seed_routines():
    import os
    import time
    for name, (title, prompt) in DEFAULT_ROUTINES.items():
        ident = 'routine-default-' + name
        if not store.get(ident):
            # Existing cost routine is superseded, retain its task evidence.
            for r in store.all('routine'):
                if r.get('owner') == name and r.get('name') in ('每日只读成本巡检', '每日成本巡检', '每日成本观察'):
                    store.update(r['id'], lambda x: {**x, 'enabled': False})
            try:
                store.put({'id': ident, 'kind': 'routine', 'name': title, 'prompt': prompt, 'owner': name,
                           'user_id': 'admin', 'conversation_id': 'direct-' + name, 'interval_hours': 24, 'enabled': os.getenv('CLOUDOPS_ENABLE_DEFAULT_ROUTINES') == 'true',
                           'next_at': time.time() + 86400, 'timezone': 'Asia/Shanghai', 'default': True})
            except Conflict:
                pass
