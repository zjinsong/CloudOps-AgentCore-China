# 四、第一个 Runtime 怎么调用

Runtime 托管的是一个服务程序。先在本地验证程序的接口，再上传镜像，最后通过 AgentCore 的数据面 API 调用。

## 先跑一个不依赖模型的 Agent

```bash
source .venv/bin/activate
python examples/hello/app.py
```

另开终端：

```bash
curl -s http://localhost:8080/ping
curl -s http://localhost:8080/invocations \
  -H 'Content-Type: application/json' -d '{"prompt":"你好"}'
```

源文件见 [hello/app.py](../examples/hello/app.py)。这个例子回显输入，用来验证接口，尚未调用模型。

HTTP 模式的服务监听 `0.0.0.0:8080`，实现健康检查 `/ping` 和请求入口 `/invocations`。端口、路由和 payload 由[Runtime 服务契约](https://docs.amazonaws.cn/en_us/bedrock-agentcore/latest/devguide/runtime-service-contract.html)约束；把普通 CLI 放进容器，并不会自动变成 Agent 服务。

## 上传并创建一个独立的 Hello Runtime

完成[部署指南](03-deployment.md)第 4 步后，可以复用本项目 Runtime Role，创建同前缀的 Hello Runtime。先创建独立 ECR 仓库：

```bash
aws ecr create-repository --repository-name cloudops-ref-hello --image-tag-mutability IMMUTABLE
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export HELLO_IMAGE="$AWS_ACCOUNT_ID.dkr.ecr.cn-northwest-1.amazonaws.com.cn/cloudops-ref-hello:v1"
aws ecr get-login-password --region cn-northwest-1 | \
  docker login --username AWS --password-stdin "$AWS_ACCOUNT_ID.dkr.ecr.cn-northwest-1.amazonaws.com.cn"
docker buildx build --platform linux/arm64 --provenance=false --push \
  -t "$HELLO_IMAGE" examples/hello
```

当前 Runtime Role 的 ECR 读取权限仅覆盖主镜像仓库，因此 **需要先给该 Role 添加 Hello 仓库的三项镜像读取权限**：`ecr:BatchGetImage`、`ecr:GetDownloadUrlForLayer`、`ecr:BatchCheckLayerAvailability`，资源只限 Hello 仓库 ARN。不要扩大成所有 ECR 仓库。

使用下面的 Python 创建；`project` 名称读取私有配置，匹配角色的信任范围：

```python
import json, os, boto3
cfg = json.load(open('.state/deployment.json'))
cp = boto3.client('bedrock-agentcore-control', region_name=cfg['region'])
result = cp.create_agent_runtime(
    agentRuntimeName=cfg['project'].replace('-', '_') + '_hello',
    agentRuntimeArtifact={'containerConfiguration': {'containerUri': os.environ['HELLO_IMAGE']}},
    roleArn=cfg['runtime_role'],
    networkConfiguration={'networkMode': 'PUBLIC'},
    protocolConfiguration={'serverProtocol': 'HTTP'},
    tags={'Project': cfg['project'], 'Repository': 'CloudOps-AgentCore-China'},
)
print(result['agentRuntimeArn'])
```

保存返回 ARN，在控制台或用 `get_agent_runtime` 等待 READY 后调用。Hello 不在六个 Agent 的注册清单中；完成练习后停止其会话并删除这个测试 Runtime 和独立镜像仓库。

## 云上调用：两个 session

将下列代码保存为本地私有文件，或在终端运行。`HELLO_RUNTIME_ARN` 填自己刚创建的 ARN，调用身份须有该 Runtime 的 `InvokeAgentRuntime` 权限。

```bash
export HELLO_RUNTIME_ARN='<YOUR_HELLO_RUNTIME_ARN>'
python - <<'PY'
import boto3, json, os, uuid
client = boto3.client('bedrock-agentcore', region_name='cn-northwest-1')
session_a = str(uuid.uuid4())
session_b = str(uuid.uuid4())
for session_id, prompt in [(session_a, '会话 A 第一次'),
                           (session_a, '会话 A 第二次'),
                           (session_b, '会话 B 第一次')]:
    result = client.invoke_agent_runtime(
        agentRuntimeArn=os.environ['HELLO_RUNTIME_ARN'],
        qualifier='DEFAULT', runtimeSessionId=session_id,
        payload=json.dumps({'prompt': prompt}, ensure_ascii=False).encode('utf-8'),
    )
    print(session_id, result['response'].read().decode('utf-8'))
PY
```

相同 session ID 让请求进入同一个会话；不同 ID 使用独立会话。**session 不会自动让回显程序记住对话**，记忆需要应用代码保存。生产应用也不能把 Runtime 会话的寿命当成业务历史保留期。

## CloudOps 为什么不能直接照抄 Hello payload

协调员是 WebSocket 服务，专家是 A2A 服务。CloudOps 还要求任务存在于 DDB，并有执行租约、用户、trace 和检查点上下文。向专家发送 `{"prompt":"查 EC2"}` 不满足它的应用协议。

正确业务入口是 Web → 控制 API → 持久任务 → Worker → 专家。专家的 A2A JSON-RPC 请求由 [control/worker.py](../control/worker.py) 构建；协调员连接由 [control/api.py](../control/api.py) 和 [control/ws.py](../control/ws.py)管理。HTTP、WebSocket 和 A2A 是传输协议选择，harness 是协议之上的任务控制。

[下一章：Gateway](05-gateway.md)
