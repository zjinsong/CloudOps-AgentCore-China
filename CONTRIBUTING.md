# 贡献指南

先阅读[接手说明](handoff.md)。提交前执行：

```bash
python -m pytest tests -q
python -m compileall -q agents control framework tools scripts examples
python scripts/check_public.py --tree
git diff --check
cd frontend && npm ci && npm run build
```

新增工具必须同时审阅输入 Schema、只读权限、Handler、能力映射和分页/覆盖语义。新增可写操作必须有独立执行权限、具体审批和幂等设计，不能只改提示词。

禁止提交密钥、账号清单、用户密码、真实主机、客户数据或私有状态。Issue 与测试 fixture 也要脱敏。文档使用 Linux 命令和占位值，区分已经实现、已验证与规划。
