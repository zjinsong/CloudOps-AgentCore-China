"""Calendar schedules and independent runs; Runtime is invoked by the worker."""
import calendar
import datetime as dt
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from control.store import store, uid, Conflict, HISTORY_RETENTION_SECONDS
from control import domain


def validate(value):
    try:
        zone = ZoneInfo(value.get('timezone', 'Asia/Shanghai'))
        date = dt.date.fromisoformat(value['start_date'])
        clock = dt.time.fromisoformat(value['time'])
    except (KeyError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError('请填写有效日期、时间和 IANA 时区，例如 Asia/Shanghai') from exc
    if clock.second or clock.microsecond or clock.tzinfo:
        raise ValueError('执行时间精确到分钟')
    repeat = value.get('repeat', 'daily')
    if repeat not in ('once', 'daily', 'weekly', 'monthly', 'interval'):
        raise ValueError('重复规则无效')
    local = dt.datetime.combine(date, clock, zone)
    if dt.datetime.fromtimestamp(local.timestamp(), zone).replace(tzinfo=None) != local.replace(tzinfo=None):
        raise ValueError('该时间处于夏令时跳过的时间段，请选择其他时间')
    return {'start_date': date.isoformat(), 'time': clock.strftime('%H:%M'), 'timezone': zone.key,
            'repeat': repeat, 'interval_hours': float(value.get('interval_hours', 24))}


def next_time(schedule, after):
    zone = ZoneInfo(schedule['timezone'])
    date = dt.date.fromisoformat(schedule['start_date'])
    clock = dt.time.fromisoformat(schedule['time'])
    anchor = dt.datetime.combine(date, clock, zone)
    repeat = schedule['repeat']
    if repeat == 'once':
        return anchor.timestamp() if anchor.timestamp() > after else None
    if repeat == 'interval':
        seconds = schedule['interval_hours'] * 3600
        if seconds < 3600 or seconds > 720 * 3600:
            raise ValueError('间隔必须为 1 至 720 小时')
        return anchor.timestamp() + max(0, int((after - anchor.timestamp()) // seconds) + 1) * seconds
    today = dt.datetime.fromtimestamp(after, zone).date()
    if repeat == 'monthly':
        offset = max(0, (today.year - date.year) * 12 + today.month - date.month)
        for n in range(offset, offset + 3):
            month = date.year * 12 + date.month - 1 + n
            year, index = divmod(month, 12)
            candidate = dt.date(year, index + 1, min(date.day, calendar.monthrange(year, index + 1)[1]))
            stamp = dt.datetime.combine(candidate, clock, zone).timestamp()
            if stamp > after:
                return stamp
    else:
        step = 7 if repeat == 'weekly' else 1
        offset = max(0, (today - date).days // step)
        for n in range(offset, offset + 3):
            candidate = dt.datetime.combine(date + dt.timedelta(days=n * step), clock, zone)
            # A future DST gap moves this run to the first valid local minute.
            for _ in range(180):
                if dt.datetime.fromtimestamp(candidate.timestamp(), zone).replace(tzinfo=None) == candidate.replace(tzinfo=None):
                    break
                candidate += dt.timedelta(minutes=1)
            if candidate.timestamp() > after:
                return candidate.timestamp()
    raise ValueError('无法计算下次执行时间')


def require(ident, user, include_deleted=False):
    value = store.get(ident)
    if not value or value.get('kind') != 'routine' or value.get('user_id', 'admin') != user or (value.get('deleted_at') and not include_deleted):
        raise PermissionError('定时任务不属于当前用户')
    return value


def delete(ident, user):
    require(ident, user, include_deleted=True)
    return store.update(ident, lambda x: {**x, 'enabled': False, 'next_at': None,
                        'deleted_at': x.get('deleted_at') or time.time(),
                        'schedule_revision': x.get('schedule_revision', 0) + 1})


def save(user, value, ident=None):
    old = require(ident, user) if ident else None
    agent = store.get('agent-' + value['owner'])
    if not agent or not agent.get('enabled'):
        raise ValueError('Agent 不存在或已停用')
    if old and old['owner'] != value['owner']:
        raise ValueError('编辑时不能改变所属 Agent')
    schedule = validate(value)
    upcoming = next_time(schedule, time.time())
    if upcoming is None:
        raise ValueError('单次任务的执行时间必须在未来')
    record = {**(old or {}), 'id': ident or uid('routine'), 'kind': 'routine', 'user_id': user,
              'owner': value['owner'], 'name': value['name'].strip(), 'prompt': value['prompt'].strip(),
              'enabled': value.get('enabled', True), 'schedule': schedule, 'timezone': schedule['timezone'],
              'interval_hours': schedule['interval_hours'], 'next_at': upcoming,
              'schedule_revision': (old or {}).get('schedule_revision', 0) + 1}
    if not record['name'] or not record['prompt']:
        raise ValueError('任务名称和执行要求不能为空')
    return store.put(record, old['version'] if old else None)


def migrate():
    for record in store.all('routine'):
        if record.get('schedule'):
            continue
        local = dt.datetime.fromtimestamp(record.get('next_at', time.time() + 86400), ZoneInfo(record.get('timezone', 'Asia/Shanghai')))
        schedule = {'start_date': local.date().isoformat(), 'time': local.strftime('%H:%M'),
                    'timezone': local.tzinfo.key, 'repeat': 'daily' if record.get('interval_hours', 24) == 24 else 'interval',
                    'interval_hours': record.get('interval_hours', 24)}
        store.update(record['id'], lambda x: {**x, 'schedule': schedule, 'schedule_revision': 1})


def tick(now=None):
    now = time.time() if now is None else now
    for record in store.all('routine'):
        if record.get('deleted_at') or not record.get('enabled') or not record.get('next_at') or record['next_at'] > now:
            continue
        slot = record['next_at']
        ident = f"task-{record['id']}-{int(slot)}"
        # Catch up at most one overdue run, then calculate the next future slot.
        conversation_id = 'scheduled-' + ident
        conversation = store.get(conversation_id)
        if not conversation:
            agent = store.get('agent-' + record['owner'])
            if not agent or not agent.get('enabled'):
                continue
            members = [a['name'] for a in domain.registry.active()] if record['owner'] == 'supervisor' else [record['owner']]
            try:
                conversation = store.put({'id': conversation_id, 'kind': 'conversation', 'user_id': record.get('user_id', 'admin'),
                    'customer_id': 'customer-default', 'owner': record['owner'], 'members': members, 'title': record['name'],
                    'scheduled': True, 'routine_id': record['id'], 'file_ids': [], 'event_sequence': 0,
                    'expires_at': int(now + HISTORY_RETENTION_SECONDS)})
            except Conflict:
                conversation = store.get(conversation_id)
        upcoming = next_time(record['schedule'], now)
        updated = {**record, 'next_at': upcoming, 'enabled': bool(upcoming), 'last_task': ident,
                   'last_at': now, 'version': record['version'] + 1, 'updated_at': time.time()}
        if not store.get(ident):
            try:
                domain.task(conversation, record['prompt'], record['owner'], ident=ident,
                            metadata={'routine_id': record['id'], 'scheduled_for': slot, 'schedule_revision': record.get('schedule_revision', 1),
                                      'routine_name': record['name']}, commit=[(updated, record['version'])])
            except Conflict:
                pass
        else:
            try:
                store.put(updated, record['version'])
            except Conflict:
                pass


def history(record):
    values = [task for task in store.all('task') if task.get('user_id', 'admin') == record.get('user_id', 'admin')
              and not task.get('parent_id')
              and (task.get('routine_id') == record['id'] or task['id'].startswith('task-' + record['id'] + '-'))]
    return [{k: task.get(k) for k in ('id', 'status', 'scheduled_for', 'created_at', 'started_at', 'finished_at',
             'result_summary', 'error', 'routine_name', 'conversation_id', 'schedule_revision')} for task in values[-20:][::-1]]
