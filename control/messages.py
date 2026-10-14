"""Chat bodies in short-lived S3; DynamoDB stores only message metadata."""
import json
import os
import time
import uuid
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config
from control.store import store, HISTORY_RETENTION_SECONDS

RETENTION_SECONDS = HISTORY_RETENTION_SECONDS


def bucket():
    return os.getenv('CLOUDOPS_CHAT_BUCKET', '')


@lru_cache(maxsize=4)
def _s3(region):
    return boto3.client('s3', region_name=region, config=Config(connect_timeout=3, read_timeout=10,
                        tcp_keepalive=True, max_pool_connections=32, retries={'mode': 'standard', 'max_attempts': 1}))


def s3():
    return _s3(os.getenv('AWS_REGION', 'cn-northwest-1'))


@lru_cache(maxsize=512)
def _body(bucket_name, key, version):
    params = {'Bucket': bucket_name, 'Key': key}
    if version:
        params['VersionId'] = version
    body = s3().get_object(**params)['Body']
    try:
        return json.loads(body.read()).get('text', '')
    finally:
        body.close()


def create(conversation, role, body, task_id='', agent='', file_ids=None, ident=None, explicit_owners=None):
    ident = ident or 'msg-' + uuid.uuid4().hex
    record = {'id': ident, 'kind': 'message', 'conversation_id': conversation['id'],
              'user_id': conversation['user_id'], 'role': role, 'task_id': task_id,
              'agent': agent, 'file_ids': file_ids or [], 'expires_at': int(time.time() + RETENTION_SECONDS)}
    if explicit_owners:
        record['explicit_owners'] = list(explicit_owners)
    if bucket():
        key = f"users/{conversation['user_id']}/agents/{conversation['owner']}/conversations/{conversation['id']}/messages/{ident}.json"
        s3().put_object(Bucket=bucket(), Key=key,
                        Body=json.dumps({'text': body}, ensure_ascii=False).encode(),
                        ContentType='application/json; charset=utf-8', ServerSideEncryption='AES256')
        record['text_ref'] = {'bucket': bucket(), 'key': key}
    else:
        record['text'] = body  # Offline tests only.
    saved = store.put(record)
    # Lets the conversation list tell non-empty chats apart without scanning every message.
    if not conversation.get('has_content'):
        try:
            store.update(conversation['id'], lambda x: {**x, 'has_content': True})
        except KeyError:
            pass
    return saved


def text(record):
    if 'text' in record:
        return record.get('text', '')
    ref = record.get('text_ref') or {}
    if not ref:
        return ''
    return _body(ref['bucket'], ref['key'], ref.get('version'))


def public(record):
    return {k: v for k, v in record.items() if k not in ('text_ref', 'text')} | {'text': text(record)}


def list_for(conversation_id, limit=200, before=None):
    values = store.scoped('message', conversation_id)
    if before:
        values = [x for x in values if x.get('created_at', 0) < before]
    selected = values[-limit:]
    if len(selected) < 2:
        return [public(x) for x in selected]
    with ThreadPoolExecutor(max_workers=min(8, len(selected))) as pool:
        return list(pool.map(public, selected))


def migrate_inline(record, conversation):
    if not bucket() or 'text' not in record:
        return record
    key = f"users/{conversation['user_id']}/agents/{conversation['owner']}/conversations/{conversation['id']}/messages/{record['id']}.json"
    s3().put_object(Bucket=bucket(), Key=key,
                    Body=json.dumps({'text': record.get('text', '')}, ensure_ascii=False).encode(),
                    ContentType='application/json; charset=utf-8', ServerSideEncryption='AES256')
    return store.update(record['id'], lambda x: {k: v for k, v in {**x, 'user_id': conversation['user_id'],
        'text_ref': {'bucket': bucket(), 'key': key}, 'expires_at': int(time.time() + RETENTION_SECONDS)}.items() if k != 'text'})
