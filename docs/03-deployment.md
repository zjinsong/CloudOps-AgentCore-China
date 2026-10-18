# 部署

在[环境准备](02-prerequisites.md)指定的 Linux EC2 上，从仓库根目录执行。示例前缀为 `cloudops-ref`，同一账号多次独立部署需更换前缀。脚本只支持宁夏区，不创建 EC2。

`--apply` 执行资源创建或更新；省略该参数只显示阶段名称，不提供完整资源差异。

## 1. 初始化

```bash
git clone https://github.com/zjinsong/CloudOps-AgentCore-China.git
cd CloudOps-AgentCore-China
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python scripts/init_config.py
export AWS_DEFAULT_REGION=cn-northwest-1
export PROJECT=cloudops-ref
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
```

生成 `.state/deployment.json` 和 `.secrets/web.json`。前者保存私有部署状态，后者保存随机登录密码及签名密钥；两者均被 Git 忽略。

## 2. 创建 ECR 与模型 Secret

```bash
aws ecr create-repository --repository-name "$PROJECT-runtime" \
  --image-tag-mutability IMMUTABLE --image-scanning-configuration scanOnPush=true \
  --tags Key=Project,Value="$PROJECT" Key=Repository,Value=CloudOps-AgentCore-China
cp examples/model-secret.example.json .secrets/model.json
chmod 600 .secrets/model.json
nano .secrets/model.json
aws secretsmanager create-secret --name "$PROJECT/model" \
  --secret-string file://.secrets/model.json \
  --tags Key=Project,Value="$PROJECT" Key=Repository,Value=CloudOps-AgentCore-China
```

将模型文件中的 `api_key` 替换为有效密钥，记录返回的 Secret ARN。若资源已存在，仅在确认属于本次部署后复用。

## 3. 填写配置

```bash
nano .state/deployment.json
printf '%s\n' "$AWS_ACCOUNT_ID.dkr.ecr.cn-northwest-1.amazonaws.com.cn/$PROJECT-runtime:v1"
```

| 字段 | 值 |
| --- | --- |
| `project` | 与 `$PROJECT` 一致，创建后保持不变 |
| `region` | `cn-northwest-1` |
| `model_id` | 模型服务支持的模型名称 |
| `model_base_url` | 经批准的 HTTPS OpenAI 兼容地址，通常含 `/v1` |
| `model_secret_arn` | 上一步返回的 Secret ARN，与部署账号/区域一致 |
| `image_uri` | 上面命令生成的 ECR URI；每次发布使用新 tag |

## 4. 创建存储与角色

```bash
python scripts/deploy.py foundation --apply
```

创建 DynamoDB 表及四个索引、TTL、时间点恢复，两个私有 S3 桶，Runtime Role、EC2 Role 和 instance profile。状态写回 `.state/deployment.json`。已有同名资源必须具有匹配的 `Project` 和 `Repository` Tag。

## 5. 构建 Layer 和镜像

在 x86_64 EC2 上先完成[ARM64 仿真与 Buildx 配置](02-prerequisites.md#x86_64-主机构建-arm64-镜像)，确认测试容器输出 `aarch64`，再执行：

```bash
python scripts/build_mcp_lambda_layers.py
python scripts/build_inspection_layers.py
bash scripts/build_runtime.sh
```

Layer 在 Linux x86_64 / Python 3.12 环境打包到 `.state/mcp-lambda-layers/`。`build_runtime.sh` 读取配置中的 `image_uri`，执行 ECR 登录及 `docker buildx build --platform linux/arm64 --provenance=false --push`；镜像直接写入 ECR，不加载到本机 Docker 镜像列表。构建器需可拉取依赖和工具镜像。

## 6. 创建 Gateway 与工具

```bash
python scripts/deploy.py gateway --apply
```

创建一个 IAM MCP Gateway、八组 Lambda Target、四个 Layer 和工具执行角色，以及 Health EventBridge rule。完成标志为 `Gateway and 8 read-only targets ready`。Health 仅查询部署后收到并归档的事件。

## 7. 创建 Runtime

```bash
python scripts/deploy.py runtimes --apply
python scripts/validate_cloud.py
```

创建六个 Runtime，等待平台 V2 READY，注册专家并校验 Gateway 工具能力映射；生成 `.secrets/control.env`。校验应输出六个 `READY V2` 和 `Gateway: 8 IAM targets READY`。

## 8. 配置 EC2 身份并启动应用

为独立测试 EC2 附加生成的 instance profile，并允许 Docker bridge 使用 IMDSv2：

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
curl -fsS http://127.0.0.1:3001/ -o /dev/null
docker compose exec -T control python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8098/ping').read().decode())"
```

已有 instance profile 时使用替换接口，不重复 associate。应用使用 EC2 临时凭证，勿向 `.secrets/control.env` 添加管理员访问密钥。预期 control healthy，worker、observer、frontend 运行；默认不启动 executor。

## 9. 登录与验收

在自己的电脑建立 SSH 隧道（替换主机和私钥）：

```bash
ssh -N -L 127.0.0.1:3100:127.0.0.1:3001 -i ~/.ssh/your_key 'ubuntu@<YOUR_EC2_HOST>'
```

访问 `http://localhost:3100`，用户名 `admin`。在 EC2 私有终端读取密码：

```bash
python -c 'import json; print(json.load(open(".secrets/web.json"))["ADMIN_PASSWORD"])'
```

Web 仅监听 EC2 回环地址；公开 HTTPS 入口需另行配置，并设置 `COOKIE_SECURE=true`。

按[测试与验收](10-testing.md)执行单专家、协作和恢复测试；需要调用链时配置[观测](07-observability.md)。测试结束后按[清理资源](11-cleanup.md)删除资源。

## 失败处理

阶段失败后检查错误、服务日志及 `.state/deployment.json`，修复后重试该阶段。不要删除状态文件重新部署；Runtime 的 `--update` 用于发布新配置，不用于连接排障。Gateway 阶段重试会发布新的 Layer 版本，清理时须处理全部版本。
