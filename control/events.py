"""Deduplicate customer-account alarms and Health notifications into investigations."""
import hashlib
import json
import os
import time

from control.store import store, Conflict
from control import domain


def ingest(event):
    if event.get('source') not in ('aws.cloudwatch', 'aws.health') or event.get('account') != os.environ.get('CLOUDOPS_ACCOUNT'):
        raise PermissionError('事件来源或账户未获授权')
    event_id = 'cloud-event-' + hashlib.sha256(event['id'].encode()).hexdigest()
    if store.get(event_id):
        return
    detail = event.get('detail', {})
    key = detail.get('alarmName') or detail.get('eventArn') or event['id']
    issue_id = 'issue-' + hashlib.sha256((event['source'] + key).encode()).hexdigest()
    state = detail.get('state', {}).get('value') if event['source'] == 'aws.cloudwatch' else detail.get('statusCode', 'open')
    issue = store.get(issue_id)
    record = {'id': event_id, 'kind': 'cloud_event', 'customer_id': 'customer-default', 'conversation_id': 'cloudops',
              'source': event['source'], 'event': event, 'created_at': time.time(), 'version': 1}
    next_issue = {'id': issue_id, 'kind': 'issue', 'customer_id': 'customer-default', 'conversation_id': 'cloudops',
                  'resource': key, 'state': state, 'last_event': event['id'], 'event_time': event.get('time', ''),
                  'version': (issue['version'] if issue else 0) + 1, 'created_at': issue['created_at'] if issue else time.time()}
    # Ignore late state changes while retaining the evidence.
    if issue and event.get('time', '') < issue.get('event_time', ''):
        store.put(record)
        return
    should_investigate = state in ('ALARM', 'open', 'upcoming') and (not issue or issue['state'] != state)
    if issue and issue.get('task_id'):
        next_issue['task_id'] = issue['task_id']
    if should_investigate:
        next_issue['task_id'] = 'task-' + event_id
    store.atomic([(record, None), (next_issue, issue['version'] if issue else None)])
    if should_investigate:
        c = domain.require_conversation('cloudops')
        try:
            t = domain.task(c, '调查云事件并附证据、影响与复查建议：\n' + json.dumps(event, ensure_ascii=False)[:8000], owner='monitoring', ident=next_issue['task_id'])
            store.event(t, '收到云事件，创建调查。')
        except Conflict:
            pass
    elif state in ('OK', 'closed') and next_issue.get('task_id'):
        t = store.get(next_issue['task_id'])
        if t:
            store.event(t, '事件来源报告恢复/关闭；这是事件状态，仍需结合指标验证实际恢复。')


def repair():
    for issue in store.all('issue'):
        if issue.get('task_id') and not store.get(issue['task_id']) and issue['state'] in ('ALARM', 'open', 'upcoming'):
            event_id = issue['task_id'].removeprefix('task-')
            record = store.get(event_id)
            if record:
                domain.task(domain.require_conversation('cloudops'), '调查事件：' + json.dumps(record['event'], ensure_ascii=False)[:8000],
                            owner='monitoring', ident=issue['task_id'])
