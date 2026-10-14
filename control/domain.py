import json
import os
import time
from pathlib import Path

from control.store import store, uid, Conflict, HISTORY_RETENTION_SECONDS

BUCKET = os.getenv('CLOUDOPS_BUCKET', '')
from control import registry
NAMES = ()  # hierarchy.json is the source of truth.
INTRO = registry.INTROS
TERMINAL = {'succeeded', 'failed', 'cancelled'}


def seed():
    hierarchy = json.loads(Path('hierarchy.json').read_text(encoding='utf-8'))
    arns = json.loads(os.getenv('SUB_AGENT_ARNS', '{}'))
    if not store.get('customer-default'):
        store.put({'id': 'customer-default', 'kind': 'customer', 'name': '默认客户', 'account': os.getenv('CLOUDOPS_ACCOUNT', ''), 'region': os.getenv('AWS_REGION', 'cn-northwest-1')})
    # Registry is shared; offline tests replace domain.store.
    registry.store = store
    for name, cfg in hierarchy['agents'].items():
        if not store.get('agent-' + name):
            registry.register(name, cfg, arns.get(name, ''))
    registry.migrate()
    registry.seed_routines()
    from control import schedules
    schedules.migrate()
    # Preserve the original single-admin records while making ownership explicit.
    for kind in ('conversation', 'task', 'routine', 'approval', 'terraform', 'file', 'knowledge'):
        for record in store.all(kind):
            if not record.get('user_id'):
                store.update(record['id'], lambda x: {**x, 'user_id': 'admin'})


def create_conversation(user_id, owner='supervisor', previous_id=None):
    agent = store.get('agent-' + owner)
    if not agent or not agent.get('enabled'):
        raise ValueError('专家不存在或已停用')
    previous_id = previous_id or (store.get('preferences-' + user_id) or {}).get('active', {}).get(owner)
    if previous_id:
        previous = require_conversation(previous_id, user_id)
        if previous['owner'] != owner or previous.get('scheduled'):
            raise PermissionError('上一对话不属于当前 Agent')
        from control import titles
        titles.enqueue(previous)
    ident = uid('conversation')
    members = [a['name'] for a in registry.active()] if owner == 'supervisor' else [owner]
    value = store.put({'id': ident, 'kind': 'conversation', 'user_id': user_id,
                      'customer_id': 'customer-default', 'owner': owner, 'members': members,
                      'name': agent.get('display_name', owner), 'title': '新对话', 'group': False,
                      'fixed': False, 'file_ids': [], 'archived': False, 'event_sequence': 0,
                      'expires_at': int(time.time() + HISTORY_RETENTION_SECONDS)})
    activate(value, user_id)
    return value


def activate(conversation, user_id):
    ident = 'preferences-' + user_id
    old = store.get(ident)
    if not old:
        try:
            store.put({'id': ident, 'kind': 'preferences', 'user_id': user_id, 'active': {conversation['owner']: conversation['id']}})
        except Conflict:
            old = store.get(ident)
    if old:
        store.update(ident, lambda x: {**x, 'active': {**x.get('active', {}), conversation['owner']: conversation['id']}})
    store.update(conversation['id'], lambda x: {**x, 'last_active_at': time.time()})


def title(prompt):
    import re
    text = re.sub(r'@[a-zA-Z][\w-]*\s*', '', prompt).strip()
    text = re.sub(r'^(请|麻烦|帮我|请帮我|可以帮我|请你)+', '', text).strip()
    text = re.split(r'[。！？\n]', text)[0].strip()
    return text[:22].rstrip('，,：:；; ') or '新对话'


def require_conversation(ident, user_id=None):
    c = store.get(ident)
    if not c or c['kind'] != 'conversation':
        raise ValueError('会话不存在')
    if user_id is not None and c.get('user_id', 'admin') != user_id:
        raise PermissionError('会话不属于当前用户')
    if user_id is not None and c.get('deleted_at'):
        raise PermissionError('对话已删除')
    return c


def permitted_files(c, ids):
    result = []
    for ident in ids:
        f = store.get(ident)
        if not f or f['kind'] != 'file' or f.get('user_id', 'admin') != c.get('user_id', 'admin') or f.get('conversation_id') != c['id']:
            raise PermissionError('资料未授权给当前客户和会话')
        result.append(f)
    return result


def task(c, prompt, owner=None, parent=None, files=None, ident=None, dependencies=None, metadata=None, commit=None):
    if c.get('archived'):
        raise ValueError('会话已停用')
    owner = owner or c['owner']
    if owner not in c['members'] or not store.get('agent-' + owner) or not store.get('agent-' + owner)['enabled']:
        raise PermissionError('专家未授权或已停用')
    files = files if files is not None else c.get('file_ids', [])
    permitted_files(c, files)
    inherited = {k: c[k] for k in ('routine_id',) if k in c}
    from framework.telemetry import identity
    trace_id, root_span_id = identity()
    tracing = {'trace_id': trace_id, 'root_span_id': root_span_id}
    if parent:
        upstream = store.get(parent) or {}
        tracing = {k: upstream[k] for k in ('trace_id', 'root_span_id') if k in upstream}
        tracing['root_task_id'] = upstream.get('root_task_id', parent)
    return store.create_task({'id': ident or uid('task'), 'kind': 'task', 'conversation_id': c['id'], 'customer_id': c['customer_id'], 'user_id': c.get('user_id', 'admin'),
                              'owner': owner, 'prompt': prompt, 'parent_id': parent, 'file_ids': files, 'status': 'queued',
                              'phase': 'plan' if owner == 'supervisor' else 'execute', 'attempts': 0, 'result': '', 'children': [],
                              'dependencies': dependencies or [], **inherited, **(metadata or {}), **tracing, 'expires_at': int(time.time() + HISTORY_RETENTION_SECONDS)}, commit=commit)


def transition(ident, status):
    def change(t):
        if t['status'] in TERMINAL and not (t['status'] == 'failed' and status == 'queued'):
            raise ValueError('任务已结束')
        if status not in ('paused', 'cancelled', 'queued'):
            raise ValueError('不支持的状态')
        if status == 'queued' and t['status'] not in ('paused', 'waiting_for_input', 'failed'):
            raise ValueError('当前任务不能恢复')
        if status == 'queued' and t.get('lease_until', 0) > time.time():
            raise ValueError('等待在途调用结束后再恢复')
        if status == 'queued' and t['status'] == 'failed':
            return {**t, 'status': status, 'attempts': 0, 'retry_at': 0, 'error': ''}
        return {**t, 'status': status}
    t = store.update(ident, change)
    if status == 'queued':
        from control import execution
        r = execution.get(ident)
        if r and r['status'] in ('failed', 'waiting_for_input', 'yielded'):
            store.update(r['id'], lambda x: {**x, 'status':'yielded', 'lease_until':0, 'total_model_calls':0, 'segment':0})
    store.event(t, {'paused': '任务已暂停；在途调用结束后不再启动下一步。', 'cancelled': '任务已取消，已经发生的操作不会自动回滚。', 'queued': '任务已恢复。'}[status])
    for child in t.get('children', []):
        sub = store.get(child)
        if sub and sub['status'] not in TERMINAL:
            try:
                transition(child, status)
            except ValueError:
                pass
    return t
