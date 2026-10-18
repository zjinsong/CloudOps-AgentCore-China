# 安全边界

## 身份与数据

| 边界 | 实现 |
| --- | --- |
| 用户 → Web | 应用账号、签名 Cookie 和 CSRF 校验 |
| Web → Runtime | EC2 instance profile / SigV4 |
| Runtime → Gateway | Runtime Role，限制本项目 Gateway |
| Gateway → Lambda | Gateway Role，限制本项目函数 |
| Lambda → AWS | 只读 API 白名单、区域与参数约束 |
| 模型密钥 | Secrets Manager，按 Secret ARN 授权 |
| 会话与结果 | 私有 DynamoDB/S3，TTL 和生命周期 |

“只读”针对业务资源，应用仍写任务、日志和检查点。部分 AWS 列表接口要求 `Resource: "*"`；当前工具授权不是每实例级隔离。用户记录按应用用户隔离，不等同于租户独立 AWS 账号或 Role。

## 部署配置

`.state/`、`.secrets/` 和私钥不提交到仓库。初始化生成随机管理员密码和签名密钥，无共享默认密码。运行应用使用 EC2 临时凭证，不在容器配置中保存部署管理员密钥。

Web 默认仅监听 `127.0.0.1:3001`，通过 SSH 隧道访问。公开入口须配置 HTTPS、`COOKIE_SECURE=true` 及适当访问控制。企业 SSO 和租户级 AWS 授权需另行集成。

## 模型数据出口

模型可能接收问题和工具结果。部署前确认模型端点、数据处理范围及组织的数据驻留要求；HTTPS 提供传输加密，不控制供应商的数据处理行为。

## 执行限制

默认 `CLOUDOPS_ENABLE_TERRAFORM_PLAN=false`、`CLOUDOPS_ENABLE_APPLY=false`，不启动 executor；默认巡检任务不自动执行。启用 Terraform 执行需要独立角色、审批、有效期和动作指纹，不能仅调整开关。

## 公开内容检查

```bash
python scripts/check_public.py --tree
```

扫描覆盖常见账号、密钥和私有路径，提交前还应审阅 diff。安全问题通过[私密漏洞报告](../SECURITY.md)反馈。
