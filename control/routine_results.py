"""Read this user's stored scheduled-run outputs for the Supervisor (no AWS or model calls)."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from control import results
from control.store import store

ZONE = ZoneInfo('Asia/Shanghai')

# Supervisor-owned platform ability. It runs inside the control plane, so it needs
# no Gateway tool and is not part of an expert's mounted capabilities.
ABILITY = {'id': 'read_routine_results', 'expert': 'supervisor', 'tools': [], 'regions': [], 'access': 'read_only',
           'description': '读取当前用户各专家定时任务（巡检）已保存的运行结果，用于汇总、对比或回顾定时任务输出；不重新查询 AWS。'
                          '支持今天、昨天、YYYY-MM-DD（北京时间），默认每个定时任务最近一次运行。',
           'inputs': ['日期范围', '专家或定时任务名称（可选）'],
           'outputs': ['routine_results'], 'platform': True}


def window(prompt):
    now = datetime.now(ZONE)
    if re.search(r'今天|今日|\btoday\b', prompt, re.I):
        day = now.date()
    elif re.search(r'昨天|昨日|\byesterday\b', prompt, re.I):
        day = (now - timedelta(days=1)).date()
    else:
        match = re.search(r'\b(20\d{2}-\d{2}-\d{2})\b', prompt)
        day = datetime.strptime(match[1], '%Y-%m-%d').date() if match else None
    if not day:
        return None, None
    start = datetime.combine(day, datetime.min.time(), ZONE).timestamp()
    return str(day), start


def query(user_id, prompt='', exclude_routine=None, per_routine=1, body_chars=4000):
    """Owned, bounded results. Excludes the calling routine so a summary never reads itself."""
    day, start = window(prompt)
    routines = {r['id']: r for r in store.all('routine')
                if r.get('user_id', 'admin') == user_id and not r.get('deleted_at') and r['id'] != exclude_routine}
    runs = {}
    for task in store.all('task'):
        rid = task.get('routine_id')
        if rid not in routines or task.get('parent_id') or task.get('user_id', 'admin') != user_id:
            continue
        when = task.get('finished_at') or task.get('scheduled_for') or task.get('created_at', 0)
        runs.setdefault(rid, []).append((when, task))
    rows = []
    for rid, routine in sorted(routines.items(), key=lambda x: x[1].get('name', '')):
        ordered = sorted(runs.get(rid, []), key=lambda x: x[0], reverse=True)
        in_range = [r for r in ordered if start is None or start <= r[0] < start + 86400]
        # A summary scheduled before the experts' runs would otherwise always see an empty day;
        # fall back to the most recent run and say so.
        fallback = start is not None and not in_range and bool(ordered)
        latest = (in_range or (ordered if fallback else []))[:per_routine]
        if not latest:
            rows.append({'routine': routine.get('name', rid), 'expert': routine.get('owner'), 'enabled': routine.get('enabled'),
                         'runs': [], 'note': '该范围内没有运行记录'})
            continue
        items = []
        for when, task in latest:
            body = ''
            if task.get('status') == 'succeeded':
                try:
                    body = results.read(task)
                except Exception as exc:  # stored body unavailable; keep the summary instead of failing all routines
                    body = task.get('result_summary', '') + f'\n[完整结果读取失败：{type(exc).__name__}]'
            items.append({'task_id': task['id'], 'status': task.get('status'),
                          'finished_at': datetime.fromtimestamp(when, ZONE).strftime('%Y-%m-%d %H:%M:%S'),
                          'result': body[:body_chars], 'truncated': len(body) > body_chars, 'error': task.get('error')})
        row = {'routine': routine.get('name', rid), 'expert': routine.get('owner'), 'enabled': routine.get('enabled'), 'runs': items}
        if fallback:
            row['note'] = f'{day} 当天没有运行记录，以下为最近一次运行'
        rows.append(row)
    return {'source': '当前用户在本平台保存的定时任务运行结果；不是本次实时查询', 'timezone': 'Asia/Shanghai',
            'date': day, 'scope': day or '每个定时任务最近一次运行', 'routines': rows}
