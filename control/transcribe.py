"""China-region Transcribe WebSocket with AWS EventStream binary framing."""
import asyncio
import json
import os
import struct
import time
import zlib
from urllib.parse import urlencode

import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
import websockets


def frame(payload):
    headers = b''
    for name, value in ((':content-type', 'application/octet-stream'), (':event-type', 'AudioEvent'), (':message-type', 'event')):
        key, val = name.encode(), value.encode()
        headers += bytes([len(key)]) + key + b'\x07' + struct.pack('>H', len(val)) + val
    prelude = struct.pack('>II', 16 + len(headers) + len(payload), len(headers))
    packet = prelude + struct.pack('>I', zlib.crc32(prelude) & 0xffffffff) + headers + payload
    return packet + struct.pack('>I', zlib.crc32(packet) & 0xffffffff)


def unframe(packet):
    if len(packet) < 16:
        raise ValueError('转写数据包不完整')
    size, header_len, pre_crc = struct.unpack('>III', packet[:12])
    if size != len(packet) or zlib.crc32(packet[:8]) & 0xffffffff != pre_crc or zlib.crc32(packet[:-4]) & 0xffffffff != struct.unpack('>I', packet[-4:])[0]:
        raise ValueError('转写数据包校验失败')
    return json.loads(packet[12 + header_len:-4])


LANGUAGES = {'zh-CN', 'zh-HK', 'en-US'}


def signed_url(language_code='zh-CN'):
    if language_code not in LANGUAGES:
        raise ValueError('请选择普通话、粤语或 English')
    region = os.getenv('AWS_REGION', 'cn-northwest-1')
    host = f'transcribestreaming.{region}.amazonaws.com.cn:8443'
    params = {'language-code': language_code, 'media-encoding': 'pcm', 'sample-rate': '16000'}
    req = AWSRequest(method='GET', url=f'https://{host}/stream-transcription-websocket?{urlencode(params)}')
    creds = boto3.Session().get_credentials().get_frozen_credentials()
    SigV4QueryAuth(creds, 'transcribe', region, expires=300).add_auth(req)
    return req.url.replace('https://', 'wss://', 1)


class Recognition:
    def __init__(self, send):
        self.send = send
        self.ws = None
        self.reader = None
        self.timer = None
        self.started = 0
        self.bytes = 0

    async def start(self, language_code='zh-CN'):
        if self.ws:
            raise ValueError('录音已开始')
        if language_code not in LANGUAGES:
            raise ValueError('请选择普通话、粤语或 English')
        self.ws = await websockets.connect(await asyncio.to_thread(signed_url, language_code), max_size=1024 * 1024, open_timeout=15)
        self.started = time.monotonic()
        self.reader = asyncio.create_task(self.read())
        self.timer = asyncio.create_task(self.timeout())
        self.bytes = 0
        await self.send({'type': 'audio.ready', 'sample_rate': 16000, 'language_code': language_code})

    async def read(self):
        try:
            async for packet in self.ws:
                body = unframe(packet)
                if 'Transcript' not in body:
                    raise ValueError(body.get('Message', '语音识别服务返回错误'))
                for result in body['Transcript'].get('Results', []):
                    alts = result.get('Alternatives', [])
                    if alts:
                        await self.send({'type': 'transcript.partial' if result.get('IsPartial') else 'transcript.final',
                                         'text': alts[0]['Transcript'], 'result_id': result['ResultId']})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Do not include a presigned URL in an error message.
            await self.send({'type': 'error', 'code': 'transcribe', 'message': f'语音识别失败（{type(e).__name__}）'})
            await self.finish(cancel=True)

    async def timeout(self):
        await asyncio.sleep(60)
        await self.finish()

    async def push(self, data):
        if not self.ws:
            raise ValueError('请先开始录音')
        if len(data) > 8192 or len(data) % 2:
            raise ValueError('音频分块无效')
        self.bytes += len(data)
        if self.bytes > 60 * 32000:
            await self.finish()
            raise ValueError('单次录音最长60秒')
        await self.ws.send(frame(data))

    async def finish(self, cancel=False):
        ws, reader = self.ws, self.reader
        if not ws:
            return
        if self.timer and self.timer != asyncio.current_task():
            self.timer.cancel()
        try:
            if not cancel:
                await ws.send(frame(b''))
                if reader:
                    await asyncio.wait_for(asyncio.shield(reader), 10)
        except (TimeoutError, websockets.ConnectionClosed):
            pass
        finally:
            if reader and reader != asyncio.current_task() and not reader.done():
                reader.cancel()
            await ws.close()
            self.ws = None
            await self.send({'type': 'audio.stopped', 'cancelled': cancel})
