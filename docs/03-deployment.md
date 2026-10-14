# 三、逐步构建 CloudOps

下面不是“一键脚本然后结束”。先知道创建什么，再逐步执行，每一步失败都可以检查私有状态后重试。

所有命令在 **Linux 仓库根目录**执行。脚本只支持宁夏区。`--apply` 表示真正创建或更新资源；不带它只打印阶段名称，**不是完整资源差异预览**。

## 1. 下载代码并初始化

```bash
git clone https://github.com/zjinsong/CloudOps-AgentCore-China.git
cd CloudOps-AgentCore-China
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python scripts/init_config.py
```

产生两个私有文件：`.state/deployment.json` 保存部署配置，`.secrets/web.json` 保存随机登录密码和签名密钥。这两个目录均被 Git 忽略。

部署脚本不会创建 EC2，也不会修改原有应用。请使用新的项目名前缀；相同名称的既有资源如果没有本仓库的归属 Tag，脚本会拒绝接管。

## 2. 准备模型密钥和镜像仓库

创建 ECR 仓库，存放 Runtime 镜像。以下使用示例项目名 `cloudops-ref`：

```bash
export AWS_DEFAULT_REGION=cn-northwest-1
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
aws ecr create-repository --repository-name cloudops-ref-runtime \
  --image-tag-mutability IMMUTABLE --image-scanning-configuration scanOnPush=true
```

如果仓库已经存在，确认是自己为本项目创建的，跳过创建。每次发布使用新 tag，例如 `v1`、`v2`，不要覆盖旧镜像。

把模型密钥写入自己的私有文件：

```bash
cp examples/model-secret.example.json .secrets/model.json
chmod 600 .secrets/model.json
nano .secrets/model.json
aws secretsmanager create-secret --name cloudops-ref/model \
  --secret-string file://.secrets/model.json
```

`api_key` 替换成真实密钥。记录命令返回的 Secret ARN，填入下一步私有配置，不要贴入 Issue。

## 3. 填写部署配置

```bash
nano .state/deployment.json
```

| 字段 | 填什么 |
| --- | --- |
| project | 新项目前缀，例如 `cloudops-ref`；后续不要改 |
| region | `cn-northwest-1` |
| model_id | 模型服务实际支持的模型名 |
| model_base_url | 批准的 HTTPS OpenAI 兼容地址，通常含 `/v1` |
| model_secret_arn | 第 2 步的 Secret ARN |
| image_uri | 自己账号的 ECR URI，镜像 tag 使用 `v1` |

ECR URI 可以在本地生成：

```bash
printf '%s\n' "$AWS_ACCOUNT_ID.dkr.ecr.cn-northwest-1.amazonaws.com.cn/cloudops-ref-runtime:v1"
```

请先在自己的网络中确认模型支持 tool calling；Runtime 的 PUBLIC 网络不会自动继承 EC2 的本地代理。

## 4. 建立任务存储与角色

```bash
python scripts/deploy.py foundation
python scripts/deploy.py foundation --apply
```

这一步创建按需计费 DynamoDB 表、查询索引、TTL 和时间点恢复；创建两个私有 S3 桶；创建 Runtime Role、Web EC2 Role 和 instance profile。

账号、桶名和 Role ARN 自动写回 `.state/deployment.json`。S3 禁止公开访问、使用服务端加密，并要求 TLS。历史对象和 DDB 历史默认保留 30 天；TTL 删除是异步的，不是准确到秒的清理器。

这个阶段还没有创建 Agent，模型镜像也可以尚未上传。执行身份需要访问模型 Secret 所在账号，但本步骤不打印密钥。

## 5. 构建工具 Layer 和 Runtime 镜像

```bash
python scripts/build_mcp_lambda_layers.py
python scripts/build_inspection_layers.py
bash scripts/build_runtime.sh
```

前两条命令将 MCP 依赖分别打包到 `.state/mcp-lambda-layers/`。它们需要 Linux x86_64、Python 3.12 和网络；第一条还会从 Docker 镜像提取 Terraform MCP 服务的文档工具二进制。

最后一条读取私有配置，登录 ECR 并构建、推送 ARM64 镜像。它不运行 Agent。Layer 的总解压体积、包版本和 Lambda 配额要在升级依赖时重新确认。

