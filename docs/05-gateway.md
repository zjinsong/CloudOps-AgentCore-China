# Gateway 与工具

Gateway 使用 `AWS_IAM` 入站鉴权，将八组 Lambda 暴露为 MCP 工具。专家使用 Runtime Role 的 SigV4 签名执行 `tools/list` 和 `tools/call`。

## 权限链

| 调用方向 | 身份 | 权限范围 |
| --- | --- | --- |
| Runtime → Gateway | Runtime Role | 本项目 Gateway ARN |
| Gateway → Lambda | Gateway Role | 本项目工具 Lambda ARN |
| Lambda → AWS API | 工具 Lambda Role | 只读动作及支持区域 |

工具 Role 权限见 [scripts/permissions.py](../scripts/permissions.py)。共享 MCP 适配器还有 API 白名单和参数校验，模型提示词不承担授权职责。

## 工具清单

| Target | 实现 | 用途 |
| --- | --- | --- |
| cost-explorer | 自定义 Lambda | 费用与预测 |
| cloudwatch | 自定义 Lambda | 指标查询与发现 |
| pricing | AWS Pricing MCP | 公开价格 |
| cloudtrail-mcp | AWS CloudTrail MCP | 审计筛选与分页 |
| terraform-mcp | Terraform MCP | Provider 文档和基础设施资料 |
| health-events | 自定义 Lambda | 本项目归档的 Health 事件 |
| aws-api | AWS API MCP | EC2、Lambda 等资源只读查询 |
| agentcore-mcp | AgentCore MCP | 平台配置与文档 |

每个目录中的 `tools.json` 定义工具名和参数 Schema；Handler 执行查询并返回结果。共享 Handler 从 Gateway 的 `context.client_context.custom` 获取工具名，按对应 MCP 工具路由。

Health 数据由 EventBridge 写入项目存储，只包含归档入口收到的事件，不是完整账号历史。CloudTrail 查询须处理分页并注明时间、区域、过滤条件和截断范围。指标查询须使用资源 ID 与正确维度；无数据不等于零。

## 工具更新

修改 Schema、Handler、IAM 及 `control/capabilities.py` 的映射，构建对应 Layer，再执行：

```bash
python scripts/deploy.py gateway --apply
python scripts/deploy.py runtimes --apply
python scripts/validate_cloud.py
```

Runtime 阶段在不加 `--update` 时复用已登记的 Runtime，并重新枚举工具、校验能力。工具变更须完成[业务验收](10-testing.md)。

[Gateway 官方文档](https://docs.amazonaws.cn/en_us/bedrock-agentcore/latest/devguide/gateway.html)
