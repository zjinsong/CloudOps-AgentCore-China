"""Scheduled run bodies in the non-versioned 90-day chat bucket."""
import json
from control import messages


def write(task, body):
    if not messages.bucket():
        return {'result': body, 'result_summary': body[:180]}
    key = f"users/{task['user_id']}/agents/{task['owner']}/routines/{task['routine_id']}/runs/{task['id']}/result.json"
    messages.s3().put_object(Bucket=messages.bucket(), Key=key, Body=json.dumps({'result': body}, ensure_ascii=False).encode(),
                           ContentType='application/json', ServerSideEncryption='AES256')
    return {'result_ref': {'bucket': messages.bucket(), 'key': key}, 'result_summary': body[:180]}


def read(record):
    if record.get('result_ref'):
        ref = record['result_ref']
        raw = messages.s3().get_object(Bucket=ref['bucket'], Key=ref['key'])['Body'].read()
        return json.loads(raw)['result']
    return record.get('result', '')
