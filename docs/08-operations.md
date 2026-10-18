# 更新与运维

## 发布

先通过[本地检查](10-testing.md)，修改 `.state/deployment.json` 的 `image_uri`，使用新的不可变 tag，然后执行：

```bash
bash scripts/build_runtime.sh
python scripts/deploy.py runtimes --apply --update
python scripts/validate_cloud.py
docker compose build control frontend
docker compose up -d
```

`--update` 更新六个 Runtime 并产生新的配置版本，不用于健康检查。新会话使用 DEFAULT；已有会话可能继续使用旧版本，发布后用新会话完成业务验收。

工具或权限变更需同时更新 Schema、Handler、IAM 和能力映射；重新构建对应 Layer 后运行 `python scripts/deploy.py gateway --apply`。该阶段会重新发布 Layer 版本。

## 回退

将 `image_uri` 改为已保留的旧镜像 tag，再执行上述 Runtime 更新。Web 使用对应代码重新构建。镜像回退不回退 DynamoDB 数据结构、工具配置、IAM 或 Secret；涉及这些资源的变更需单独处理。

## 运行检查

```bash
python scripts/validate_cloud.py
docker compose ps
docker compose logs --tail 100 control worker observer
```

任务失败先检查状态、专家结果和工具错误；连接失败检查 READY、DEFAULT、IAM、网络和模型端点。详细步骤见[观测与排障](07-observability.md)。

## 费用与保留

费用包括 EC2/EBS、Runtime、Gateway、Lambda、DynamoDB、S3、日志、网络和外部模型。默认关闭定时任务自动执行，历史 TTL 和 S3 生命周期默认 30 天；工作桶启用版本控制。TTL 异步删除，不能用于即时清理。

按实际任务量、模型 token、工具调用和日志量设置预算。服务单价见 [AWS 中国区 AgentCore 价格](https://www.amazonaws.cn/agentcore/pricing/)。

## 停止与删除

`docker compose down` 仅停止本机服务，不删除云资源。结束测试后按[清理资源](11-cleanup.md)删除项目资源。
