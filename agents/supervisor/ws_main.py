"""AgentCore WS transport; durable coordination is shared with control workers."""
import asyncio
import hmac
import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from control.ws import serve

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload):
    # Migration HTTP entry uses the exact same task contract; no second router.
    from control.ws import dispatch
    if not hmac.compare_digest(str(payload.get('bridge_token', '')), os.environ['CLOUDOPS_BRIDGE_SECRET']):
        return {'error': '未授权'}
    return dispatch(payload.get('conversation_id', ''), payload, payload.get('user_id', ''))


@app.websocket
async def connection(ws, context):
    await ws.accept()
    try:
        hello = await asyncio.wait_for(ws.receive_json(), 10)
        if not hmac.compare_digest(str(hello.get('bridge_token', '')), os.environ['CLOUDOPS_BRIDGE_SECRET']):
            await ws.close(code=1008)
            return
        await serve(ws, hello.get('conversation_id', ''), hello.get('user_id', ''), lambda: hello.get('expires_at', 0) > __import__('time').time())
    except (ValueError, TimeoutError):
        await ws.close(code=1008)


if __name__ == '__main__':
    app.run()
