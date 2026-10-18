# Runtime 接口

## CloudOps 入口

| 服务 | 协议 | 代码 |
| --- | --- | --- |
| 协调员 | Runtime HTTP/WebSocket | `agents/supervisor/ws_main.py` |
| 专家 | Runtime A2A，容器端口 9000 | `agents/leaf/a2a_main.py` |
| 业务入口 | Web → 控制 API → 持久任务 → Worker | `control/api.py`、`control/worker.py` |

CloudOps 请求需要持久任务、用户、执行租约及检查点上下文。业务测试应使用 Web；直接向专家发送任意 `prompt` JSON 不满足应用协议。

Runtime 会话空闲超时默认 900 秒，最大生命周期 28,800 秒；DynamoDB/S3 独立保存任务及历史。平台版本 `V2` 与每次发布产生的配置版本号不同。

## 本地 HTTP 示例（可选）

此示例仅用于验证 Runtime HTTP 服务契约，不部署云资源、不调用模型，也不属于 CloudOps 部署步骤。

```bash
source .venv/bin/activate
python examples/hello/app.py
```

在另一个终端执行：

```bash
curl -fsS http://localhost:8080/ping
curl -fsS http://localhost:8080/invocations \
  -H 'Content-Type: application/json' -d '{"prompt":"hello"}'
```

预期健康检查成功、请求返回输入回显。示例源文件为 [examples/hello/app.py](../examples/hello/app.py)。HTTP 服务契约要求监听 `0.0.0.0:8080`，提供 `/ping` 和 `/invocations`；详见 [Runtime 服务契约](https://docs.amazonaws.cn/en_us/bedrock-agentcore/latest/devguide/runtime-service-contract.html)。

[部署](03-deployment.md) · [测试](10-testing.md)
