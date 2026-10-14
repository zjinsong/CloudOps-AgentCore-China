export async function api(path: string, body?: unknown, method?: string) {
  const r = await fetch('/api' + path, { method: method || (body === undefined ? 'GET' : 'POST'),
    headers: { 'Content-Type': 'application/json', 'X-CloudOps-CSRF': '1' },
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (!r.ok) {
    const text = await r.text();
    let detail = text;
    try { detail = JSON.parse(text).detail; } catch { /* plain error */ }
    throw new Error(r.status === 401 ? '需要登录' : detail);
  }
  return r.json();
}

export class ConversationSocket {
  ws?: WebSocket;
  stopped = false;
  timer?: ReturnType<typeof setTimeout>;
  heartbeat?: ReturnType<typeof setInterval>;
  pending = new Map<string, object>();
  chunks = new Map<string, string[]>();
  attempts = 0;
  constructor(public conversation: string, public receive: (value: any) => void, public status: (s: string) => void) { this.connect(); }
  connect() {
    if (this.stopped) return;
    this.status('连接中');
    this.chunks.clear();
    this.ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/ws/${encodeURIComponent(this.conversation)}`);
    this.ws.onmessage = e => {
      let data = JSON.parse(e.data);
      if (data.type === 'wire.chunk') {
        const id = data.id;
        const parts = this.chunks.get(id) || Array<string>(data.total);
        parts[data.index] = data.data; this.chunks.set(id, parts);
        if (parts.filter(x => x !== undefined).length !== data.total) return;
        data = JSON.parse(parts.join('')); this.chunks.delete(id);
      }
      if (data.type === 'session.ready') {
        this.attempts = 0; this.status('已连接');
        this.pending.forEach(v => this.send(v));
        clearInterval(this.heartbeat);
        // Reconcile chat even if the final event arrived before an index update.
        // sync also keeps the connection alive and rechecks authorization.
        this.heartbeat = setInterval(() => {
          if (this.ws?.readyState === WebSocket.OPEN) this.send({ type: 'sync' });
        }, 5000);
      }
      if (data.type === 'message.ack') this.pending.delete(data.message_id);
      this.receive(data);
    };
    this.ws.onclose = e => {
      clearInterval(this.heartbeat);
      if (this.stopped) return;
      this.status(e.code === 1008 ? '登录或授权已失效' : '连接中断，重连中');
      if (e.code !== 1008) this.timer = setTimeout(() => this.connect(), Math.min(15000, 1000 * 2 ** this.attempts++));
    };
    this.ws.onerror = () => this.status('连接失败');
  }
  send(value: any) {
    if (this.ws?.readyState !== WebSocket.OPEN) throw new Error('连接尚未就绪，请稍后重试');
    this.ws.send(JSON.stringify(value));
  }
  submit(text: string, owners: string[] = [], file_ids: string[] = []) {
    const message_id = crypto.randomUUID();
    const value = { type: 'message.submit', message_id, text, owners, file_ids };
    this.send(value); this.pending.set(message_id, value);
  }
  close() { this.stopped = true; clearTimeout(this.timer); clearInterval(this.heartbeat); this.ws?.close(); }
}

export class Microphone {
  stream?: MediaStream;
  ctx?: AudioContext;
  node?: AudioWorkletNode;
  constructor(public socket: ConversationSocket) {}
  async start() {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('麦克风需要 HTTPS 或 localhost 页面');
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    try {
      this.ctx = new AudioContext(); await this.ctx.resume();
      await this.ctx.audioWorklet.addModule('/pcm-worklet.js');
      this.node = new AudioWorkletNode(this.ctx, 'pcm16');
      this.node.port.onmessage = e => {
        if (this.socket.ws?.readyState === WebSocket.OPEN) this.socket.ws.send(e.data);
      };
      const gain = this.ctx.createGain(); gain.gain.value = 0;
      this.ctx.createMediaStreamSource(this.stream).connect(this.node).connect(gain).connect(this.ctx.destination);
    } catch (e) { this.stop(); throw e; }
  }
  stop() { this.node?.disconnect(); this.stream?.getTracks().forEach(t => t.stop()); const ctx = this.ctx; this.ctx = undefined; if (ctx && ctx.state !== 'closed') void ctx.close().catch(() => {}); this.node = undefined; this.stream = undefined; }
}
