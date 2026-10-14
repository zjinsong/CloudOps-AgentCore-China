"""One application protocol for cloud Runtime WS and authenticated local tests."""
import asyncio
import json
import time
import uuid

from starlette.websockets import WebSocketDisconnect
from control import domain
from control.store import store
from control.transcribe import Recognition


async def send_wire(ws, value):
    raw = json.dumps(value, ensure_ascii=False)
    if len(raw.encode()) < 28000:
        await ws.send_text(raw)
    else:
        ident = uuid.uuid4().hex
        parts = [raw[i:i+3500] for i in range(0, len(raw), 3500)]
        for index, data in enumerate(parts):
            await ws.send_json({'type': 'wire.chunk', 'id': ident, 'index': index, 'total': len(parts), 'data': data})


def snapshot(ident, user_id):
    from control.api import conversation_data
    return conversation_data(ident, user_id)


def dispatch(ident, msg, user_id):
    from control.api import Message, send_message, TaskAction
    typ = msg.get('type')
    if typ == 'message.submit':
        body = Message(text=msg.get('text', ''), owners=msg.get('owners', []), file_ids=msg.get('file_ids'), request_id=msg.get('message_id', ''))
        t = send_message(ident, body, user_id)
        return {'type': 'message.ack', 'message_id': body.request_id, 'task': t}
    if typ == 'task.action':
        t = store.get(msg.get('task_id', ''))
        if not t or t.get('conversation_id') != ident or t.get('user_id', 'admin') != user_id:
            raise PermissionError('任务不属于当前会话')
        body = TaskAction(status=msg.get('status', ''), supplement=msg.get('supplement', ''))
        if body.supplement:
            if t['status'] not in ('paused', 'waiting_for_input'):
                raise ValueError('请先暂停任务再补充信息')
            store.update(t['id'], lambda x: {**x, 'prompt': x['prompt'] + '\n用户补充：' + body.supplement})
        return {'type': 'task.updated', 'task': domain.transition(t['id'], body.status)}
    if typ == 'sync':
        return {'type': 'snapshot', **snapshot(ident, user_id)}
    if typ == 'ping':
        return {'type': 'pong'}
    raise ValueError('未知消息类型')


async def serve(ws, ident, user_id, authorized=lambda: True):
    domain.require_conversation(ident, user_id)
    lock = asyncio.Lock()
    async def emit(value):
        async with lock:
            await send_wire(ws, value)
    speech = Recognition(emit)
    initial = snapshot(ident, user_id)
    # Initial snapshot already carries durable state; don't replay old deltas.
    cursor = initial['conversation'].get('event_sequence', 0)
    await emit({'type': 'session.ready', 'conversation_id': ident, 'protocol': 1})
    await emit({'type': 'snapshot', **initial})

    async def updates():
        nonlocal cursor
        while True:
            if not authorized():
                await ws.close(code=1008)
                return
            events = await asyncio.to_thread(store.events_after, ident, cursor)
            for e in events:
                await emit({'type': e.get('event_type', 'task.event'), 'event': e})
                cursor = e['sequence']
            if events:
                await emit({'type': 'snapshot', **(await asyncio.to_thread(snapshot, ident, user_id))})
            await asyncio.sleep(0.5)

    pump = asyncio.create_task(updates())
    try:
        while True:
            data = await ws.receive()
            if data['type'] == 'websocket.disconnect':
                break
            try:
                if not authorized():
                    await ws.close(code=1008)
                    break
                if data.get('bytes') is not None:
                    await speech.push(data['bytes'])
                    continue
                raw = data.get('text', '')
                if len(raw.encode()) > 28000:
                    raise ValueError('消息过长')
                msg = json.loads(raw)
                if not isinstance(msg, dict):
                    raise ValueError('消息必须是对象')
                if msg.get('type') == 'audio.start':
                    try:
                        await speech.start(msg.get('language_code', 'zh-CN'))
                    except Exception as e:
                        await emit({'type': 'error', 'code': 'transcribe', 'message': f'语音连接失败（{type(e).__name__}），请重试或更换语言'})
                elif msg.get('type') in ('audio.end', 'audio.cancel'):
                    await speech.finish(cancel=msg['type'] == 'audio.cancel')
                else:
                    await emit(await asyncio.to_thread(dispatch, ident, msg, user_id))
                    if msg.get('type') not in ('ping', 'sync'):
                        await emit({'type': 'snapshot', **(await asyncio.to_thread(snapshot, ident, user_id))})
            except (ValueError, PermissionError) as e:
                await emit({'type': 'error', 'message': str(e)})
            except Exception as e:
                await emit({'type': 'error', 'message': f'请求失败（{type(e).__name__}），可重试同一消息'})
    except WebSocketDisconnect:
        pass
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        try:
            await speech.finish(cancel=True)
        except (WebSocketDisconnect, RuntimeError):
            pass
