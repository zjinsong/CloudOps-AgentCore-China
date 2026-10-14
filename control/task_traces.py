"""Task trace detail: durable lifecycle + real ADOT spans, fetched only on demand."""
import json
import os
import re
import time
import boto3
from botocore.config import Config

_cache = {}


def spans(task):
    trace_id = task.get('trace_id','')
    if not re.fullmatch('[a-f0-9]{32}', trace_id):
        return [], '旧任务未接入 SDK 追踪'
    cached = _cache.get(trace_id)
    if cached and time.monotonic()-cached[0]<10:
        return cached[1], cached[2]
    client=boto3.client('logs',region_name=os.getenv('AWS_REGION','cn-northwest-1'),
                       config=Config(connect_timeout=3,read_timeout=8,retries={'max_attempts':1}))
    result=[]; token=None; note=''; seen=set(); truncated=False
    try:
        for page_number in range(5):
            args={'logGroupName':os.getenv('CLOUDOPS_TRACE_LOG_GROUP','aws/spans'),
                  'filterPattern':'"'+trace_id+'"', 'startTime':int((task['created_at']-60)*1000),
                  'endTime':int((task.get('finished_at') or time.time()+1)*1000)+60000, 'limit':200}
            if token:args['nextToken']=token
            page=client.filter_log_events(**args)
            for event in page.get('events',[]):
                raw=json.loads(event['message'])
                if raw.get('traceId') != trace_id or raw.get('spanId') in seen:continue
                attributes=raw.get('attributes',{})
                if not isinstance(attributes,dict):continue
                # Only our metadata spans reach the browser. Framework-generated model content stays out.
                if not attributes.get('cloudops.task.id'):continue
                seen.add(raw['spanId'])
                start=float(raw['startTimeUnixNano'])/1e9; end=float(raw['endTimeUnixNano'])/1e9
                result.append({'id':raw['spanId'],'parent_id':raw.get('parentSpanId'),'name':raw.get('name','步骤'),
                    'start':start,'end':end,'owner':attributes.get('cloudops.owner',''),
                    'phase':attributes.get('cloudops.phase'),'attempt':attributes.get('cloudops.attempt'),
                    'error_type':attributes.get('error.type'), 'status':raw.get('status',{}).get('code','UNSET'), 'source':'ADOT SDK'})
            next_token=page.get('nextToken')
            if not next_token or next_token==token:break
            token=next_token
            truncated=page_number==4
        if truncated: note='调用链较长，仅显示前 1000 个日志事件'
        if not result:note='追踪正在上报；可稍后刷新。'
    except Exception as exc:
        note='追踪读取失败（'+type(exc).__name__+'）；任务记录仍可查看。'
    if len(_cache)>=128:_cache.pop(next(iter(_cache)))
    _cache[trace_id]=(time.monotonic(),result,note)
    return sorted(result,key=lambda x:x['start']), note


def detail(task, db):
    records=[task]+[r for ident in task.get('children',[]) if (r:=db.get(ident)) and r.get('user_id','admin')==task.get('user_id','admin')]
    now=time.time(); lifecycle=[]
    for r in records:
        begin=r.get('started_at'); end=r.get('finished_at') or now
        lifecycle.append({'id':r['id']+'-queue','name':'排队','owner':r['owner'], 'start':r['created_at'],
                          'end':begin or end,'source':'任务记录','status':r['status']})
        if begin:
            lifecycle.append({'id':r['id'],'name':'任务执行','owner':r['owner'],'start':begin,'end':end,
                              'source':'任务记录','status':r['status'],'attempt':r.get('attempts',0)})
    traces,note=spans(task)
    return {'task':db.task_summary(task),'lifecycle':lifecycle,'spans':traces,'note':note,'generated_at':now}
