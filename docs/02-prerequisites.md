# 环境准备

本指南在一台独立的 Ubuntu EC2 上完成构建和 Web 部署。使用 AWS 中国区宁夏区域 `cn-northwest-1`；工具 Lambda 为 x86_64，Runtime 镜像为 ARM64。

## 环境要求

| 项目 | 要求 |
| --- | --- |
| AWS 账号 | 中国区测试账号，可使用宁夏区 AgentCore；部署身份具备资源创建权限 |
| EC2 | Ubuntu 24.04 x86_64，建议起步 4 vCPU / 8 GiB，磁盘满足镜像与依赖构建 |
| Python | 3.12、venv 和 pip |
| 容器工具 | Docker Engine、Compose、Buildx，构建器支持 `linux/arm64` |
| 命令行 | AWS CLI v2、Git、curl |
| 模型 | 可从 EC2 和 Runtime 访问的 HTTPS OpenAI 兼容端点，支持工具调用 |
| 网络 | 可拉取 PyPI/npm/容器依赖并访问 ECR、AWS API 和模型端点 |

本地运行前端测试需要 Node.js 20 和 npm；仅通过 Docker 构建前端时不需要在主机安装 Node。EC2 仅开放所需管理入口，Web 通过 SSH 隧道访问。

## 安装入口

在 Ubuntu 24.04 上安装基础工具：

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv python3-pip git curl unzip nano
```

Docker 按 [Docker 官方 Ubuntu 安装指南](https://docs.docker.com/engine/install/ubuntu/)安装 Engine、Buildx 和 Compose 插件；AWS CLI 按 [Linux x86_64 安装步骤](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)安装 v2。下文假设当前用户可运行 Docker 命令。部署主机还需访问 Docker Hub、Public ECR 和 HashiCorp 下载站点。

## 环境检查

```bash
export AWS_DEFAULT_REGION=cn-northwest-1
aws sts get-caller-identity
python3.12 --version
docker version
docker compose version
docker buildx version
git --version
```

Lambda Layer 在 Linux x86_64 / Python 3.12 环境构建；Runtime 镜像按下一节配置跨架构构建。

## x86_64 主机构建 ARM64 镜像

在上述 Ubuntu x86_64 构建主机上注册 ARM64 QEMU 仿真，并创建独立的 Buildx 构建器：

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64
docker buildx create --name cloudops-builder --driver docker-container --use
docker buildx inspect --bootstrap
docker run --rm --platform linux/arm64 \
  public.ecr.aws/docker/library/python:3.12-slim uname -m
```

通过标准：Buildx 输出的 `Platforms` 包含 `linux/arm64`，测试容器输出 `aarch64`。同名构建器已存在时，用 `docker buildx use cloudops-builder` 代替 `create`。主机重启后若仿真注册丢失，重新执行第一条命令。

`--privileged` 用于向构建主机注册 `binfmt_misc`，不用于应用容器。该步骤需要拉取 Docker Hub 的 `tonistiigi/binfmt`；使用镜像代理时替换为对应镜像地址。支持 ARM64 的现有构建器或原生 ARM64 远端节点也可复用，配置方式见 [Docker 多平台构建文档](https://docs.docker.com/build/building/multi-platform/)。

完成后，部署阶段的 `bash scripts/build_runtime.sh` 会调用当前构建器，以 `--platform linux/arm64` 构建并直接推送 ECR。Web 与控制服务仍在 x86_64 主机上按本机架构构建，Lambda Layer 仍为 x86_64。

## 身份与权限

部署身份需要创建或更新 AgentCore、IAM、Lambda、DynamoDB、S3、ECR、Secrets Manager、EventBridge，以及相关 Tag 和 `iam:PassRole` 权限；首次使用相关服务可能需要创建服务关联角色。组织 SCP、权限边界及配额需允许这些操作。

部署脚本生成 Runtime Role、EC2 Role、Gateway Role 和各工具 Lambda Role。运行应用时使用 EC2 instance profile，不向容器注入部署管理员凭证。只读工具动作见 [scripts/permissions.py](../scripts/permissions.py)。

## 模型配置

准备 `model_id`、HTTPS `model_base_url` 和 `api_key`。端点通常以 `/v1` 结尾，须支持 Strands 所需的 OpenAI 工具调用协议。Runtime 使用 PUBLIC 网络，不继承 EC2 的本地代理；仅在 EC2 测试模型成功不足以确认 Runtime 可达。

[下一步：部署](03-deployment.md)
