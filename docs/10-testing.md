# 测试与验收

所有命令在仓库根目录执行。本地检查不调用模型、不创建云资源；云端验收需要完成[部署](03-deployment.md)，会产生资源和模型调用费用。

## 1. 云端配置与 Web

`validate_cloud.py` 和下文 AWS CLI 对照查询使用部署身份，或具备对应读取权限的验收身份；应用容器继续使用生成的 EC2 Role。不要为运行验收命令向应用角色添加部署管理员权限。

```bash
python scripts/validate_cloud.py
docker compose ps
curl -fsS http://127.0.0.1:3001/ -o /dev/null
docker compose exec -T control python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8098/ping').read().decode())"
```

通过标准：六个 Runtime 显示 `READY V2`；DEFAULT 指向记录的配置版本；Gateway 的八个 IAM Target READY；control、worker、observer、frontend 运行且 control healthy；控制服务 `/ping` 成功、前端首页可访问。

建立 SSH 隧道后访问 `http://localhost:3100`，使用部署生成的 admin 密码登录。未登录时业务 API 应拒绝访问。

## 2. 专家查询与协作

在测试账号中执行，保存查询时间、任务 ID 和核对结果。

| 用例 | 输入 | 通过标准 |
| --- | --- | --- |
| 资源发现 | 向 Monitoring 输入“列出宁夏区当前运行中的 EC2，返回实例 ID、类型和状态” | 与 EC2 控制台/CLI 一致；无实例时明确返回空结果 |
| 指标查询 | “查询实例 `<INSTANCE_ID>` 最近一小时 CPU 平均值和最大值” | 区域、维度、时间窗和统计口径正确；无指标不写成零 |
| 审计查询 | 向 CloudTrail 输入“查询宁夏区过去 24 小时的 StopInstances 操作，返回时间、操作者和实例 ID” | 与 CloudTrail 对照；标明分页或截断范围 |
| 费用查询 | 向 FinOps 输入“查询过去七个完整自然日的 EC2 费用，按天汇总并标明币种” | 与 Cost Explorer 同口径数据一致；不将账户费用冒充实例费用 |
| 三专家协作 | 向协调员输入下面的协作任务 | 显示有效分工与依赖；汇总引用专家结果，失败分支明确 |
| 持久化 | 完成任务后刷新页面，重新登录查看历史 | 任务与结果仍可读取 |
| 默认执行限制 | 请求 Terraform 创建资源 | 不执行 plan/apply，不创建业务资源 |

协作用例：

> 请让 Monitoring、CloudTrail 和 FinOps 协作检查宁夏区 EC2：发现当前运行实例并查询最近一小时 CPU 平均值与最大值；查询过去 24 小时 StartInstances/StopInstances；查询过去七个完整自然日的 EC2 账户费用。汇总资源、指标、操作和费用证据，明确查询时间与缺口，不将账户费用分摊到实例。

可用以下 CLI 查询对照资源和审计事件；指标和费用需使用与 Agent 相同的时间、过滤条件和统计口径：

```bash
aws ec2 describe-instances --region cn-northwest-1 \
  --filters Name=instance-state-name,Values=running
aws cloudtrail lookup-events --region cn-northwest-1 \
  --lookup-attributes AttributeKey=EventName,AttributeValue=StopInstances \
  --start-time '<START_ISO8601>' --end-time '<END_ISO8601>'
```

## 3. 页面功能

| 页面 | 验收操作 | 通过标准 |
| --- | --- | --- |
| 团队管理 | 查看专家能力，停用一名测试专家后再启用 | 状态同步更新，停用专家不参与新任务，启用后恢复可选 |
| 定时任务 | 新建一个停用状态的未来任务，修改时间/时区，再删除 | 配置保存正确，停用任务不执行，删除后列表移除 |
| 服务指标 | 执行查询后进入“观测指标 → 服务指标”，切换时间范围并刷新 | 可查看 Runtime / Gateway 指标；允许 CloudWatch 上报延迟，无数据不当作故障 |
| 任务调用链 | 从任务或回答进入调用链，按状态和专家筛选 | Task ID、Trace ID、阶段耗时可见；配置遥测后可查看 SDK Span |

业务查询成功但没有 SDK Span 时，先完成[部署第 8 步](03-deployment.md#8-配置任务遥测)，再检查[遥测配置](07-observability.md)。

## 4. 恢复与观测

在独立测试环境提交一个只读任务，记录任务 ID，在任务执行期间运行：

```bash
docker compose restart worker
docker compose logs --tail 100 worker
```

通过标准：租约到期后任务可恢复或明确结束；页面可查询状态，不出现重复最终答复。尚未保存的只读查询可能重查，数据应注明查询时间。

如已配置[遥测](07-observability.md)，在观测页面选择该任务，确认模型、专家和 MCP Span 可关联到任务 ID。未启用遥测时，使用任务事件和容器日志排障。

## 5. 本地代码检查（修改代码时）

需要 Python 开发依赖；前端源码构建另外需要 Node.js 20 和 npm。仅验收已部署应用时无需执行本节。

```bash
source .venv/bin/activate
python -m pytest tests -q
python -m compileall -q agents control framework tools scripts examples
python scripts/check_links.py
python scripts/check_public.py --tree
git diff --check
cd frontend
npm ci --no-audit --no-fund
npm run build
cd ..
```

通过标准：测试、编译、链接检查、公开内容扫描和前端构建均返回退出码 0。测试覆盖任务状态、恢复、权限、部分失败、CloudTrail 分页及 SDK 请求契约；不证明目标账号的 IAM、配额、网络或答案质量。

## 发布验收

更换模型、提示词、工具或权限后，重复上述用例。关键数值与 AWS 对照一致、失败不伪装成成功、写操作受限是通过条件；本仓库不提供自动答案语义评分或预设准确率。完整账号初始化、IAM 与网络组合须在目标账号完成端到端验收。
