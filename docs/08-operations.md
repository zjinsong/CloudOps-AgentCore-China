# 八、更新、费用与清理

## 发布更新

代码更新后先执行本地验证，再构建新的不可变 tag：

```bash
# 修改 .state/deployment.json 的 image_uri：使用新的 tag
bash scripts/build_runtime.sh
python scripts/deploy.py runtimes --apply --update
python scripts/validate_cloud.py
docker compose build control frontend
docker compose up -d
```

每次 Runtime 更新都会产生新的配置版本。`--update` 是显式发布动作，不是健康检查；脚本会更新六个 Runtime。已有会话可能继续使用旧版本，新会话走 DEFAULT。发布后用新会话做验收。

平台 V2 是运行平台，不是配置版本“2”。SDK 太旧可能从解析结果丢掉 `platformVersion`，本脚本通过原始响应补回该字段。不要因 SDK 没返回字段就判断中国区没有 V2。

回退镜像与 Runtime 配置，不会自动回退 DDB Schema、IAM、工具、提示词或 Secret。保留每次发布的镜像、代码 commit、模型名、工具 Schema 和权限变更记录；涉及数据结构时先做兼容迁移。

## 冷启动和长连接

V2 从已准备的快照恢复环境，减少每个实例重复初始化；它不会消除网络、模型推理和工具延迟。不要承诺一个固定冷启动数字，应分别测 Runtime 连接、首次模型请求和整个任务耗时。

快照前不能冻结请求唯一 ID、过期凭证或特定用户状态。健康检查应在初始化完成后就绪；请求 ID、会话信息和短期令牌在请求阶段生成/刷新。

## 费用怎么看

本应用费用来自 EC2/EBS、Runtime 的资源运行时间、Gateway 调用、Lambda、DDB、S3、CloudWatch/X-Ray、网络和外部模型。Runtime 与模型不是同一项账单。

```text
日成本 ≈ EC2 和磁盘全天成本
       + Runtime 实际资源使用
       + 模型输入/输出 token
       + 工具调用及 Lambda 执行
       + DDB、S3、日志和网络
```

同样“100 次任务”，计划步骤、工具数量和 token 可以差很多。先在测试账号跑固定题集，记录日用量与账单，再估算客户演示预算。此仓库不写一个无法复现的固定每日价格。

查看当前[中国区 AgentCore 价格](https://www.amazonaws.cn/bedrock/agentcore/pricing/)，以及模型供应商价格。Identity 费用与本项目无 OAuth/API Key Provider 的模式应分开分析，不能把 API Key 请求价格当成用户登录费。

控制建议：关闭默认定时任务；限制分段预算；只查询必要资源；日志保留 30 天；任务摘要查询使用索引；监控 DDB/CloudWatch 费用；设置账号预算告警。调度用 active 稀疏索引轮询，未部署 SQS；如果增加队列，它仍只能做唤醒提示，DDB 是状态依据。

## 停止与清理

临时停止 Web：

```bash
docker compose down
```

它只停止本机容器，不删除云资源。结束测试时按自己的私有资源清单逐项清理：

1. 停止新任务和定时任务，等待或取消已有任务。
2. 停止仍活跃的 Runtime 会话，再删除测试 Runtime。
3. 删除 Gateway Targets、Gateway、项目 Lambda 与 Layer、Health EventBridge rule/target。
4. 确认不再需要历史后，清理自己创建的 S3 对象/版本、DDB 表、ECR 镜像与仓库、模型 Secret。
5. 解除测试 EC2 instance profile，删除项目 IAM policy/role/profile；停止或删除测试 EC2/EBS。
6. 检查相关日志保留期与账单。删除 Runtime 不等于共享日志立即消失。

只删除 `Project` 和 `Repository` Tag 对应的本项目资源。`aws/spans` 和 X-Ray 目的地可能被其他应用共享，不得作为普通项目资源盲删。
