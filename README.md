# CloudOps：AWS 中国区多 Agent 运维助手

基于 Amazon Bedrock AgentCore 和 Strands Agents 的云运维参考实现。协调员根据用户请求分派专家，通过只读 MCP 工具查询 AWS 资源、指标、审计事件和费用，汇总带工具证据的结果。

## 架构

```mermaid
flowchart LR
    U[浏览器] --> W[EC2：Web / 控制 API / Worker]
    W <--> D[(DynamoDB：任务与状态)]
    W <--> S[(S3：历史与检查点)]
    W --> R[AgentCore Runtime：协调员与五位专家]
    R --> M[OpenAI 兼容模型服务]
    R --> G[AgentCore Gateway：IAM / MCP]
    G --> L[八组 Lambda 工具]
    L --> A[AWS 中国区只读 API]
    W --> O[CloudWatch / X-Ray]
    R --> O
```

| Agent | 查询范围 |
| --- | --- |
| 协调员 | 能力选择、任务依赖、专家分派和结果汇总 |
| Monitoring | 资源发现、CloudWatch 指标与告警 |
| CloudTrail | 管理操作审计、分页与事件筛选 |
| FinOps | Cost Explorer 费用、预测与公开价格 |
| AgentCore | Runtime、Gateway 等平台配置 |
| Terraform | Provider 文档与基础设施方案 |

默认部署：宁夏区 `cn-northwest-1`、六个 Runtime、一个 Gateway、八组 Lambda Target、四个 EC2 容器。Terraform plan/apply 默认关闭，预置定时任务默认停用；用户可在页面创建并启用定时任务。模型通过 OpenAI 兼容 HTTPS 接口调用，密钥存放在 Secrets Manager。

## 中国区功能替代

本项目使用 Runtime、Gateway 和 Observability。对中国区不提供的托管 Memory、Harness、Registry、Policy 和 Evaluations，分别使用 DynamoDB/S3、应用任务调度、能力清单、IAM/代码校验及本地测试和云端验收；替代范围见[架构说明](docs/01-architecture.md)。服务支持范围以 [AWS 中国区官方说明](https://docs.amazonaws.cn/en_us/aws/latest/userguide/bedrock-agentcore.html)为准。

## 部署、测试与清理

| 步骤 | 文档 | 完成标准 |
| --- | --- | --- |
| 1 | [架构与中国区功能替代](docs/01-architecture.md) | 理解组件、权限和应用实现范围 |
| 2 | [环境准备](docs/02-prerequisites.md) | Linux、AWS 身份、Docker 和模型端点可用 |
| 3 | [部署](docs/03-deployment.md) | Runtime / Target READY，Web 可以登录 |
| 4 | [测试与验收](docs/10-testing.md) | 单专家、协作、持久化与只读边界通过 |
| 5 | [清理资源](docs/11-cleanup.md) | 按依赖删除项目资源并检查残留 |

部署入口：

```bash
git clone https://github.com/zjinsong/CloudOps-AgentCore-China.git
cd CloudOps-AgentCore-China
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python scripts/init_config.py
```

随后按[部署指南](docs/03-deployment.md)创建模型 Secret 和 ECR 仓库，填写私有配置，再依次执行 `foundation`、`gateway`、`runtimes` 阶段。脚本不创建 EC2；完整部署需要云端资源和可访问的模型服务。

## 页面功能

Web 提供 CloudOps 工作区和观测指标两个入口，包含以下功能：

| 页面 / 功能 | 内容 |
| --- | --- |
| 专家对话与协作 | 单独向专家提问，或由协调员选择专家；支持显式指定专家、并行分工和上游结果传递 |
| 团队管理 | 查看专家职责、启用状态和任务能力可用性；启用或停用专家 |
| 会话历史与任务进度 | 保存对话、任务状态和专家结果；刷新后继续查看，支持从任务或回答进入调用链 |
| 定时任务 | 创建、编辑、启停和删除任务，配置执行时间、时区及重复周期，查看运行记录与结果；预置任务默认停用 |
| 观测指标 → 服务指标 | 查看各 Agent Runtime 和共享 Gateway 的调用、错误、延迟等指标；支持时间范围筛选、手动刷新和每分钟自动刷新 |
| 观测指标 → 任务调用链 | 按时间、状态和专家筛选任务，分页查看详情；展示 Task ID、Trace ID、总耗时、执行尝试次数及阶段 / Span 时间线 |

### 服务指标与 Trace / Span

- **服务指标**：Runtime 展示调用次数、错误、延迟、运行或连接时长，以及 CPU（vCPUh）和内存（GBh）用量；Gateway 展示调用、错误、限流、延迟和 Target 执行时间。协调员使用 WebSocket，其连接指标与专家 HTTP 请求指标分别展示。
- **Trace**：通过 Trace ID 关联同一业务任务的调用过程，结合任务记录定位规划、分派、专家执行和汇总阶段。
- **Span**：表示专家、模型或 MCP 调用的执行片段，记录父子关联、时间和错误状态；页面以时间线展示并行与串行调用，区分任务阶段记录和 SDK Span。

服务指标随应用部署提供；模型和工具的 SDK Span 需要完成遥测配置。CPU / 内存数值为资源用量，非实时利用率百分比。配置及排障见[观测与排障](docs/07-observability.md)，协作机制见[多 Agent 调度](docs/06-harness.md)，功能验收见[测试与验收](docs/10-testing.md)。

## 使用范围

本项目提供应用账号登录和用户记录隔离；企业 SSO、租户级 AWS 权限隔离、自动答案语义评分和 Terraform 执行角色不包含在默认部署中。完整部署是否可用，应按测试指南在目标账号验收；本地测试不替代云端验证。

[技术文档](docs/README.md) · [贡献指南](CONTRIBUTING.md) · [安全报告](SECURITY.md) · [来源与许可](NOTICE.md)
