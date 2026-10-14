"""Versioned records. DynamoDB in deployment, SQLite only for offline tests."""
import json
import os
import sqlite3
import threading
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def uid(prefix):
    return prefix + '-' + uuid.uuid4().hex


class Conflict(Exception):
    pass


# Records that still need work carry `active` (sparse GSI active-index). Dispatch loops
# query this small index instead of scanning every task ever created.
ACTIVE_INDEX = 'active-index'
TERMINAL_TASK = {'succeeded', 'failed', 'cancelled'}
# DynamoDB TTL (attribute expires_at) and S3 lifecycle. Conversations, messages, tasks, events,
# files and routine runs are kept 30 days; delivery bookkeeping only needs days.
HISTORY_RETENTION_SECONDS = 30 * 24 * 3600
OUTBOX_RETENTION_SECONDS = 7 * 24 * 3600


def active_kind(r):
    if r.get('kind') == 'task' and r.get('status') not in TERMINAL_TASK:
        return 'task'
    if r.get('kind') == 'outbox' and not r.get('sent'):
        return 'outbox'
    if r.get('kind') == 'conversation_title' and r.get('status') in ('queued', 'running'):
        return 'conversation_title'
    return None


class Store:
    def __init__(self):
        self.table_name = os.getenv('CLOUDOPS_TABLE')
        self.lock = threading.RLock()
        if self.table_name:
            region = os.getenv('AWS_REGION', 'cn-northwest-1')
            config = Config(connect_timeout=3, read_timeout=5, tcp_keepalive=True, max_pool_connections=32,
                            retries={'mode': 'standard', 'max_attempts': 1})
            self.db = boto3.resource('dynamodb', region_name=region, config=config).Table(self.table_name)
            # Keep a LOW-LEVEL client for serialized transaction AttributeValues;
            # the resource client would serialize them a second time.
            self.client = boto3.client('dynamodb', region_name=region, config=config)
        else:
            self.sql = sqlite3.connect(os.getenv('CLOUDOPS_DB', ':memory:'), check_same_thread=False)
            self.sql.execute('CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, data TEXT)')
            self.sql.execute('PRAGMA busy_timeout=5000')

    def get(self, ident):
        if self.table_name:
            item = self.db.get_item(Key={'id': ident}, ConsistentRead=True).get('Item')
            return json.loads(item['data']) if item else None
        with self.lock:
            row = self.sql.execute('SELECT data FROM records WHERE id=?', (ident,)).fetchone()
            return json.loads(row[0]) if row else None

    def all(self, kind=None):
        if self.table_name:
            items, kw = [], {}
            if kind:
                from boto3.dynamodb.conditions import Key
                kw = {'IndexName': 'kind-index', 'KeyConditionExpression': Key('kind').eq(kind)}
            while True:
                page = self.db.query(**kw) if kind else self.db.scan(**kw)
                items.extend(json.loads(x['data']) for x in page.get('Items', []))
                if not page.get('LastEvaluatedKey'):
                    break
                kw['ExclusiveStartKey'] = page['LastEvaluatedKey']
        else:
            with self.lock:
                items = [json.loads(r[0]) for r in self.sql.execute('SELECT data FROM records')]
        return sorted((x for x in items if kind is None or x['kind'] == kind), key=lambda x: x.get('created_at', 0))

    def put(self, record, expected=None):
        r = dict(record)
        r.setdefault('created_at', time.time())
        r['updated_at'] = time.time()
        r['version'] = (expected or 0) + 1
        if self.table_name:
            kw = {'Item': self.item(r)}
            if expected is None:
                kw['ConditionExpression'] = 'attribute_not_exists(id)'
            else:
                kw.update(ConditionExpression='#v=:v', ExpressionAttributeNames={'#v': 'version'}, ExpressionAttributeValues={':v': expected})
            try:
                self.db.put_item(**kw)
            except ClientError as e:
                if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                    raise Conflict() from e
                raise
        else:
            with self.lock:
                old = self.get(r['id'])
                if (expected is None and old) or (expected is not None and (not old or old['version'] != expected)):
                    raise Conflict()
                self.sql.execute('INSERT OR REPLACE INTO records VALUES (?,?)', (r['id'], json.dumps(r)))
                self.sql.commit()
        return r

    @staticmethod
    def item(r):
        item = {'id': r['id'], 'kind': r['kind'], 'version': r['version'],
                'scope': r.get('conversation_id', r.get('customer_id', r['kind'])),
                'order': f"{r.get('sequence', 0):020d}#{r.get('created_at', 0):020.6f}#{r['id']}",
                'data': json.dumps(r, ensure_ascii=False)}
        if r.get('expires_at'):
            item['expires_at'] = int(r['expires_at'])
        if active_kind(r):
            item['active'] = active_kind(r)
        if r['kind']=='task' and not r.get('parent_id'):
            item['timeline_scope'] = 'user#' + r.get('user_id', 'admin')
            item['timeline_order'] = f"{r.get('created_at', 0):020.6f}#{r['id']}"
            item['summary'] = json.dumps(Store.task_summary(r), ensure_ascii=False)
        return item

    @staticmethod
    def task_summary(r):
        return {**{k:r[k] for k in ('id','owner','status','created_at','started_at','finished_at','trace_id',
                                  'conversation_id','attempts','explicit_owners','routine_id') if k in r},
                'title': r.get('prompt','')[:160],
                'experts': sorted(set(r.get('explicit_owners', []) + [p.get('agent','') for p in r.get('plan',[]) if isinstance(p,dict)]))}

    def task_page(self, user_id, since, cursor=None, status='', expert='', limit=20):
        """Bounded summary-only GSI reads; no full task/conversation scan in the UI."""
        import base64
        def matches(x):
            return (not status or x['status']==status) and (not expert or expert==x['owner'] or expert in x['experts'])
        key = None
        if cursor:
            try:
                key = json.loads(base64.urlsafe_b64decode(cursor))
                if key.get('timeline_scope') != 'user#'+user_id:
                    raise ValueError()
            except Exception:
                raise ValueError('分页游标无效') from None
        if not self.table_name:
            rows = [self.task_summary(x) for x in reversed(self.all('task')) if not x.get('parent_id')
                    and x.get('user_id','admin')==user_id and x['created_at']>=since]
            if key:
                rows = [x for x in rows if f"{x['created_at']:020.6f}#{x['id']}" < key['timeline_order']]
            rows = [x for x in rows if matches(x)]
            page = rows[:limit]
            key = {'timeline_scope':'user#'+user_id,'timeline_order':f"{page[-1]['created_at']:020.6f}#{page[-1]['id']}"} if len(rows)>limit else None
        else:
            from boto3.dynamodb.conditions import Key
            page = []
            for _ in range(5):
                args = {'IndexName':'task-timeline-index',
                        'KeyConditionExpression':Key('timeline_scope').eq('user#'+user_id) & Key('timeline_order').gte(f'{since:020.6f}#'),
                        'ProjectionExpression':'summary', 'ScanIndexForward':False, 'Limit':limit-len(page)}
                if key: args['ExclusiveStartKey']=key
                result=self.db.query(**args)
                page.extend(x for item in result.get('Items',[]) if matches(x:=json.loads(item['summary'])))
                key=result.get('LastEvaluatedKey')
                if not key or len(page)>=limit: break
        return {'items':page, 'cursor':base64.urlsafe_b64encode(json.dumps(key).encode()).decode() if key else None}

    def active(self, kind):
        """Unfinished records of one kind (task/outbox/conversation_title), via the sparse index."""
        if not self.table_name:
            return [x for x in self.all(kind) if active_kind(x) == kind]
        from boto3.dynamodb.conditions import Key
        items, kw = [], {'IndexName': ACTIVE_INDEX, 'KeyConditionExpression': Key('active').eq(kind)}
        while True:
            page = self.db.query(**kw)
            items.extend(json.loads(x['data']) for x in page.get('Items', []))
            if not page.get('LastEvaluatedKey'):
                break
            kw['ExclusiveStartKey'] = page['LastEvaluatedKey']
        # GSI reads are eventually consistent; callers re-validate with get()/conditional writes.
        return sorted(items, key=lambda x: x.get('created_at', 0))

    def scoped(self, kind, conversation_id):
        if not self.table_name:
            return [x for x in self.all(kind) if x.get('conversation_id') == conversation_id]
        from boto3.dynamodb.conditions import Key
        result, kw = [], {'IndexName': 'scope-index', 'KeyConditionExpression': Key('scope').eq(conversation_id)}
        while True:
            page = self.db.query(**kw)
            result.extend(json.loads(x['data']) for x in page['Items'] if x['kind'] == kind)
            if not page.get('LastEvaluatedKey'):
                break
            kw['ExclusiveStartKey'] = page['LastEvaluatedKey']
        return sorted(result, key=lambda x: (x.get('sequence', 0), x.get('created_at', 0), x['id']))

    def atomic(self, records):
        """List of (record, expected_version); None means create, all or none."""
        if self.table_name:
            from boto3.dynamodb.types import TypeSerializer
            ser = TypeSerializer()
            ops = []
            for r, expected in records:
                put = {'TableName': self.table_name, 'Item': {k: ser.serialize(v) for k, v in self.item(r).items()},
                       'ConditionExpression': 'attribute_not_exists(id)' if expected is None else '#v=:v'}
                if expected is not None:
                    put.update(ExpressionAttributeNames={'#v': 'version'}, ExpressionAttributeValues={':v': {'N': str(expected)}})
                ops.append({'Put': put})
            try:
                self.client.transact_write_items(TransactItems=ops)
            except ClientError as e:
                if e.response['Error']['Code'] in ('TransactionCanceledException', 'ConditionalCheckFailedException'):
                    raise Conflict() from e
                raise
        else:
            with self.lock:
                for r, expected in records:
                    old = self.get(r['id'])
                    if (expected is None and old) or (expected is not None and (not old or old['version'] != expected)):
                        raise Conflict()
                try:
                    self.sql.executemany('INSERT OR REPLACE INTO records VALUES (?,?)', [(r['id'], json.dumps(r)) for r, _ in records])
                    self.sql.commit()
                except Exception:
                    self.sql.rollback()
                    raise

    def update(self, ident, fn):
        for _ in range(8):
            old = self.get(ident)
            if not old:
                raise KeyError(ident)
            new = fn(dict(old))
            try:
                return self.put(new, old['version'])
            except Conflict:
                continue
        raise Conflict()

    def create_task(self, task, commit=None):
        """Task and durable notification are committed together."""
        task = {**task, 'version': 1, 'created_at': time.time(), 'updated_at': time.time()}
        out = {'id': 'out-' + task['id'], 'kind': 'outbox', 'task_id': task['id'], 'sent': False,
               'version': 1, 'created_at': time.time(), 'expires_at': int(time.time() + OUTBOX_RETENTION_SECONDS)}
        self.atomic([(task, None), (out, None)] + (commit or []))
        return task

    def event(self, task, text, agent=None, event_type='task.event', delta=''):
        for _ in range(20):
            c = self.get(task['conversation_id'])
            if not c:
                raise ValueError('会话不存在')
            seq = c.get('event_sequence', 0) + 1
            e = {'id': f"event-{c['id']}-{seq}", 'kind': 'event', 'task_id': task['id'],
                 'conversation_id': c['id'], 'agent': agent or task['owner'], 'text': text,
                   'sequence': seq, 'created_at': time.time(), 'version': 1}
            # Every event expires; scheduled runs share their conversation's expiry.
            e['expires_at'] = int(c['expires_at']) if c.get('scheduled') and c.get('expires_at') else int(time.time() + HISTORY_RETENTION_SECONDS)
            if event_type != 'task.event':
                e.update(event_type=event_type, delta=delta)
            new = {**c, 'event_sequence': seq, 'version': c['version'] + 1, 'updated_at': time.time()}
            try:
                self.atomic([(new, c['version']), (e, None)])
                return e
            except Conflict:
                continue
        raise Conflict()

    def events_after(self, conversation_id, sequence, limit=100):
        # Strong reads by deterministic IDs avoid GSI lag causing a skipped event.
        c = self.get(conversation_id)
        end = min(c.get('event_sequence', 0), sequence + limit)
        return [e for n in range(sequence + 1, end + 1)
                if (e := self.get(f'event-{conversation_id}-{n}'))]


store = Store()
