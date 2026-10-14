# 接手说明

这是独立公开参考仓库，主线为 CloudOps、AgentCore 与应用 harness。私有部署历史与开发工作区没有迁入。

## 先做什么

先读 [README](README.md)，本地跑 [验证命令](docs/11-validation.md)，再按[部署指南](docs/03-deployment.md)在测试账号逐步构建。不要从旧系统拷贝密码、Role、桶或 ARN。

## 代码怎么找

| 路径 | 职责 |
| --- | --- |
| `agents/supervisor/ws_main.py` | 协调员 Runtime WebSocket 入口 |
| `agents/leaf/a2a_main.py` | 专家 Runtime A2A 入口 |
| `framework/model.py` | 模型接口与 Secret 读取 |
| `framework/gateway.py` | 签名 MCP 客户端 |
| `control/api.py` | Web API、观测、任务入口 |
| `control/worker.py` | 持久任务调度与专家执行 |
| `control/execution.py` | 执行租约、预算、检查点 |
| `control/capabilities.py` | 能力与工具映射 |
| `control/store.py` | DDB 存储与离线 SQLite |
| `framework/telemetry.py` | ADOT Span，无业务正文捕获 |
| `tools/` | Schema、Handler 和工具限制 |
| `frontend/` | CloudOps、团队管理和观测界面 |
| `scripts/deploy.py` | foundation / gateway / runtimes 三阶段部署 |

## 默认状态

地区宁夏，平台 V2；六个 Runtime、八组 MCP Target；随机应用密码；模型密钥在 Secrets Manager；计划执行、apply 和默认定时任务关闭；历史默认 30 天。

测试与验证边界见[验证记录](docs/11-validation.md)。评估自动化和企业登录属于后续工作，不能把规划写成已交付。

## 修改后必做

运行测试、前端构建、发布扫描和 Git diff 检查。工具变化同步 Schema/权限/能力映射；Runtime 更新明确镜像 tag 和配置版本；云端验收保存脱敏结果。公开 Git 历史不要带入真实部署配置。
