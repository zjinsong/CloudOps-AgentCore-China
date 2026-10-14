"""Keep saved tool evidence valid JSON, preserving CloudTrail coverage metadata."""
import json


def bounded_tool_content(content, byte_limit):
    original=json.dumps(content,ensure_ascii=False,default=str)
    if len(original.encode())<=byte_limit:return original,False
    compact=[]
    for part in content:
        if not isinstance(part,dict):continue
        text=part.get('text')
        if not isinstance(text,str):continue
        try:data=json.loads(text)
        except ValueError:data=None
        if isinstance(data,dict) and 'queries' in data and 'complete' in data and 'events' in data:
            metadata={k:v for k,v in data.items() if k!='events'}
            metadata['events_preview']=[{k:e[k] for k in ('EventId','EventTime','EventName','EventSource','Username') if k in e}
                                        for e in data['events'][:10]]
            metadata['saved_event_details_truncated']=True
            compact.append({'text':json.dumps(metadata,ensure_ascii=False,default=str)})
        else:
            compact.append({'text':text[:max(100,byte_limit//8)],'saved_text_truncated':True})
    packed=json.dumps(compact,ensure_ascii=False,default=str)
    if len(packed.encode())<=byte_limit:return packed,True
    return json.dumps([{'text':'工具证据超过保存上限；完整结果用于本次模型回答，历史证据仅保留截断标记。',
                       'saved_content_truncated':True}],ensure_ascii=False),True
