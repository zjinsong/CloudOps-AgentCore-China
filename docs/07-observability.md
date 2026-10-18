# 观测与排障

观测页面关联服务指标、DynamoDB 任务记录和 ADOT Span。任务 ID 标识业务任务，Trace/Span 表示模型、专家和 MCP 调用关系；并行 Span 的时长不能直接相加作为任务总耗时。

## 配置遥测

项目使用 ADOT SDK，不部署 Collector。SDK 通过临时 AWS 凭证向 X-Ray OTLP 端点发送遥测，Trace 目的地为 CloudWatch Logs `aws/spans`。

以下操作修改账号/区域级 Trace 目的地和日志资源策略。先保存当前配置，并确认同账号其他应用允许该调整：

```bash
aws xray get-trace-segment-destination --region cn-northwest-1
export PROJECT=$(python -c 'import json; print(json.load(open(".state/deployment.json"))["project"])')
python scripts/provision_task_observability.py \
  --table "$PROJECT-tasks" --roles "$PROJECT-runtime" "$PROJECT-web"
```

使用部署身份执行。首次产生 Span 后再次运行脚本，为服务创建的 `aws/spans` 设置 30 天保留期。默认仅采集任务元数据，不采集 prompt、工具输入和结果正文。

通过标准：观测页面可按时间范围选择任务，显示专家、模型和工具调用；Span 可关联任务 ID。配置参考 [AWS 中国区观测文档](https://docs.amazonaws.cn/bedrock-agentcore/latest/devguide/observability-configure.html#observability-configure-3p)。

## 排障

```bash
python scripts/validate_cloud.py
docker compose ps
docker compose logs --tail 100 control worker observer
```

| 现象 | 检查项 |
| --- | --- |
| 等待执行 | Worker 状态、任务租约、active-index、最后更新时间 |
| Runtime 连接失败 | READY、DEFAULT 配置版本、IAM/SigV4、协议与网络 |
| 模型请求超时 | 模型服务、Runtime 网络连通性、执行预算 |
| 工具缺失 | Gateway 实际清单、能力映射、专家工具白名单 |
| CloudTrail 结果不完整 | EventName、时间窗、区域、分页与截断 |
| 指标为空 | 资源 ID、维度、统计口径、时间范围与读取权限 |
| 无 Span | 任务 trace_id、SDK 开关、X-Ray 目的地、日志权限 |
| 重启后无法恢复 | DynamoDB 任务记录、执行 token、S3 检查点 |

协调员主要使用 WebSocket，HTTP Invocation/Latency 不代表每条业务消息；耗时以任务记录和关联 Span 为准。公开反馈错误前移除账号、资源标识和业务数据。
