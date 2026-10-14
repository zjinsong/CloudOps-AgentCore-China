"""Actual 48-hour deployment observation, persisted across observer restarts."""
import json
import logging
import time
import urllib.request

from control.harness import cp
from control.store import store, Conflict

IDENT = 'observation-cloudops-20261003'
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('cloudops-observation')


def sample():
    failures = []
    with urllib.request.urlopen('http://control:8098/ping', timeout=10) as response:
        if json.load(response).get('status') != 'healthy':
            failures.append('control-health')
    for a in store.all('agent'):
        if a.get('runtime_arn') and a.get('enabled'):
            status = cp().get_agent_runtime(agentRuntimeId=a['runtime_arn'].split('/')[-1])['status']
            if status != 'READY':
                failures.append(a['name'] + ':' + status)
    now = time.time()
    tasks = store.all('task')
    stalled = [t['id'] for t in tasks if t['status'] == 'running' and t.get('lease_until', 0) < now - 180]
    failures.extend('stalled:' + ident for ident in stalled)
    return failures, len(tasks), sum(t['status'] == 'failed' for t in tasks)


def main():
    if not store.get(IDENT):
        try:
            store.put({'id': IDENT, 'kind': 'observation', 'status': 'running', 'started_at': time.time(),
                       'duration_seconds': 48 * 3600, 'samples': 0, 'error_samples': 0})
        except Conflict:
            pass
    while True:
        record = store.get(IDENT)
        if record['status'] == 'complete':
            time.sleep(600)
            continue
        try:
            failures, tasks, failed = sample()
        except Exception as e:
            failures, tasks, failed = ['sample:' + type(e).__name__], None, None
        now = time.time()
        result = store.update(IDENT, lambda x: {**x, 'last_at': now, 'samples': x['samples'] + 1,
            'error_samples': x['error_samples'] + bool(failures), 'last_failures': failures[:20],
            'failure_history': (x.get('failure_history', []) + ([{'at': now, 'failures': failures[:20]}] if failures else []))[-50:],
            'task_count': tasks, 'failed_task_count': failed,
            'status': 'complete' if now - x['started_at'] >= x['duration_seconds'] else 'running'})
        log.info('status=%s samples=%s error_samples=%s failed_tasks=%s', result['status'], result['samples'], result['error_samples'], failed)
        if failures:
            log.warning('observation failures=%s', failures)
        time.sleep(300)


if __name__ == '__main__':
    main()