## 6. 创建 Gateway 和工具

```bash
python scripts/deploy.py gateway
python scripts/deploy.py gateway --apply
```

产生一个 `AWS_IAM` MCP Gateway、八组 Lambda Target、各自的只读执行角色和 Layer；同时建立当前账号 AWS Health 事件的 EventBridge 归档入口。

Gateway 就绪后，脚本将 Runtime 和 Web 的调用权限收窄到这个 Gateway 的 ARN。`gateway_url`、Target 函数和 Layer ARN 会保存在私有状态里。每个 Target 都要达到 READY；失败时查看相关 Lambda 日志及 Target 的失败原因。

Health 工具查询本项目收到并归档的事件，不是完整 AWS Health 历史库。没有历史事件不代表账号从未发生服务事件。

## 7. 创建六个 Runtime

```bash
python scripts/deploy.py runtimes
python scripts/deploy.py runtimes --apply
python scripts/validate_cloud.py
```

协调员使用 HTTP/WebSocket 协议，专家使用 A2A。它们共用镜像，通过环境变量选择入口。脚本等待 READY，核对平台 V2，登记 Runtime 到任务表，再从 Gateway 获取工具清单，确认能力映射可用。

默认会话空闲超时 900 秒，最大生命周期 28,800 秒；这不是整个业务任务的截止时间。任务持久化在 DDB/S3，重建会话需要由 harness 恢复上下文。

最终生成 `.secrets/control.env`，供 Web、Worker、Observer 使用。`validate_cloud.py` 是只读检查，会确认 DEFAULT Endpoint 指向已记录的配置版本以及八个 Target 就绪。

## 8. 在 EC2 启动 Web 与 Worker

把第 4 步生成的 instance profile 附加到运行 Web 的 EC2。已有 profile 不要直接覆盖，先评估与原应用的角色关系。推荐使用独立测试 EC2。

容器需要通过 EC2 metadata 获取临时凭证，IMDSv2 应开启；Docker bridge 环境通常需要 metadata hop limit 为 2。在 EC2 控制台设置，或使用部署身份：

```bash
export INSTANCE_ID='<YOUR_TEST_INSTANCE_ID>'
export PROFILE_NAME=$(python -c 'import json; print(json.load(open(".state/deployment.json"))["instance_profile"])')
aws ec2 associate-iam-instance-profile --instance-id "$INSTANCE_ID" \
  --iam-instance-profile "Name=$PROFILE_NAME"
aws ec2 modify-instance-metadata-options --instance-id "$INSTANCE_ID" \
  --http-tokens required --http-put-response-hop-limit 2
docker compose build control frontend
docker compose up -d
docker compose ps
docker compose logs --tail 50 control worker observer
```

已有 profile 使用替换接口，不要重复 associate。若仍在 shell 环境保留部署管理员凭证，先清除，再验证 EC2 Role 生效；不要将它们写入 `control.env`。

Web 只绑定 EC2 回环地址 `127.0.0.1:3001`。在自己的电脑建立隧道：

```bash
ssh -N -L 3100:127.0.0.1:3001 -i ~/.ssh/your_key ubuntu@<YOUR_EC2_HOST>
```

打开 `http://localhost:3100`。用户名是 **admin**，密码在 EC2 的 `.secrets/web.json` 中，字段 `ADMIN_PASSWORD`。只在自己的终端查看：

```bash
python -c 'import json; print(json.load(open(".secrets/web.json"))["ADMIN_PASSWORD"])'
```

公网入口需要另行配置 HTTPS 和 `COOKIE_SECURE=true`，不能把此 HTTP 演示入口直接开放公网。

## 9. 做一次真实验收

先向 Monitoring 发一个简单只读查询，再给协调员[协作任务](06-harness.md)。确认界面有进度、任务最终结束、专家结果能追溯工具证据、刷新后历史仍在。观测调用链需要完成[遥测配置](07-observability.md)。

遇到问题先读[排障](07-observability.md)；不要只重复 `--update`，它会产生新配置版本。部署代码集中在 [scripts/deploy.py](../scripts/deploy.py)，前面每一步与该文件的 foundation、gateway、runtimes 三个阶段对应。
