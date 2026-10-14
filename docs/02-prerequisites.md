# 二、准备环境

先准备一个测试账号和一台 Ubuntu Linux 主机。本指南使用宁夏区，工具 Lambda 使用 x86_64，Runtime 镜像使用 ARM64。

## 你需要准备什么

| 条件 | 用途 |
| --- | --- |
| AWS 中国区测试账号 | 创建 AgentCore、IAM、Lambda、DynamoDB、S3、ECR |
| 部署身份的临时凭证或部署 Role | 创建资源；不能拿应用 Role 当部署管理员 |
| Ubuntu 24.04 x86_64、Python 3.12 | 构建兼容 Lambda 的 Python Layer |
| Docker Engine、Compose、Buildx | 构建两个容器镜像及 ARM64 Runtime 镜像 |
| AWS CLI v2、Git | ECR、Secret 和代码操作 |
| 可联网的模型 HTTPS 地址和密钥 | 模型推理；应先验证工具调用能力 |

本地构建前端才需要 Node.js 20；Docker 构建前端会自行安装依赖。EC2 建议先用 4 vCPU、8 GiB 内存做构建与演示，最终规格应看实际负载。

```bash
aws sts get-caller-identity --region cn-northwest-1
python3.12 --version
docker version
docker compose version
docker buildx version
git --version
```

CLI 身份输出包含账号信息，只在自己的终端查看，不提交到仓库。

## 权限分开

部署身份需要本项目涉及服务的创建、更新、Tag 和 `iam:PassRole` 权限。新账号首次使用 Runtime 还可能需要创建服务关联角色的权限。组织 SCP、权限边界、服务配额或角色传播延迟都可能影响部署。

部署脚本生成的应用角色只访问自己的任务存储、模型 Secret、Runtime 和 Gateway；工具角色只获得选定的只读 AWS API。部署结束后 EC2 改用生成的 instance profile，不能把部署管理员密钥留在容器环境里。

## ARM64 构建

Runtime 容器使用 ARM64。在 x86_64 主机上，需要已有 ARM64 Buildx 构建器或 QEMU 仿真；也可以使用原生 ARM64 构建机。不要把 Windows/ARM64 主机上的 pip 安装目录直接打包为 x86_64 Lambda Layer。

检查 Buildx 支持的平台：

```bash
docker buildx inspect --bootstrap
```

若未包含 `linux/arm64`，先按你的容器管理规范配置构建器，再执行部署。Layer 构建仍使用 Linux x86_64 + Python 3.12。

[下一章：逐步部署](03-deployment.md)
