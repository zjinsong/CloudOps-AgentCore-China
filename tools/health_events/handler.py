"""Read the existing EventBridge event archive from a read-only MCP Runtime."""
def list_health_events(args):
    from control.store import store
    events = [x['event'] for x in store.all('cloud_event') if x.get('source') == 'aws.health'][-40:]
    return {'source': 'EventBridge captured events', 'events': events,
            'coverage': '仅接入后收到的账户事件；未收到事件不代表账户健康'}

_DISPATCH = {'list_health_events': list_health_events}
