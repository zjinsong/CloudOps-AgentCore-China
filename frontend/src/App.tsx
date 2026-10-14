import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import Button from '@cloudscape-design/components/button';
import Modal from '@cloudscape-design/components/modal';
import Input from '@cloudscape-design/components/input';
import Textarea from '@cloudscape-design/components/textarea';
import Alert from '@cloudscape-design/components/alert';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api, ConversationSocket, Microphone } from './cloudopsClient';
import './cloudops.css';
const Observability = lazy(() => import('./Observability'));

type Row = { id: string; [key: string]: any };
const names: Record<string,string> = {
  supervisor:'CloudOps 协调员', finops:'FinOps 成本专家', monitoring:'Monitoring 监控专家',
  cloudtrail:'CloudTrail 审计专家', terraform:'Terraform 基础设施专家', agentcore:'AgentCore 平台专家', user:'你'
};
const states: Record<string,string> = {
  queued:'等待执行', running:'执行中', waiting_for_dependencies:'等待专家', waiting_for_input:'等待补充',
  retry_scheduled:'等待重试', paused:'已暂停', failed:'失败', cancelled:'已取消', succeeded:'完成',
  plan_queued:'正在准备', planning:'正在生成 plan', planned:'等待你的决定', applying:'正在部署',
  pending:'等待你的决定', approved:'已允许', executing:'正在部署', rejected:'已拒绝', needs_review:'需要核对'
};
const terminal = new Set(['succeeded','failed','cancelled']);
const stamp = (value?:number) => value ? new Date(value * 1000).toLocaleString('zh-CN', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '尚未执行';
const zonedStamp=(value:number|undefined,zone:string)=>{try{return value?new Date(value*1000).toLocaleString('zh-CN',{timeZone:zone}):'请检查日期、时间与时区'}catch{return '请输入有效时区'}};
const progressText=(task:Row)=>{
  const value=task.last_progress||task.progress?.stage||task.error;
  if(value?.includes('正在查询 ')&&/lookup_events|___|provider|module/.test(value)) {
    return ({finops:'正在查询费用数据',monitoring:'正在检查告警与健康事件',cloudtrail:'正在查询操作记录',terraform:'正在查阅 Terraform 官方文档'} as Record<string,string>)[task.owner]||'专家正在查询数据';
  }
  return value||(task.status==='queued'?'已接收，正在等待执行；任务会在后台继续。':`${names[task.owner]||task.owner} 正在处理`);
};

function Bot({name}:{name:string}) {
  return <svg className={`bot bot-${name}`} viewBox="0 0 48 52" aria-hidden="true">
    <path d="M24 2C20 12 5 22 5 33a19 17 0 0 0 38 0C43 22 28 12 24 2Z" fill="currentColor"/>
    <rect x="11" y="25" width="26" height="15" rx="7" fill="#142b33"/>
    <circle cx="18" cy="32" r="2.5" fill="#fff"/><circle cx="30" cy="32" r="2.5" fill="#fff"/>
    <path d="M20 37h8" stroke="#fff" strokeWidth="1.5"/>
  </svg>;
}

export default function App() {
  const [boot,setBoot] = useState<any>();
  const [observability,setObservability] = useState(false);
  const [traceTask,setTraceTask] = useState<string>();
  const viewTrace=(id:string)=>{setTraceTask(id);setNavGroup('observability');setObservability(true)};
  const [navGroup,setNavGroup] = useState<'cloudops'|'observability'|null>('cloudops');
  const [agentName,setAgentName] = useState('supervisor');
  const [current,setCurrent] = useState('');
  const [snap,setSnap] = useState<any>();
  const [username,setUsername] = useState('admin');
  const [password,setPassword] = useState('');
  const [error,setError] = useState('');
  const [text,setText] = useState('');
  const [connection,setConnection] = useState('连接中');
  const [modal,setModal] = useState('');
  const [form,setForm] = useState<any>({interval_hours:24});
  const [attachments,setAttachments] = useState<Row[]>([]);
  const [mention,setMention] = useState(false);
  const [recording,setRecording] = useState(false);
  const [voicePending,setVoicePending] = useState(false);
  const [detail,setDetail] = useState<any>();
  const [runDetail,setRunDetail] = useState<any>();
  const [runLoading,setRunLoading] = useState('');
  const [runError,setRunError] = useState('');
  const [language,setLanguage] = useState('zh-CN');
  const [panel,setPanel] = useState(false);
  const [preview,setPreview] = useState<number>();
  const [sending,setSending] = useState(false);
  const [now,setNow] = useState(Date.now());
  const bootRequest=useRef<Promise<any>>();
  const bootReadAt=useRef(0);
  const socket=useRef<ConversationSocket>();
  const mic=useRef<Microphone>();
  const picker=useRef<HTMLInputElement>(null);
  const composing=useRef(false);
  const transcript=useRef({base:'',final:new Map<string,string>()});
  const bottom=useRef<HTMLDivElement>(null);
  const activeByAgent=useRef<Record<string,string>>({});
  const entering=useRef(0);
  const speechLanguage=useRef('zh-CN');
  const currentConversation=useRef(current);
  currentConversation.current=current;

  async function run(fn:()=>Promise<unknown>) {
    try { setError(''); await fn(); } catch(e) { setError((e as Error).message); }
  }
  async function refresh(force=true) {
    if(bootRequest.current)return bootRequest.current;
    if(!force&&boot&&Date.now()-bootReadAt.current<2000)return boot;
    bootRequest.current=api('/bootstrap').then(value=>{setBoot(value);bootReadAt.current=Date.now();return value}).finally(()=>{bootRequest.current=undefined});
    return bootRequest.current;
  }
  async function remember(owner:string, ident:string, user:string) {
    await api(`/conversations/${ident}/activate`,{});
    activeByAgent.current={...activeByAgent.current,[owner]:ident};
  }
  async function enter(owner:string, forceNew=false, knownBoot?:any) {
    const revision=++entering.current;
    const available=knownBoot||boot;
    const saved=activeByAgent.current[owner]||available.active_conversations?.[owner];
    const reusable=!forceNew&&saved&&available.conversations.some((item:Row)=>item.id===saved&&item.owner===owner);
    const conversation=reusable?{id:saved}:await api('/conversations',{owner,previous_id:forceNew?saved:undefined});
    if(revision!==entering.current)return;
    await remember(owner,conversation.id,available.user);
    if(revision!==entering.current)return;
    setAgentName(owner);
    setCurrent(conversation.id);
    setModal('');
    if(!reusable)await refresh();
  }
  async function start() {
    const value=await refresh();
    activeByAgent.current=value.active_conversations||{};
    const preferred=localStorage.getItem(`cloudops-language-${value.user}`)||'zh-CN';
    setLanguage(['zh-CN','zh-HK','en-US'].includes(preferred)?preferred:'zh-CN');
    await enter('supervisor',false,value);
  }
  useEffect(()=>{void (async()=>{try{await start()}catch(e){if((e as Error).message!=='需要登录')setError((e as Error).message)}})()},[]);
  useEffect(()=>{if(!boot)return;const timer=setInterval(()=>void run(()=>refresh(false)),15000);return()=>clearInterval(timer)},[Boolean(boot)]);
  useEffect(()=>{if(!snap?.tasks?.some((t:Row)=>!terminal.has(t.status)))return;setNow(Date.now());const timer=setInterval(()=>setNow(Date.now()),1000);return()=>clearInterval(timer)},[Boolean(snap?.tasks?.some((t:Row)=>!terminal.has(t.status)))]);
  useEffect(()=>{
    socket.current?.close(); mic.current?.stop(); setSnap(undefined); setText(''); setAttachments([]);
    setMention(false); setRecording(false); setVoicePending(false);
    setSending(false);
    if(!current)return;
    const ws=new ConversationSocket(current,value=>{
      if(socket.current!==ws)return;
      if(value.type==='snapshot') {
        setSnap(value);
      }
      if(value.type==='message.ack') {
        setSending(false);
        setSnap((previous:any)=>previous?{...previous,tasks:[...(previous.tasks||[]).filter((t:Row)=>t.id!==value.task.id),value.task]}:previous);
      }
      if(value.type==='message.ack'||value.type==='task.event')void run(()=>refresh(false));
      if(value.type==='error'){setError(value.message);setSending(false);if(value.code==='transcribe'){mic.current?.stop();setRecording(false);setVoicePending(false)}}
      if(value.type==='audio.ready')void (async()=>{
        const microphone=new Microphone(ws);
        try { mic.current=microphone;await microphone.start();if(socket.current!==ws||ws.stopped){microphone.stop();return}setRecording(true);setVoicePending(false); }
        catch(e) { ws.send({type:'audio.cancel'});setError((e as Error).message);setVoicePending(false); }
      })();
      if(value.type==='audio.stopped'){mic.current?.stop();setRecording(false);setVoicePending(false)}
      if(value.type==='transcript.final')transcript.current.final.set(value.result_id,value.text);
      if(value.type.startsWith('transcript.')){
        const separator=speechLanguage.current==='en-US'?' ':'';
        const final=[...transcript.current.final.values()].join(separator);
        const partial=value.type==='transcript.partial'?value.text:'';
        setText(transcript.current.base+final+(final&&partial?separator:'')+partial);
      }
    },setConnection);
    socket.current=ws;
    return()=>{mic.current?.stop();ws.close()};
  },[current]);
  useEffect(()=>{
    setPreview(undefined);
    if(modal!=='routine'||!form.start_date||!form.time)return;
    let live=true;
    const timer=setTimeout(()=>void api('/schedule-preview',form).then(value=>{if(live)setPreview(value.next_at)}).catch(()=>{}),250);
    return()=>{live=false;clearTimeout(timer)};
  },[modal,form.start_date,form.time,form.repeat,form.timezone,form.interval_hours]);
  useEffect(()=>{
    if(!snap)return;
    const frame=requestAnimationFrame(()=>bottom.current?.scrollIntoView({block:'end'}));
    return()=>cancelAnimationFrame(frame);
  },[current,snap?.messages?.length,snap?.tasks?.map((t:Row)=>t.reply_text||'').join(''),boot?.projects?.length]);

  async function upload(file:File) {
    if(!current)throw new Error('对话正在建立，请稍后上传');
    const conversationId=current;
    const result=await api(`/conversations/${current}/uploads`,{name:file.name,size:file.size,content_type:file.type||'application/octet-stream'});
    const body=new FormData();
    Object.entries(result.upload.fields).forEach(([key,value])=>body.append(key,String(value)));
    body.append('file',file);
    const response=await fetch(result.upload.url,{method:'POST',body});
    if(!response.ok)throw new Error('文件上传失败');
    await api(`/conversations/${current}/files/${result.file.id}/complete`,{});
    if(currentConversation.current===conversationId)setAttachments(value=>[...value,{...result.file,status:'pending'}]);
  }
  async function removeAttachment(file:Row) {
    await api(`/conversations/${current}/files/${file.id}`,undefined,'DELETE');
    setAttachments(value=>value.filter(item=>item.id!==file.id));
  }
  function selectedExperts() {
    if(agentName!=='supervisor')return [];
    const requested=[...text.matchAll(/(?<![\w@])@\s*([a-z][a-z0-9_-]*)/gi)].map(match=>match[1].toLowerCase());
    return [...new Set(requested)].filter(name=>name!=='supervisor'&&boot.agents.some((agent:Row)=>agent.name===name));
  }
  function send() {
    if(recording||voicePending||sending)return;
    const body=text.trim();
    if(!body||!socket.current)return;
    try { socket.current.submit(body,selectedExperts(),attachments.map(file=>file.id)); }
    catch(e) { setError((e as Error).message);return; }
    setSending(true);setText('');setAttachments([]);setMention(false);
    transcript.current={base:'',final:new Map()};
  }
  function addMention(name:string) {
    setText(value=>value.replace(/(^|\s)@[\w-]*$/,(match,prefix)=>`${prefix}@${name} `));
    setMention(false);
  }
  function voice() {
    if(!socket.current)return;
    try {
      if(recording||voicePending){mic.current?.stop();setRecording(false);setVoicePending(false);socket.current.send({type:'audio.end'});}
      else {speechLanguage.current=language;transcript.current={base:text,final:new Map()};socket.current.send({type:'audio.start',language_code:language});setVoicePending(true);}
    }catch(e){setError((e as Error).message);setVoicePending(false)}
  }
  async function decide(approval:Row,allow:boolean) {
    await api(`/approvals/${approval.id}/decision`,{approve:allow});
    await refresh();
  }
  async function allowProject(project:Row, existing?:Row) {
    const approval=existing||await api(`/terraform/${project.id}/approval`,{});
    await decide(approval,true);
  }
  async function rejectProject(project:Row, existing?:Row) {
    const approval=existing||await api(`/terraform/${project.id}/approval`,{});
    await decide(approval,false);
  }
  async function openRoutine(ident:string) {
    const value=await api(`/routines/${ident}`);setDetail(value);setRunDetail(undefined);setRunError('');setModal('routine-detail');
  }
  function editRoutine(value?:any) {
    const now=new Date();
    const date=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(now);
    setForm(value?{...value,...value.schedule}:{owner:agentName,name:'',prompt:'',start_date:date,time:'09:00',repeat:'daily',timezone:'Asia/Shanghai',enabled:true,interval_hours:24});setModal('routine');
  }
  async function showProject(ident:string){setDetail(await api(`/terraform/${ident}`));setModal('terraform-detail')}
  async function deleteConversation() {
    await api(`/conversations/${form.id}`,undefined,'DELETE');
    const available=await refresh();
    activeByAgent.current=available.active_conversations||{};
    if(form.id===current)await enter(agentName,false,available);
    setModal('');
  }
  function conversationMenu(item:any,currentItem=false) {
    return <details className="conversation-menu"><summary aria-label={currentItem?'当前对话操作':`对话操作 ${item.title}`}>⋯</summary><div role="menu"><button role="menuitem" onClick={event=>{event.currentTarget.closest('details')?.removeAttribute('open');setForm({id:item.id,title:item.title});setModal('rename')}}>重命名</button><button role="menuitem" onClick={event=>{event.currentTarget.closest('details')?.removeAttribute('open');setForm({id:item.id,title:item.title});setModal('delete-conversation')}}>删除对话</button></div></details>;
  }

  if(!boot)return <div className="login"><div className="login-card">
    <Bot name="supervisor"/><h1>CloudOps</h1><p>登录后开始与你的云运维专家对话。</p>
    {error&&<Alert type="error">{error}</Alert>}
    <Input value={username} onChange={event=>setUsername(event.detail.value)} placeholder="用户名"/>
    <Input value={password} type="password" onChange={event=>setPassword(event.detail.value)} placeholder="密码"/>
    <Button variant="primary" fullWidth onClick={()=>void run(async()=>{await api('/login',{username,password});await start()})}>登录</Button>
  </div></div>;

  const agent=boot.agents.find((item:Row)=>item.name===agentName);
  const conversations=boot.conversations.filter((item:Row)=>item.owner===agentName&&item.id!==current);
  const routines=boot.routines.filter((item:Row)=>item.owner===agentName);
  const tasks=(snap?.tasks||[]).filter((item:Row)=>!terminal.has(item.status)&&!item.parent_id);
  const projects=agentName==='terraform' ? boot.projects.filter((item:Row)=>item.conversation_id===current) : [];
  const conversation=boot.conversations.find((item:Row)=>item.id===current)||snap?.conversation;
  const projectState=(p:any,a?:any)=>p.status==='succeeded'?'部署成功':p.status==='failed'?(p.apply_started_at?'部署失败':'计划生成失败'):p.status==='applying'?'正在部署':a?.status==='approved'?'已批准 · 等待执行':a?.status==='rejected'?'已拒绝':a?.status==='needs_review'?'执行情况需核对':states[p.status]||p.status;

  return <div className={`app${observability?' observability-app':''}`}>
    <aside className="agents">
      <div className="brand"><Bot name="supervisor"/><div><strong>CloudOps</strong><small>云运维工作台</small></div></div>
      <nav className="workspace-nav" aria-label="工作区">
        <section className={navGroup==='cloudops'?'open':''}>
          <button className="workspace-group" onClick={()=>setNavGroup(navGroup==='cloudops'?null:'cloudops')}><span className="workspace-symbol">⌁</span><span><strong>CloudOps</strong><small>多 Agent 运维协作</small></span><b>⌄</b></button>
          <div className="workspace-children">
            {boot.agents.map((item:Row)=><button key={item.name} className={!observability&&agentName===item.name?'selected':''}
              onClick={()=>{setNavGroup('cloudops');setObservability(false);void run(()=>enter(item.name))}}><Bot name={item.name}/><span>{item.display_name}</span></button>)}
            <button className="workspace-child team-child" aria-label="团队管理" onClick={()=>void run(async()=>{const [agents,capabilities]=await Promise.all([api('/agents'),api('/capabilities')]);setForm({agents,capabilities});setModal('team')})}>
              <span className="child-icon">♙</span><span>团队管理</span>
            </button>
          </div>
        </section>
        <section className={navGroup==='observability'?'open':''}>
          <button className="workspace-group" onClick={()=>setNavGroup(navGroup==='observability'?null:'observability')}><span className="workspace-symbol">◫</span><span><strong>观测指标</strong><small>Runtime 与任务调用链</small></span><b>⌄</b></button>
          <div className="workspace-children"><button className={observability?'selected':''} onClick={()=>{setNavGroup('observability');setObservability(true)}}><span className="child-icon">◉</span><span>全部指标</span></button></div>
        </section>
      </nav>
      <div className="sidebar-bottom"><span>{boot.user}</span><button onClick={()=>void run(async()=>{await api('/logout',{});location.reload()})}>退出登录</button></div>
    </aside>
    {observability&&<Suspense fallback={<div>正在加载观测指标…</div>}><Observability initialTask={traceTask}/></Suspense>}
    <main style={{display:observability?'none':undefined}}>
      <header className="chat-header">
        <div className="current-agent"><Bot name={agentName}/><div><h1>{conversation?.title||'新对话'}</h1><small>{agent?.display_name||names[agentName]}</small></div></div>
        <div className="header-actions">
          <span className={`connection ${connection==='已连接'?'online':''}`}>{connection}</span>
          <button className="panel-toggle" onClick={()=>setPanel(!panel)} aria-label="对话与定时任务">☰</button>
        </div>
      </header>
      <section className={!snap?.messages?.length&&!projects.length&&!tasks.length?'messages fresh':'messages'}>
        {!snap?.messages?.length&&<div className="welcome"><Bot name={agentName}/><span className="eyebrow">新对话</span>
          <h2>{agent?.display_name||names[agentName]}</h2><p>{agent?.intro||'告诉我你希望完成什么。'}</p></div>}
        {snap?.messages?.map((message:Row)=><article className={`message ${message.role}`} key={message.id}>
          <Bot name={message.role==='user'?'user':(message.agent||agentName)}/>
          <div><div className="message-heading"><strong>{message.role==='user'?'你':names[message.agent||agentName]||message.agent}</strong><time>{stamp(message.created_at)}</time></div>
            {message.role==='user'&&message.explicit_owners?.length>0&&<div className="message-assignments" aria-label="指定专家">指定专家：{message.explicit_owners.map((name:string)=><span key={name}>@{names[name]||name}</span>)}</div>}
            {message.task_id&&<button className="task-trace-link" onClick={()=>viewTrace(message.task_id)}>查看调用链</button>}<div className="message-body">{message.role==='user'?message.text:<Markdown remarkPlugins={[remarkGfm]}>{message.text}</Markdown>}</div></div></article>)}
        {sending&&<div className="task" role="status">正在发送，等待服务器确认接收…</div>}
        {tasks.map((task:Row)=><div className="task" key={task.id} role="status">
          <StatusIndicator type={task.status==='failed'?'error':task.status==='waiting_for_input'?'warning':'in-progress'}>{states[task.status]||task.status}</StatusIndicator>
          <span>{progressText(task)}</span><button className="task-trace-link" onClick={()=>viewTrace(task.id)}>查看调用链</button>
          <small>已等待 {Math.max(0,Math.floor((now-task.created_at*1000)/1000))} 秒{task.progress?.updated_at?` · 最近更新 ${stamp(task.progress.updated_at)}`:''}</small>
          {task.reply_text&&<div className="message-body streaming-reply" aria-label="正在生成的答复"><Markdown remarkPlugins={[remarkGfm]}>{task.reply_text}</Markdown></div>}
          {['paused','waiting_for_input','failed'].includes(task.status)&&<Button onClick={()=>void run(async()=>{await api(`/tasks/${task.id}/action`,{status:'queued'});socket.current?.send({type:'sync'})})}>继续</Button>}
        </div>)}
        {projects.map((project:Row)=>{
          const approval=boot.approvals.find((item:Row)=>item.action?.project_id===project.id);
          return <section className="terraform-inline" key={project.id}>
            <div className="terraform-heading"><span className="terraform-mark">T</span><div><strong>{project.name}</strong><small>{projectState(project,approval)}</small></div><button className="text-link" onClick={()=>void run(()=>showProject(project.id))}>查看详情 →</button></div>
            <p>{project.status==='succeeded'?'部署已完成，详情中可查看资源与执行记录。':project.status==='applying'?'正在执行你批准的 plan。':approval?.status==='approved'?'已允许执行，正在等待部署结果。':approval?.status==='rejected'?'你已拒绝此 plan，没有启动部署。':project.error||project.summary||'正在准备 Terraform 变更预览。'}</p>
            {project.status==='planned'&&(!approval||approval.status==='pending')&&<><div className="plan-meta"><span>{project.region}</span><span>{(project.changes||[]).length} 项变更</span><span>有效至 {stamp(project.plan_expires_at)}</span></div>
              {project.plan_expires_at<=Date.now()/1000?<p className="status-warning">plan 已过期，请重新生成后再确认。</p>:
                (!approval||approval.status==='pending')&&<div className="approval-actions"><Button variant="primary" onClick={()=>void run(()=>allowProject(project,approval))}>允许运行此 plan</Button><Button onClick={()=>void run(()=>rejectProject(project,approval))}>拒绝</Button></div>}
            </>}
            {approval?.status==='needs_review'&&<p className="status-warning">执行结果需要人工核对，请查看任务进度。</p>}
          </section>;
        })}
        <div ref={bottom}/>
      </section>
      <section className="composer">
        {attachments.length>0&&<div className="attachments">{attachments.map(file=><span key={file.id}>{file.name}<button aria-label={`移除 ${file.name}`} onClick={()=>void run(()=>removeAttachment(file))}>×</button></span>)}</div>}
        {mention&&agentName==='supervisor'&&<div className="mentions">{boot.agents.filter((item:Row)=>item.name!=='supervisor').map((item:Row)=><button key={item.name} onClick={()=>addMention(item.name)}><Bot name={item.name}/><span>{item.display_name}</span></button>)}</div>}
        <textarea aria-label="消息输入" value={text} placeholder={agentName==='supervisor'?'向协调员交办任务，输入 @ 可指定专家':'输入消息'}
          onChange={event=>{setText(event.target.value);setMention(agentName==='supervisor'&&/(^|\s)@[\w-]*$/.test(event.target.value))}}
          onCompositionStart={()=>composing.current=true} onCompositionEnd={()=>composing.current=false}
          onKeyDown={event=>{if(event.key==='Enter'&&!event.shiftKey&&!composing.current&&!mention){event.preventDefault();send()}}}/>
        <div className="composer-actions">
          <input ref={picker} hidden type="file" multiple onChange={event=>{[...(event.target.files||[])].forEach(file=>void run(()=>upload(file)));event.currentTarget.value=''}}/>
          <button className="add-file" title="添加本地文件" aria-label="添加本地文件" onClick={()=>picker.current?.click()}>＋</button>
          <Button disabled={connection!=='已连接'&&!recording&&!voicePending} onClick={voice}>{recording?'停止录音':voicePending?'正在连接':'语音输入'}</Button>
          <select aria-label="语音语言" value={language} disabled={recording||voicePending} onChange={event=>{setLanguage(event.target.value);localStorage.setItem(`cloudops-language-${boot.user}`,event.target.value)}}><option value="zh-CN">普通话</option><option value="zh-HK">粤语</option><option value="en-US">English</option></select>
          <span>Enter 发送 · Shift+Enter 换行</span>
          <Button variant="primary" disabled={!text.trim()||connection!=='已连接'||recording||voicePending} onClick={send}>发送</Button>
        </div>
      </section>
    </main>
    <aside style={{display:observability?'none':undefined}} className={`workspace-panel ${panel?'open':''}`} aria-label="对话与定时任务侧栏">
      <div className="panel-title"><span>工作空间</span><button onClick={()=>setPanel(false)} className="panel-close" aria-label="关闭侧栏">×</button></div>
      <button className="new-chat" onClick={()=>void run(async()=>{await enter(agentName,true);setPanel(false)})}>＋ 新对话</button>
      <div className="section-label">当前对话</div>
      <div className="conversation-row active"><span>{conversation?.title||'新对话'}</span>{conversationMenu({id:current,title:conversation?.title||'新对话'},true)}</div>
      <div className="section-label">最近 <span>{Math.min(conversations.length,10)} / 10</span></div>
      <div className="recent-list">{conversations.slice(0,10).map((item:Row)=><div className="conversation-row" key={item.id}><button className="conversation-link" title={item.summary||item.title} onClick={()=>void run(async()=>{await remember(agentName,item.id,boot.user);setCurrent(item.id);await refresh();setPanel(false)})}>{item.title||'未命名对话'}</button>{conversationMenu(item)}</div>)}</div>
      {!conversations.length&&<p className="panel-empty">新建对话后，之前的对话会出现在这里。</p>}
      <div className="section-label schedule-label">定时任务 <button aria-label="新增定时任务" onClick={()=>editRoutine()}>＋</button></div>
      {routines.map((routine:Row)=><button className="schedule-item" key={routine.id} onClick={()=>void run(()=>openRoutine(routine.id))}><span><strong>{routine.name}</strong><small>{routine.enabled?`下次 ${stamp(routine.next_at)}`:'已停用'}</small></span><span className={routine.enabled?'schedule-dot enabled':'schedule-dot'}/></button>)}
      {!routines.length&&<p className="panel-empty">添加一个任务，让专家按时检查。</p>}
      <button className="text-link add-schedule" onClick={()=>editRoutine()}>＋ 新增定时任务</button>
    </aside>
    <Modal visible={modal==='rename'} onDismiss={()=>setModal('')} header="重命名对话" footer={<Button variant="primary" onClick={()=>void run(async()=>{await api(`/conversations/${form.id}`,{title:form.title},'PATCH');await refresh();setModal('')})}>保存</Button>}>
      <Input value={form.title||''} onChange={event=>setForm({...form,title:event.detail.value})}/>
    </Modal>
    <Modal visible={modal==='delete-conversation'} onDismiss={()=>setModal('')} header="删除对话？" footer={<div className="approval-actions"><Button onClick={()=>setModal('')}>取消</Button><Button variant="primary" onClick={()=>void run(deleteConversation)}>删除</Button></div>}>
      <p>删除“{form.title}”后，它将不再出现在历史中，也无法继续打开。已启动的后台任务会继续执行。</p>
    </Modal>
    <Modal visible={modal==='routine'} onDismiss={()=>setModal('')} header={form.id?'编辑定时任务':'新增定时任务'}>
      <label>名称<Input ariaLabel="任务名称" value={form.name||''} onChange={event=>setForm({...form,name:event.detail.value})}/></label>
      <label>执行要求<Textarea ariaLabel="执行要求" value={form.prompt||''} onChange={event=>setForm({...form,prompt:event.detail.value})}/></label>
      <div className="schedule-fields"><label>开始日期<input aria-label="开始日期" type="date" value={form.start_date||''} onChange={event=>setForm({...form,start_date:event.target.value})}/></label><label>时间<input aria-label="执行时间" type="time" value={form.time||''} onChange={event=>setForm({...form,time:event.target.value})}/></label></div>
      <label>重复<select aria-label="重复" value={form.repeat||'daily'} onChange={event=>setForm({...form,repeat:event.target.value})}><option value="once">仅一次</option><option value="daily">每天</option><option value="weekly">每周（开始日期对应的星期）</option><option value="monthly">每月（开始日期对应的日，月末不足则取最后一天）</option>{form.repeat==='interval'&&<option value="interval">固定间隔（已有任务）</option>}</select></label>
      {form.repeat==='interval'&&<label>间隔（小时）<Input type="number" value={String(form.interval_hours)} onChange={event=>setForm({...form,interval_hours:Number(event.detail.value)})}/></label>}
      <label>时区<input aria-label="时区" list="timezones" value={form.timezone||''} onChange={event=>setForm({...form,timezone:event.target.value})}/><datalist id="timezones"><option>Asia/Shanghai</option><option>Asia/Hong_Kong</option><option>UTC</option><option>America/New_York</option><option>Europe/London</option></datalist></label>
      <p className="detail-meta">下次执行：{zonedStamp(preview,form.timezone||'Asia/Shanghai')} · {form.timezone}</p>
      <label className="checkbox"><input type="checkbox" checked={Boolean(form.enabled)} onChange={event=>setForm({...form,enabled:event.target.checked})}/>启用任务</label>
      <Button variant="primary" onClick={()=>void run(async()=>{const saved=await api(form.id?`/routines/${form.id}`:'/routines',{...form,owner:agentName},form.id?'PATCH':'POST');await refresh();await openRoutine(saved.id)})}>保存</Button>
    </Modal>
    <Modal visible={modal==='routine-detail'} onDismiss={()=>setModal('')} header={detail?.name||'定时任务'} size="large" footer={<div className="approval-actions"><Button onClick={()=>editRoutine(detail)}>编辑任务</Button><Button onClick={()=>void run(async()=>{await api(`/routines/${detail.id}/toggle`,{});await refresh();await openRoutine(detail.id)})}>{detail?.enabled?'停用':'启用'}</Button><Button onClick={()=>void run(()=>openRoutine(detail.id))}>刷新记录</Button><Button onClick={()=>setModal('delete-routine')}>删除任务</Button></div>}>
      {runDetail?<section className="run-detail">
        <button className="run-back" onClick={()=>setRunDetail(undefined)}>← 返回执行历史</button>
        <h3>{stamp(runDetail.scheduled_for||runDetail.created_at)} · {states[runDetail.status]||runDetail.status}</h3>
        <p className="detail-meta">开始 {stamp(runDetail.started_at)} · 结束 {stamp(runDetail.finished_at)}{runDetail.started_at&&runDetail.finished_at?` · 用时 ${Math.round(runDetail.finished_at-runDetail.started_at)} 秒`:''}</p>
        {runDetail.error&&<p className="run-error">{runDetail.error}</p>}
        <div className="run-result message-body">{runDetail.result?<Markdown remarkPlugins={[remarkGfm]}>{runDetail.result}</Markdown>:<p>{runDetail.last_progress||'任务正在等待执行。'}</p>}</div>
        {Boolean(runDetail.events?.length)&&<details className="run-events"><summary>执行过程（{runDetail.events.length} 条）</summary>{runDetail.events.map((event:Row)=><p className="timeline-event" key={event.id}><time>{stamp(event.created_at)}</time> {event.text||event.message}</p>)}</details>}
      </section>:<>
      <StatusIndicator type={detail?.enabled?'success':'stopped'}>{detail?.enabled?'已启用':'已停用'}</StatusIndicator>
      <p className="detail-prompt">{detail?.prompt}</p><p className="detail-meta">{detail?.schedule?.repeat==='interval'?`每 ${detail?.schedule?.interval_hours||detail?.interval_hours} 小时`:({once:'仅一次',daily:'每天',weekly:'每周',monthly:'每月'} as Record<string,string>)[detail?.schedule?.repeat]} · {detail?.schedule?.time} · {detail?.schedule?.timezone}<br/>下次：{detail?.enabled?zonedStamp(detail.next_at,detail.schedule?.timezone||'Asia/Shanghai'):'已停用，不再触发'}</p>
      <h3>执行历史</h3><p className="detail-meta">每次定时触发独立执行，记录保留 30 天。点击某次执行查看完整结果。</p>
      {!detail?.runs?.length&&<p className="history-empty">尚未执行。</p>}
      {runError&&<p className="run-error" role="alert">{runError}</p>}
      <div className="run-list">{detail?.runs?.map((item:Row)=><button key={item.id} disabled={Boolean(runLoading)} aria-busy={runLoading===item.id} onClick={async()=>{setRunError('');setRunLoading(item.id);try{setRunDetail(await api(`/routines/${detail.id}/runs/${item.id}`))}catch(e){setRunError('打开执行记录失败：'+(e as Error).message)}finally{setRunLoading('')}}}><time>{stamp(item.scheduled_for||item.created_at)}</time><span>{runLoading===item.id?'正在打开…':states[item.status]||item.status}</span><small>{item.result_summary||item.error||'查看进度与结果 →'}</small></button>)}</div>
      </>}
    </Modal>
    <Modal visible={modal==='delete-routine'} onDismiss={()=>setModal('routine-detail')} header="删除定时任务？" footer={<div className="approval-actions"><Button onClick={()=>setModal('routine-detail')}>取消</Button><Button variant="primary" onClick={()=>void run(async()=>{await api(`/routines/${detail.id}`,undefined,'DELETE');setModal('');setDetail(undefined);setRunDetail(undefined);await refresh()})}>确认删除</Button></div>}>
      <p>删除“{detail?.name}”后，将从列表移除并停止后续调度。已触发的执行会继续，历史记录按原保留期保存。</p>
    </Modal>
    <Modal visible={modal==='terraform-detail'} onDismiss={()=>setModal('')} header={`${detail?.name||'Terraform'} · 记录详情`} size="large">
      <p>{detail&&projectState(detail,detail.approvals?.[detail.approvals.length-1])}</p><p className="detail-meta">账号 {detail?.account} · {detail?.region}</p><p>{detail?.summary||detail?.error}</p>
      <h3>审批与执行</h3><p className="detail-meta">批准后排队执行；以部署结果判断是否成功。</p>
      {detail?.started_at&&<p className="timeline-event">{stamp(detail.started_at)} · 开始生成 plan</p>}{detail?.planned_at&&<p className="timeline-event">{stamp(detail.planned_at)} · plan 已生成</p>}
      {detail?.approvals?.length?detail.approvals.map((item:Row)=><p key={item.id} className="timeline-event">{stamp(item.decided_at||item.created_at)} · {states[item.status]||item.status}{item.user?` · ${item.user}`:''}</p>):<p className="detail-meta">尚未批准。</p>}
      {detail?.events?.map((event:Row)=><p className="timeline-event" key={event.id}>{stamp(event.created_at)} · {event.text||event.message}</p>)}
      {detail?.apply_started_at&&<p className="timeline-event">{stamp(detail.apply_started_at)} · 开始部署</p>}{detail?.finished_at&&<p className="timeline-event">{stamp(detail.finished_at)} · {projectState(detail)}</p>}
      <div className="change-list">{detail?.changes?.map((change:Row,index:number)=><div key={index}><code>{change.address}</code><span>{(change.actions||[]).join(' → ')}</span></div>)}</div>
      <h3>Terraform 脚本</h3><pre className="detail-code">{detail?.script}</pre><p className="detail-meta">plan SHA256：{detail?.plan_sha256||'尚未生成'}</p>
      {detail?.result&&<><h3>执行结果</h3><pre className="detail-code">{JSON.stringify(detail.result,null,2)}</pre></>}{detail?.error&&<Alert type="error">{detail.error}</Alert>}
    </Modal>
    <Modal visible={modal==='team'} onDismiss={()=>setModal('')} header="团队管理" footer={<Button onClick={()=>setModal('')}>关闭</Button>}>
      {(form.agents||[]).map((item:Row)=><div key={item.name} style={{marginBottom:16}}>
        <strong>{item.display_name}</strong><p>{item.config.description}</p>
        {item.name!=='supervisor'&&<details><summary>任务能力：{(form.capabilities||[]).filter((c:Row)=>c.expert===item.name&&c.available).length} 项可用</summary>
          <ul>{(form.capabilities||[]).filter((c:Row)=>c.expert===item.name).map((c:Row)=><li key={c.id}>{c.description}{!c.available&&'（暂不可用）'}</li>)}</ul>
        </details>}
        <StatusIndicator type={item.enabled?'success':'stopped'}>{item.enabled?'已启用':'已停用'}</StatusIndicator>
        {item.name!=='supervisor'&&<Button onClick={()=>void run(async()=>{
          await api('/agents/'+item.name,{approve:!item.enabled},'PATCH');
          const [agents,capabilities]=await Promise.all([api('/agents'),api('/capabilities')]);setForm({agents,capabilities});await refresh();
          if(item.name===agentName&&item.enabled)await enter('supervisor');
        })}>{item.enabled?'停用':'启用'}</Button>}
      </div>)}
    </Modal>
    {error&&<div className="toast"><Alert type="error" dismissible onDismiss={()=>setError('')}>{error}</Alert></div>}
  </div>;
}
