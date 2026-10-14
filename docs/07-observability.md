# 七、观测与排障

观测页面组合两类数据：AWS 服务指标，以及应用自己的任务记录/Span。它们描述不同的事情。

## Trace 和 Span 是什么

一次用户任务对应一条 Trace，例如“检查 EC2”。其中的模型调用、专家执行、MCP 查询各是一个 Span，包含开始、结束、父子关系和错误状态。

```text
任务 Trace
├─ 排队（任务记录）
└─ 执行
   ├─ Monitoring：模型 → EC2 工具 → CloudWatch 工具
   ├─ CloudTrail：模型 → 审计工具
   └─ 协调员汇总
```

并行条和父子条会重叠，不能把所有 Span 时长加起来当总耗时。DDB 保存排队/执行摘要，ADOT SDK 产生 Span，前端关联展示。没有 Span 时也应保留任务记录，避免空白页面误导用户。

## 配置遥测

本项目使用 **ADOT SDK，未使用 ADOT Collector**。SDK 以临时 AWS 凭证向 X-Ray 的 OTLP 端点发送，目的地设为 CloudWatch Logs 的 `aws/spans`。

下面脚本会改变 **账号/区域级 Trace 目的地**，并更新日志资源策略和指定角色权限。先查看现有设置，确认对同账号其他应用的影响：

```bash
aws xray get-trace-segment-destination --region cn-northwest-1
export PROJECT=$(python -c 'import json; print(json.load(open(".state/deployment.json"))["project"])')
python scripts/provision_task_observability.py \
  --table "$PROJECT-tasks" --roles "$PROJECT-runtime" "$PROJECT-web"
```

该脚本需要部署身份。首次发送遥测后再次执行，以确认服务创建的 `aws/spans` 日志组保留期设为 30 天。不要将这里误解成“开启后所有查询内容都进入日志”：`framework/telemetry.py` 默认只发本项目任务的元数据，不发 prompt、工具输入和结果。

参考[中国区第三方 Agent 观测配置](https://docs.amazonaws.cn/bedrock-agentcore/latest/devguide/observability-configure.html#observability-configure-3p)。索引、采样、日志摄取和保存都可能收费，见维护章节。

## 协调员为什么 HTTP invocation 可能是零

协调员主要走 WebSocket。HTTP Invocation/Latency 并不等于每条 WebSocket 业务消息；专家通过 A2A 请求则是另一路。协调员任务耗时应结合应用 task/Span，连接状况看相应服务指标，不能把缺失的 HTTP 指标伪装成零毫秒。

观测页面支持时间范围、自动刷新与手动刷新；任务调用链按时间范围加载，再选择任务查看。具体 API 与默认数量以 `control/api.py` 为准，避免为了展示把所有历史逐条读出。

## 常见排障

| 现象 | 先查什么 |
| --- | --- |
| 一直“等待执行” | Worker 是否活着；任务租约、active-index、最后更新时间 |
| Runtime 连接超时 | READY、DEFAULT 版本、SigV4 权限、网络和请求入口 |
| 模型慢/连接超时 | 目标模型网络、服务状态、请求预算；Runtime PUBLIC 网络独立于 EC2 |
| 选错工具 | Gateway 实际清单、能力映射、专家工具白名单 |
| CloudTrail 噪声多 | 按 EventName、时间和区域筛选；检查分页覆盖 |
| 指标为空 | 区域/维度/时间窗、角色读取权限；无数据不等于零 |
| Span 不显示 | trace_id 关联、SDK 开关、X-Ray 目的地、aws/spans 权限 |
| 重建后忘记任务 | 检查 DDB/S3 记录是否存在；Runtime 临时文件不是持久任务库 |

排障输出可能包含资源标识和业务信息。公开提问前脱敏，不上传私有 `.state`、Secret 或完整业务日志。
