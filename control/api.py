import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta

import boto3
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Response, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from control import domain, files, registry, messages, schedules, results
from control.security import valid_session, session_token, session_user, authenticate
from control.store import store, uid, Conflict, HISTORY_RETENTION_SECONDS

app = FastAPI(title='CloudOps Control', docs_url=None, redoc_url=None)


@app.middleware('http')
async def authorize(request: Request, call_next):
    if request.url.path not in ('/api/login', '/ping'):
        user_id = session_user(request.cookies.get('cloudops_session', ''))
        if not user_id:
            return Response('需要登录', status_code=401)
        request.state.user_id = user_id
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.headers.get('X-CloudOps-CSRF') != '1':
            return Response('缺少请求校验', status_code=403)
        origin = request.headers.get('origin')
        if origin and origin.split('://')[-1] != request.headers.get('host'):
            return Response('来源不匹配', status_code=403)
    try:
        response = await call_next(request)
    except PermissionError:
        return Response('未授权', status_code=403)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.exception_handler(ValueError)
async def bad_request(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse({'detail': str(exc)}, status_code=400)


@app.exception_handler(PermissionError)
async def forbidden(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse({'detail': str(exc)}, status_code=403)


@app.exception_handler(Conflict)
async def conflict(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse({'detail': '记录已变化，请刷新后重试'}, status_code=409)


@app.on_event('startup')
def startup():
    from control.security import validate_auth_config
    validate_auth_config()
    domain.seed()


@app.get('/ping')
def ping():
    return {'status': 'healthy', 'service': 'cloudops-control'}


_login_attempts = {}


@app.post('/api/login')
async def login(request: Request, response: Response):
    ip = request.client.host
    recent = [t for t in _login_attempts.get(ip, []) if t > time.time() - 60]
    if len(recent) >= 10:
        raise HTTPException(429, '请稍后再试')
    _login_attempts[ip] = recent + [time.time()]
    body = await request.json()
    user_id = authenticate(body.get('username', 'admin'), body.get('password', ''))
    if not user_id:
        raise HTTPException(401, '密码不正确')
    response.set_cookie('cloudops_session', session_token(user_id), httponly=True, samesite='strict', max_age=43200,
                        secure=os.getenv('COOKIE_SECURE') == 'true')
    return {'user': user_id}


@app.post('/api/logout')
def logout(response: Response):
    response.delete_cookie('cloudops_session')
    return {'ok': True}


@app.get('/api/bootstrap')
def bootstrap(request: Request):
    user_id = request.state.user_id
    agents = registry.active()
    owners = {a['name'] for a in agents}
    conversations = [c for c in store.all('conversation') if c.get('user_id', 'admin') == user_id and not c.get('deleted_at') and not c.get('archived') and not c.get('group') and not c.get('scheduled') and c['owner'] in owners]
    conversations = sorted(conversations, key=lambda x: x.get('last_active_at', x.get('updated_at', 0)), reverse=True)
    preferences = (store.get('preferences-' + user_id) or {}).get('active', {})
    active = {}
    for owner in owners:
        candidates = [c for c in conversations if c['owner'] == owner]
        chosen = next((c for c in candidates if c['id'] == preferences.get(owner)), candidates[0] if candidates else None)
        if chosen:
            active[owner] = chosen['id']
            conversations.remove(chosen)
            conversations.insert(0, chosen)
    counts = {}
    recent = []
    # Avoid scanning every message and task on each 15 s refresh: a conversation is non-empty once it
    # has a message (has_content) or an event; active tasks come from the sparse active index.
    populated = {c['id'] for c in conversations if c.get('has_content') or c.get('event_sequence', 0) > 0}
    for conversation in conversations:
        owner = conversation['owner']
        if conversation['id'] != active.get(owner) and conversation['id'] not in populated:
            continue  # Empty drafts must not fill the ten history slots.
        # Keep the current conversation plus up to ten entries in its history.
        if counts.get(owner, 0) < 11:
            recent.append(conversation)
            counts[owner] = counts.get(owner, 0) + 1
    conversations = recent
    conversation_ids = {c['id'] for c in conversations}
    tasks = [t for t in store.active('task') if t.get('user_id', 'admin') == user_id and t.get('conversation_id') in conversation_ids]
    return {'user': user_id, 'agents': agents, 'conversations': conversations, 'active_conversations': active,
            'tasks': tasks[-300:], 'routines': [r for r in store.all('routine') if r.get('user_id', 'admin') == user_id and r.get('owner') in owners and not r.get('deleted_at')],
            'approvals': [a for a in store.all('approval') if a.get('user_id', 'admin') == user_id][-100:],
            'projects': [p for p in store.all('terraform') if p.get('user_id', 'admin') == user_id][-100:]}



class NewConversation(BaseModel):
    owner: str = 'supervisor'
    previous_id: str | None = None


@app.post('/api/conversations')
def create_conversation(body: NewConversation, request: Request):
    return domain.create_conversation(request.state.user_id, body.owner, body.previous_id)


@app.post('/api/conversations/{ident}/activate')
def activate_conversation(ident: str, request: Request):
    c = domain.require_conversation(ident, request.state.user_id)
    if c.get('scheduled'):
        raise ValueError('定时运行通过执行历史查看')
    domain.activate(c, request.state.user_id)
    return store.get(ident)


class Rename(BaseModel):
    title: str = Field(min_length=1, max_length=60)


@app.patch('/api/conversations/{ident}')
def rename_conversation(ident: str, body: Rename, request: Request):
    domain.require_conversation(ident, request.state.user_id)
    if not body.title.strip():
        raise ValueError('名称不能为空')
    return store.update(ident, lambda x: {**x, 'title': body.title.strip(), 'title_manual': True})


@app.delete('/api/conversations/{ident}')
def delete_conversation(ident: str, request: Request):
    c = domain.require_conversation(ident, request.state.user_id)
    if c.get('scheduled'):
        raise ValueError('定时执行记录按保留期清理')
    store.update(ident, lambda x: {**x, 'deleted_at': time.time()})
    preferences = 'preferences-' + request.state.user_id
    if store.get(preferences):
        store.update(preferences, lambda x: {**x, 'active': {owner: value for owner, value in x.get('active', {}).items() if value != ident}})
    return {'ok': True}


def conversation_data(ident, user_id):
    c = domain.require_conversation(ident, user_id)
    all_tasks = store.scoped('task', ident)
    tasks = all_tasks[-100:]
    assignments = {t['id']: t.get('explicit_owners', []) for t in all_tasks}
    history = messages.list_for(ident, 200)
    for message in history:
        if message['role'] == 'user' and not message.get('explicit_owners'):
            message['explicit_owners'] = assignments.get(message.get('task_id'), [])
    return {'conversation': c, 'messages': history,
            'events': store.scoped('event', ident)[-200:],
            'files': [f for f in store.scoped('file', ident) if f.get('user_id', 'admin') == user_id],
            'tasks': tasks}


@app.get('/api/conversations/{ident}')
def conversation(ident: str, request: Request):
    return conversation_data(ident, request.state.user_id)


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=16000)
    owner: str | None = None
    owners: list[str] = Field(default_factory=list)
    file_ids: list[str] | None = None
    request_id: str = Field(min_length=8, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')


def send_message(ident, body, user_id):
    c = domain.require_conversation(ident, user_id)
    explicit = list(dict.fromkeys(x.lower() for x in body.owners))
    if c['owner'] == 'supervisor':
        aliases = {'cloudwatch': 'monitoring', '监控专家': 'monitoring', '成本专家': 'finops',
                   '审计专家': 'cloudtrail', '平台专家': 'agentcore', '基础设施专家': 'terraform'}
        for mention in re.findall(r'(?<![\w@])@\s*([a-zA-Z][a-zA-Z0-9_-]*|[\u4e00-\u9fff]+)', body.text):
            name = aliases.get(mention.lower(), mention.lower())
            if name not in explicit:
                explicit.append(name)
    if explicit and c['owner'] != 'supervisor':
        raise ValueError('只有协调员对话可以指定专家')
    allowed = {a['name'] for a in registry.active() if a['name'] != 'supervisor'}
    if any(x not in allowed for x in explicit):
        raise ValueError('包含未注册专家')
    if any(x not in c['members'] for x in explicit):
        raise PermissionError('指定专家未加入当前会话团队')
    # One mention is a direct assignment; several mentions need Supervisor planning.
    owner = explicit[0] if len(explicit) == 1 else ('supervisor' if explicit else (body.owner or c['owner']))
    task_id = 'task-' + ident + '-' + body.request_id
    existing = store.get(task_id)
    # Reconnects may replay a single-mention message created by the old planner route.
    if existing and len(explicit) == 1 and existing['owner'] == 'supervisor' and set(existing.get('explicit_owners', [])) == set(explicit):
        owner = 'supervisor'
    if existing and (existing['prompt'] != body.text or existing['owner'] != owner or
                     set(existing.get('explicit_owners', [])) != set(explicit)):
        raise ValueError('消息ID已被不同内容使用')
    try:
        t = existing or domain.task(c, body.text, owner, files=body.file_ids or [], ident=task_id,
                                   metadata={'explicit_owners': explicit})
    except Conflict:
        t = store.get(task_id)
        if not t or t['prompt'] != body.text or t['owner'] != owner or set(t.get('explicit_owners', [])) != set(explicit):
            raise ValueError('消息冲突')
    try:
        messages.create(c, 'user', body.text, task_id=t['id'], file_ids=t['file_ids'], ident='msg-' + task_id,
                        explicit_owners=explicit)
    except Conflict:
        pass
    store.update(ident, lambda x: {**x, 'title': domain.title(body.text), 'last_active_at': time.time(), 'expires_at': int(time.time() + HISTORY_RETENTION_SECONDS)} if x.get('title') == '新对话' and not x.get('title_manual') else {**x, 'last_active_at': time.time(), 'expires_at': int(time.time() + HISTORY_RETENTION_SECONDS)})
    store.event(t, '任务已登记，将在后台执行。') if not existing else None
    return t


@app.post('/api/conversations/{ident}/messages')
def send(ident: str, body: Message, request: Request):
    return send_message(ident, body, request.state.user_id)


@app.get('/api/tasks/{ident}')
def task_detail(ident: str, request: Request):
    t = store.get(ident)
    if not t or t['kind'] != 'task' or t.get('user_id', 'admin') != request.state.user_id:
        raise HTTPException(404, '任务不存在')
    return {'task': t, 'children': [c for c in (store.get(x) for x in t.get('children', [])) if c],
            'events': [e for e in store.all('event') if e['task_id'] == ident],
            'files': [f for f in store.all('file') if f.get('task_id') == ident]}


class TaskAction(BaseModel):
    status: str
    supplement: str = Field(default='', max_length=8000)


@app.post('/api/tasks/{ident}/action')
def task_action(ident: str, body: TaskAction, request: Request):
    t = store.get(ident)
    if not t or t.get('user_id', 'admin') != request.state.user_id:
        raise PermissionError('任务不属于当前用户')
    if body.status == 'supplement':
        t = store.get(ident)
        if not t or t['status'] in domain.TERMINAL or not body.supplement.strip():
            raise ValueError('只能补充未完成任务')
        store.update(ident, lambda x: {**x, 'supplement': x.get('supplement', '') + '\n' + body.supplement})
        store.event(t, '用户补充：' + body.supplement + '。已开始的调用可能继续；后续步骤与汇总将读取补充内容。')
        return store.get(ident)
    if body.supplement:
        t = store.get(ident)
        if not t or t['status'] not in ('paused', 'waiting_for_input'):
            raise ValueError('请先暂停任务再补充信息')
        store.update(ident, lambda x: {**x, 'prompt': x['prompt'] + '\n用户补充：' + body.supplement})
    return domain.transition(ident, body.status)


@app.get('/api/events')
async def events(request: Request, conversation_id: str, after: int = 0):
    domain.require_conversation(conversation_id, request.state.user_id)
    async def stream():
        cursor = after
        for _ in range(30):
            if await request.is_disconnected():
                return
            current = await asyncio.to_thread(store.events_after, conversation_id, cursor)
            for e in current:
                yield 'id: ' + str(e['sequence']) + '\ndata: ' + json.dumps(e, ensure_ascii=False) + '\n\n'
                cursor = e['sequence']
            yield ': heartbeat\n\n'
            await asyncio.sleep(2)
    return StreamingResponse(stream(), media_type='text/event-stream')


class Upload(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    size: int = Field(gt=0, le=20 * 1024 * 1024)
    content_type: str = Field(default='application/octet-stream', max_length=100)


@app.post('/api/conversations/{ident}/uploads')
def upload(ident: str, body: Upload, request: Request):
    c = domain.require_conversation(ident, request.state.user_id)
    if c.get('archived'):
        raise ValueError('群已归档')
    name = Path(body.name.replace('\\', '/')).name
    fid = uid('file')
    key = f"cloudops/users/{c['user_id']}/conversations/{ident}/inputs/{fid}/{name}"
    f = store.put({'id': fid, 'kind': 'file', 'customer_id': c['customer_id'], 'user_id': c['user_id'], 'conversation_id': ident,
                   'name': name, 'key': key, 'size': body.size, 'status': 'uploading', 'expires_at': int(time.time() + HISTORY_RETENTION_SECONDS)})
    post = files.s3().generate_presigned_post(domain.BUCKET, key,
        Fields={'Content-Type': body.content_type, 'x-amz-server-side-encryption': 'AES256'},
        Conditions=[['content-length-range', 1, 20 * 1024 * 1024], {'Content-Type': body.content_type}, {'x-amz-server-side-encryption': 'AES256'}], ExpiresIn=300)
    return {'file': f, 'upload': post}


@app.post('/api/conversations/{ident}/files/{fid}/complete')
def complete_upload(ident: str, fid: str, request: Request):
    c = domain.require_conversation(ident, request.state.user_id)
    f = domain.permitted_files(c, [fid])[0]
    if f['status'] != 'uploading':
        return f
    head = files.s3().head_object(Bucket=domain.BUCKET, Key=f['key'])
    if head['ContentLength'] != f['size']:
        raise ValueError('文件长度与登记不一致')
    return store.update(fid, lambda x: {**x, 'status': 'pending', 's3_version': head.get('VersionId')})


@app.get('/api/conversations/{ident}/files/{fid}')
def download(ident: str, fid: str, request: Request):
    c = domain.require_conversation(ident, request.state.user_id)
    domain.permitted_files(c, [fid])
    return {'url': f'/api/conversations/{ident}/files/{fid}/content'}


@app.get('/api/conversations/{ident}/files/{fid}/content')
def file_content(ident: str, fid: str, request: Request):
    c = domain.require_conversation(ident, request.state.user_id)
    f = domain.permitted_files(c, [fid])[0]
    params = {'Bucket': domain.BUCKET, 'Key': f['key']}
    if f.get('s3_version'):
        params['VersionId'] = f['s3_version']
    obj = files.s3().get_object(**params)
    def chunks():
        try:
            yield from obj['Body'].iter_chunks(16384)
        finally:
            obj['Body'].close()
    from urllib.parse import quote
    return StreamingResponse(chunks(), media_type='application/octet-stream', headers={
        'Content-Disposition': "attachment; filename*=UTF-8''" + quote(f['name']),
        'Content-Length': str(obj['ContentLength'])})


@app.delete('/api/conversations/{ident}/files/{fid}')
def revoke_file(ident: str, fid: str, request: Request):
    c = domain.require_conversation(ident, request.state.user_id)
    domain.permitted_files(c, [fid])
    return store.update(fid, lambda x: {**x, 'status': 'removed'})


class Routine(BaseModel):
    conversation_id: str | None = None
    name: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=5000)
    owner: str = 'supervisor'
    interval_hours: float = Field(ge=1, le=720, default=24)
    enabled: bool = True
    next_at: float | None = None
    start_date: str | None = None
    time: str = '09:00'
    repeat: str = 'daily'
    timezone: str = 'Asia/Shanghai'


@app.post('/api/schedule-preview')
def schedule_preview(body: dict):
    schedule = schedules.validate(body)
    return {'schedule': schedule, 'next_at': schedules.next_time(schedule, time.time())}


@app.post('/api/routines')
def routine(body: Routine, request: Request):
    if body.conversation_id:
        c = domain.require_conversation(body.conversation_id, request.state.user_id)
        if body.owner not in c['members']:
            raise ValueError('专家未授权给此会话')
    value = body.model_dump()
    if not body.start_date:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        local = datetime.fromtimestamp(body.next_at or time.time() + body.interval_hours * 3600, ZoneInfo(body.timezone))
        value.update(start_date=local.date().isoformat(), time=local.strftime('%H:%M'), repeat='interval')
    return schedules.save(request.state.user_id, value)


@app.patch('/api/routines/{ident}')
def edit_routine(ident: str, body: Routine, request: Request):
    return schedules.save(request.state.user_id, body.model_dump(), ident)


@app.get('/api/routines/{ident}')
def routine_detail(ident: str, request: Request):
    r = schedules.require(ident, request.state.user_id)
    return {**r, 'runs': schedules.history(r)}


@app.delete('/api/routines/{ident}')
def delete_routine(ident: str, request: Request):
    schedules.delete(ident, request.state.user_id)
    return {'ok': True}


@app.get('/api/routines/{ident}/runs/{task_id}')
def routine_run(ident: str, task_id: str, request: Request):
    r = schedules.require(ident, request.state.user_id)
    t = store.get(task_id)
    if not t or t.get('kind') != 'task' or t.get('user_id', 'admin') != request.state.user_id or not (
        t.get('routine_id') == r['id'] or t['id'].startswith('task-' + r['id'] + '-')):
        raise PermissionError('执行记录不属于该定时任务')
    from control import execution
    return {**{k:v for k,v in t.items() if k != 'result_ref'}, 'result': results.read(t),
            'execution': execution.get(t['id']), 'events': store.scoped('event', t['conversation_id'])[-100:],
            'messages': messages.list_for(t['conversation_id'])}


@app.post('/api/routines/{ident}/toggle')
def toggle_routine(ident: str, request: Request):
    r = schedules.require(ident, request.state.user_id)
    upcoming = schedules.next_time(r['schedule'], time.time()) if not r['enabled'] else r.get('next_at')
    if not r['enabled'] and upcoming is None:
        raise ValueError('单次任务已结束，请编辑新的执行日期')
    return store.update(ident, lambda x: {**x, 'enabled': not x['enabled'], 'next_at': upcoming,
                                         'schedule_revision': x.get('schedule_revision', 0) + 1})


class Decision(BaseModel):
    approve: bool


@app.post('/api/approvals/{ident}/decision')
def decision(ident: str, body: Decision, request: Request):
    old = store.get(ident)
    if not old or old.get('user_id', 'admin') != request.state.user_id:
        raise PermissionError('部署确认不属于当前用户')
    def change(a):
        if a['status'] != 'pending' or a['expires_at'] <= time.time():
            raise ValueError('审批已处理或过期')
        if body.approve:
            from control.terraform import validate_action
            validate_action(a['action'])
        return {**a, 'status': 'approved' if body.approve else 'rejected', 'user': request.state.user_id, 'decided_at': time.time()}
    return store.update(ident, change)


@app.get('/api/terraform/history')
def terraform_history(request: Request, query: str = '', limit: int = 20):
    from control import deploy_history
    return deploy_history.query(request.state.user_id, query, limit)


@app.get('/api/terraform/{ident}')
def terraform_project(ident: str, request: Request):
    p = store.get(ident)
    if not p or p.get('user_id', 'admin') != request.state.user_id:
        raise PermissionError('Terraform项目不属于当前用户')
    from control.terraform import public_project
    detail = public_project(ident)
    return {**detail, 'approvals': [a for a in store.all('approval') if a.get('action', {}).get('project_id') == ident],
            'events': [e for e in store.scoped('event', p['conversation_id']) if e.get('task_id') == p['task_id']][-100:]}


@app.post('/api/terraform/{ident}/approval')
def terraform_approval(ident: str, request: Request):
    p = store.get(ident)
    if not p or p.get('user_id', 'admin') != request.state.user_id:
        raise PermissionError('Terraform项目不属于当前用户')
    from control.terraform import approval
    return approval(ident)


class Knowledge(BaseModel):
    conversation_id: str
    text: str = Field(min_length=1, max_length=5000)
    source: str = Field(default='用户确认', max_length=500)
    category: str = 'fact'
    expires_at: float | None = None


@app.get('/api/knowledge/{ident}')
def knowledge(ident: str, request: Request):
    domain.require_conversation(ident, request.state.user_id)
    return [k for k in store.all('knowledge') if k['conversation_id'] == ident and k.get('user_id', 'admin') == request.state.user_id and not k.get('forgotten')]


@app.post('/api/knowledge')
def remember(body: Knowledge, request: Request):
    domain.require_conversation(body.conversation_id, request.state.user_id)
    if body.category not in ('fact', 'preference', 'skill'):
        raise ValueError('知识类别不支持')
    return store.put({'id': uid('knowledge'), 'kind': 'knowledge', 'user_id': request.state.user_id, **body.model_dump(), 'confirmed': True})


@app.delete('/api/knowledge/{ident}')
def forget(ident: str, request: Request):
    record = store.get(ident)
    if not record or record['kind'] != 'knowledge' or record.get('user_id', 'admin') != request.state.user_id:
        raise ValueError('知识不存在')
    return store.update(ident, lambda x: {**x, 'forgotten': True, 'text': '', 'source': ''})


@app.patch('/api/agents/{name}')
def enable_agent(name: str, body: Decision):
    if name == 'supervisor' and not body.approve:
        raise ValueError('不能停用默认负责人')
    value = store.update('agent-' + name, lambda x: {**x, 'enabled': body.approve})
    registry.refresh_team()
    return value


@app.get('/api/agents')
def team_agents():
    return [a for a in store.all('agent') if a['name'] not in ('harness', 'cdk')]


@app.get('/api/capabilities')
def team_capabilities():
    from control.capabilities import catalog
    return catalog(store.all('agent'),store)


@app.websocket('/api/ws/{ident}')
async def websocket_bridge(ws: WebSocket, ident: str):
    token = ws.cookies.get('cloudops_session', '')
    user_id = session_user(token)
    origin = ws.headers.get('origin', '')
    if not user_id or not origin or origin.split('://')[-1] != ws.headers.get('host'):
        await ws.close(code=1008)
        return
    domain.require_conversation(ident, user_id)
    await ws.accept()
    arn = os.getenv('CLOUDOPS_WS_RUNTIME')
    if not arn:
        if os.getenv('CLOUDOPS_LOCAL_WS') == 'test':
            from control.ws import serve
            try:
                await serve(ws, ident, user_id, lambda: session_user(token) == user_id)
            except asyncio.CancelledError:
                pass
        else:
            await ws.send_json({'type': 'error', 'message': '云端 WebSocket 执行器尚未配置'})
            await ws.close(code=1011)
        return
    import websockets
    from bedrock_agentcore.runtime import AgentCoreRuntimeClient
    from starlette.websockets import WebSocketDisconnect
    client = AgentCoreRuntimeClient(region=os.getenv('AWS_REGION', 'cn-northwest-1'))
    url, headers = await asyncio.to_thread(client.generate_ws_connection, runtime_arn=arn)
    try:
        async with websockets.connect(url, additional_headers=headers, max_size=32768, open_timeout=30) as upstream:
            await upstream.send(json.dumps({'bridge_token': os.environ['CLOUDOPS_BRIDGE_SECRET'], 'conversation_id': ident,
                                            'user_id': user_id, 'expires_at': int(token.split('.')[1])}))
            async def incoming():
                while True:
                    data = await ws.receive()
                    if data['type'] == 'websocket.disconnect':
                        return
                    payload = data.get('bytes') if data.get('bytes') is not None else data.get('text', '')
                    size = len(payload) if isinstance(payload, bytes) else len(payload.encode('utf-8'))
                    if not valid_session(token) or size > 28000:
                        await ws.close(code=1008)
                        return
                    await upstream.send(payload)
            async def outgoing():
                async for payload in upstream:
                    if not valid_session(token):
                        await ws.close(code=1008)
                        return
                    if isinstance(payload, bytes):
                        await ws.send_bytes(payload)
                    else:
                        await ws.send_text(payload)
            tasks = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except (WebSocketDisconnect, websockets.ConnectionClosed):
        pass
    except Exception as e:
        import logging
        status = getattr(getattr(e, 'response', None), 'status_code', None)
        logging.getLogger('cloudops').warning('Runtime WebSocket bridge failed: %s status=%s', type(e).__name__, status)
        try:
            await ws.send_json({'type': 'error', 'message': f'云端连接失败（{type(e).__name__}{": " + str(status) if status else ""}）'})
            await ws.close(code=1011)
        except RuntimeError:
            pass


_OBS_NAMESPACE = 'AWS/Bedrock-AgentCore'

@app.get('/api/observability/tasks')
def observability_tasks(request: Request, hours: int=24, cursor: str='', status: str='', expert: str=''):
    if hours not in (1,6,24,168,720):raise ValueError('不支持的时间窗口')
    return store.task_page(request.state.user_id,time.time()-hours*3600,cursor or None,status,expert)

@app.get('/api/observability/tasks/{ident}')
def observability_task(request: Request, ident: str):
    t=store.get(ident)
    if not t or t.get('kind')!='task' or t.get('user_id','admin')!=request.state.user_id:
        raise HTTPException(404,'任务不存在')
    from control import task_traces
    return task_traces.detail(t,store)

_OBS_METRICS = ('Invocations','SystemErrors','UserErrors','Throttles','Latency','Duration','CPUUsed-vCPUHours','MemoryUsed-GBHours')
_obs_catalog_cache = {'at': 0.0, 'rows': []}

def _obs_catalog(client):
    if time.monotonic() - _obs_catalog_cache['at'] < 60 and _obs_catalog_cache['rows']:
        return _obs_catalog_cache['rows']
    rows=[];token=None
    while True:
        args={'Namespace':_OBS_NAMESPACE}
        if token:args['NextToken']=token
        result=client.list_metrics(**args);rows.extend(result.get('Metrics',[]));token=result.get('NextToken')
        if not token:break
    _obs_catalog_cache.update(at=time.monotonic(),rows=rows)
    return rows

def _obs_series(client, metric, dimensions, start, end, period, stat):
    try:
        r=client.get_metric_statistics(Namespace=_OBS_NAMESPACE,MetricName=metric,Dimensions=dimensions,StartTime=start,EndTime=end,Period=period,Statistics=[stat])
        return [{'at':p['Timestamp'].isoformat(),'value':p.get(stat)} for p in sorted(r.get('Datapoints',[]),key=lambda x:x['Timestamp']) if p.get(stat) is not None]
    except Exception as exc:return {'error':str(exc)}

def _obs_dimensions(rows, metric, required, preferred_names=()):
    candidates=[]
    for row in rows:
        if row.get('MetricName') != metric:continue
        values={d['Name']:d['Value'] for d in row.get('Dimensions',[])}
        if all(values.get(name)==value for name,value in required.items()):candidates.append(row.get('Dimensions',[]))
    if not candidates:return None
    preferred=set(preferred_names)
    candidates.sort(key=lambda dims:(set(d['Name'] for d in dims)!=preferred,len(dims)))
    return candidates[0]

@app.get('/api/observability')
def observability(request: Request, hours: int = 1):
    if hours not in (1,6,24,168):raise ValueError('观测窗口仅支持 1、6、24 或 168 小时')
    from botocore.config import Config
    from concurrent.futures import ThreadPoolExecutor
    region=os.getenv('AWS_REGION','cn-northwest-1');client=boto3.client('cloudwatch',region_name=region,
        config=Config(connect_timeout=3,read_timeout=8,max_pool_connections=16,retries={'max_attempts':1}))
    end=datetime.now(timezone.utc);start=end-timedelta(hours=hours);period=60 if hours<=6 else (300 if hours<=24 else 3600)
    catalog=_obs_catalog(client)
    agents=[]
    for agent in registry.active():
        arn=agent.get('runtime_arn','');resource=arn;metrics={}
        operation='InvokeAgentRuntimeWithWebSocketStream' if agent['name']=='supervisor' else 'InvokeAgentRuntime'
        if arn:
            for metric in _OBS_METRICS:
                source_metric='ConnectionDuration' if metric=='Duration' and agent['name']=='supervisor' else metric
                if metric in ('CPUUsed-vCPUHours','MemoryUsed-GBHours'):
                    dims=_obs_dimensions(catalog,metric,{'Resource':resource,'Service':'AgentCore.Runtime'},('Resource','Service'))
                else:
                    preferred=('Resource','Operation') if agent['name']=='supervisor' else ('ComputeType','Name','Operation','Resource')
                    dims=_obs_dimensions(catalog,source_metric,{'Resource':resource,'Operation':operation},preferred)
                if not dims:metrics[metric]=[];continue
                stat='Sum' if metric in ('Invocations','SystemErrors','UserErrors','Throttles','CPUUsed-vCPUHours','MemoryUsed-GBHours') else 'Average'
                metrics[metric]=(source_metric,dims,stat)
        agents.append({'name':agent['name'],'display_name':agent.get('display_name',agent['name']),'runtime_arn':arn,'runtime_version':agent.get('runtime_version'),'metrics':metrics,'metric_operation':operation,'duration_label':'连接时长' if agent['name']=='supervisor' else 'Duration'})
    gateway={}
    for metric in ('Invocations','SystemErrors','UserErrors','Throttles','Latency','Duration','TargetExecutionTime'):
        try:
            dims=_obs_dimensions(catalog,metric,{'Operation':'InvokeGateway','Protocol':'MCP'},('Operation','Protocol'))
            stat='Sum' if metric in ('Invocations','SystemErrors','UserErrors','Throttles') else 'Average'
            gateway[metric]=[] if not dims else (metric,dims,stat)
        except Exception as exc:gateway[metric]={'error':str(exc)}
    tools=[]
    # Independent CloudWatch requests previously took over a minute in series.
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending=[]
        for group in [a['metrics'] for a in agents]+[gateway]+[t['metrics'] for t in tools]:
            for name,spec in group.items():
                if isinstance(spec,tuple):
                    metric,dims,stat=spec
                    pending.append((group,name,pool.submit(_obs_series,client,metric,dims,start,end,period,stat)))
        for group,name,future in pending:group[name]=future.result()
    return {'region':region,'hours':hours,'generated_at':end.isoformat(),'agents':agents,'gateway':gateway,'tools':tools,'note':'CloudWatch 指标可能延迟数分钟；“暂无数据”表示所选窗口没有数据，不代表服务异常。'}
