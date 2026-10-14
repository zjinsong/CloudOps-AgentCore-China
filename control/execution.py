"""Durable expert execution independent of a caller's HTTP lifetime."""
import json
import os
import time
import uuid
import boto3
from control.store import store, Conflict

# Resume checkpoints (full model history) are only needed while a task can still continue.
EXECUTION_RETENTION_SECONDS = 7 * 24 * 3600

class YieldExecution(Exception):
    pass

def ident(task_id):
    return 'execution-' + task_id

def get(task_id):
    return store.get(ident(task_id))

def claim(t):
    old = get(t['id'])
    now = time.time()
    if old and (old['status'] in ('completed', 'failed', 'waiting_for_input') or old.get('lease_until', 0) > now):
        return old, False
    token = uuid.uuid4().hex
    value = {**(old or {}), 'id': ident(t['id']), 'kind': 'execution', 'task_id': t['id'], 'owner': t['owner'],
             'conversation_id': t['conversation_id'], 'user_id': t.get('user_id', 'admin'), 'status': 'running', 'token': token, 'lease_until': now + 120,
             'started_at': now, 'segment': (old or {}).get('segment', 0) + 1,
             'deadline': now + int(os.getenv('CLOUDOPS_SEGMENT_SECONDS', '1800')), 'model_calls': 0,
             'expires_at': int(now + EXECUTION_RETENTION_SECONDS),
             'max_model_calls': int(os.getenv('CLOUDOPS_MAX_MODEL_CALLS', '20')),
             'total_model_calls': (old or {}).get('total_model_calls', 0),
             'runtime_session': t.get('runtime_session', '')}
    if value['segment'] > 12:
        value.update(status='waiting_for_input', lease_until=0, error='任务自动续做已达12段，请检查进度后继续。')
    try:
        return store.put(value, old['version'] if old else None), value['status'] == 'running'
    except Conflict:
        return get(t['id']), False

def owned(run, fn):
    def change(x):
        if x.get('token') != run['token'] or x.get('lease_until', 0) <= time.time():
            raise Conflict('专家执行已被接管')
        return fn(x)
    return store.update(run['id'], change)

def guard(run, model=False):
    current = store.get(run['id'])
    t = store.get(run['task_id'])
    if not current or current['token'] != run['token'] or current.get('lease_until', 0) <= time.time():
        raise Conflict('旧执行不能继续调用工具')
    if t['status'] in ('paused', 'cancelled'):
        raise YieldExecution('用户暂停或取消任务')
    if time.time() > current['deadline'] or current['model_calls'] >= current['max_model_calls']:
        raise YieldExecution('本段执行预算已到，已保存进度')
    if current.get('total_model_calls', 0) >= 60:
        raise YieldExecution('模型调用总预算已到，需要用户检查后继续')
    if model:
        owned(run, lambda x: {**x, 'model_calls': x['model_calls'] + 1, 'total_model_calls': x.get('total_model_calls', 0) + 1})

def checkpoint(run, messages):
    owned(run, lambda x: x)
    raw = json.dumps(messages, ensure_ascii=False, default=str).encode()
    if len(raw) > 8 * 1024 * 1024:
        raise YieldExecution('会话检查点达到8MB，请拆分任务')
    key = 'cloudops/users/' + run.get('user_id', 'admin') + '/checkpoints/' + run['task_id'] + '/' + run['token'] + '/' + uuid.uuid4().hex + '.json'
    r = boto3.client('s3', region_name=os.getenv('AWS_REGION', 'cn-northwest-1')).put_object(Bucket=os.environ['CLOUDOPS_BUCKET'], Key=key, Body=raw, ServerSideEncryption='AES256')
    owned(run, lambda x: {**x, 'checkpoint': {'key': key, 'version': r.get('VersionId')}, 'message_count': len(messages), 'last_progress_at': time.time()})

def history(run):
    ref = run.get('checkpoint')
    if not ref:
        return []
    kw = {'Bucket': os.environ['CLOUDOPS_BUCKET'], 'Key': ref['key']}
    if ref.get('version'):
        kw['VersionId'] = ref['version']
    return json.loads(boto3.client('s3', region_name=os.getenv('AWS_REGION', 'cn-northwest-1')).get_object(**kw)['Body'].read())

def rpc_task(run):
    states = {'completed': 'completed', 'failed': 'failed', 'waiting_for_input': 'input-required', 'yielded': 'working'}
    result = {'kind': 'task', 'id': run['task_id'], 'contextId': run['conversation_id'],
              'status': {'state': states.get(run['status'], 'working')},
              'metadata': {'execution_status': run['status'], 'heartbeat': run.get('updated_at'), 'segment': run.get('segment'),
                           'checkpoint_messages': run.get('message_count', 0), 'error': run.get('error', '')}}
    if run.get('result'):
        result['artifacts'] = [{'artifactId': run['task_id'], 'parts': [{'kind': 'text', 'text': run['result']}], 'metadata': run.get('evidence', {})}]
    return result
