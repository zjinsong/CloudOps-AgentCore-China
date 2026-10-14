"""Minimal HTTP Runtime: verifies the service contract without a model or tools."""
from bedrock_agentcore.runtime import BedrockAgentCoreApp
app=BedrockAgentCoreApp()

@app.entrypoint
def hello(payload):
    return {'message':'你好，AgentCore Runtime','received':payload.get('prompt','')}

if __name__=='__main__':app.run()
