"""EventBridge ingestion and Gateway reads use distinct invocation contracts."""
import os
from control.store import store,Conflict,HISTORY_RETENTION_SECONDS
from tools.health_events.handler import list_health_events

def handler(event,context):
    if getattr(context,'client_context',None):
        name=context.client_context.custom['bedrockAgentCoreToolName'].split('___',1)[-1]
        if name!='list_health_events':raise PermissionError('Unsupported read-only tool')
        return list_health_events(event)
    if event.get('source')!='aws.health' or event.get('account')!=os.environ['CLOUDOPS_ACCOUNT']:
        raise PermissionError('Only this account Health events are accepted')
    import time
    try:
        store.put({'id':'health-'+event['id'],'kind':'cloud_event','source':'aws.health','event':event,
                   'expires_at':int(time.time()+HISTORY_RETENTION_SECONDS)})
    except Conflict:pass
    return {'archived':True}
