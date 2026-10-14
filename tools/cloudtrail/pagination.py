"""Bounded auto-pagination around the official CloudTrail MCP (read-only)."""
import json
import time
from collections import Counter
from datetime import datetime, timezone, timedelta

NOISE_EVENTS={'AssumeRole','CreateLogStream'}


def routine_agentcore_event(event):
    name=event.get('EventName',event.get('eventName'))
    if name not in NOISE_EVENTS:return False
    try:raw=json.loads(event.get('CloudTrailEvent','{}'))
    except (ValueError,TypeError):return False
    identity=raw.get('userIdentity') or {}
    invoked=identity.get('invokedBy','')
    if identity.get('type')=='AWSService' and 'bedrock-agentcore' in invoked:return True
    if name=='CreateLogStream':
        issuer=identity.get('sessionContext',{}).get('sessionIssuer',{}).get('userName','')
        group=(raw.get('requestParameters') or {}).get('logGroupName','')
        return issuer.startswith('agentcore-demo-cloudops-') and group.startswith('/aws/bedrock-agentcore/runtimes/')
    # Human or unfamiliar AssumeRole calls remain visible, even for an AgentCore role.
    return False


def lookup(arguments, fetch):
    args=dict(arguments);names=args.pop('event_names',None);include_noise=args.pop('include_noise',False)
    max_pages=min(24,max(1,int(args.pop('max_pages',24))))
    max_events=min(200,max(1,int(args.pop('max_events',100))))
    if names is not None and (not isinstance(names,list) or not 1<=len(names)<=16 or
                              any(not isinstance(n,str) or not n or len(n)>100 for n in names)):
        raise ValueError('event_names requires 1–16 event names')
    if names and (args.get('attribute_key') or args.get('attribute_value') or args.get('next_token')):
        raise ValueError('event_names cannot be combined with an attribute filter or next_token')
    if args.get('next_token') and not (args.get('start_time') and args.get('end_time')):
        raise ValueError('Continuing a page requires its original start_time and end_time')
    now=datetime.now(timezone.utc)
    if not args.get('end_time'):args['end_time']=now.isoformat()
    if not args.get('start_time'):args['start_time']=(now-timedelta(hours=24)).isoformat()
    args.setdefault('max_results',50)
    args.setdefault('region','cn-northwest-1')
    if args['region'] not in ('cn-northwest-1','cn-north-1'):raise ValueError('Only China Regions are allowed')
    filters=[{**args,'attribute_key':'EventName','attribute_value':n} for n in dict.fromkeys(names)] if names else [args]
    deadline=time.monotonic()+24; pages=0; scanned=0; matched=0; rows=[]; scopes=[];noise=Counter();seen=set();size=0;last_call=0
    for original in filters:
        current=dict(original); token=current.get('next_token');complete=False;reason=''; count=0;scope_pages=0
        while pages<max_pages:
            if last_call:
                wait=max(0,.55-(time.monotonic()-last_call))
                if wait:time.sleep(wait)
            remaining=deadline-time.monotonic()
            if remaining<1:
                reason='time_budget';break
            try:
                last_call=time.monotonic(); payload=fetch(current,remaining)
            except Exception as exc:
                if not pages:raise
                reason='query_error:'+type(exc).__name__;break
            pages+=1;scope_pages+=1
            events=payload.get('events',[])
            if not isinstance(events,list):raise ValueError('CloudTrail MCP returned an invalid events list')
            for event in events:
                scanned+=1
                if not include_noise and original.get('attribute_value') not in NOISE_EVENTS and routine_agentcore_event(event):
                    noise[event.get('EventName','unknown')]+=1;continue
                count+=1
                ident=event.get('EventId')
                if ident and ident in seen:continue
                if ident:seen.add(ident)
                matched+=1
                bytes_count=len(json.dumps(event,default=str).encode())
                if len(rows)<max_events and size+bytes_count<=96000:
                    rows.append(event);size+=bytes_count
            next_token=payload.get('next_token')
            # Pin the official MCP's actual query parameters for continuation.
            returned=payload.get('query_params') or {}
            current={**current,**{k:v for k,v in returned.items() if k in
                ('start_time','end_time','attribute_key','attribute_value','max_results','region') and v is not None}}
            if not next_token:
                complete=True;token=None;break
            if next_token==token:
                reason='repeated_token';token=next_token;break
            token=next_token;current['next_token']=token
        if not complete and not reason:reason='page_budget'
        query_params={k:v for k,v in current.items() if k!='next_token'}
        scopes.append({'query_params':query_params,'next_token':token,'complete':complete,'pages':scope_pages,
                       'matching_events':count,'stopped_reason':reason})
    complete=all(s['complete'] for s in scopes)
    omitted=max(0,matched-len(rows))
    return {'events':rows,'complete':complete,'status':'success' if complete else 'partial',
        'next_token':scopes[0]['next_token'] if len(scopes)==1 else None,
        'query_params':scopes[0]['query_params'] if len(scopes)==1 else None,'queries':scopes,
        'pages_fetched':pages,'scanned_events':scanned,'matching_events':matched,
        'noise_removed':dict(noise),'omitted_matching_events':omitted,'output_truncated':omitted>0,
        'coverage':'Only management event history in '+args['region']+'; not S3 object/data events or other Regions. IAM/global-service audits must also check cn-north-1.',
        'note':('All requested filters exhausted.' if complete else 'Incomplete: continue each unfinished query with its unchanged query_params and next_token; do not infer absence.')+
               (' Some matching events omitted from output; narrow filters/time to retrieve details.' if omitted else '')}
