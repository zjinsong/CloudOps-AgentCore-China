# 清理资源

在部署所用 Linux 主机的仓库根目录执行，使用具有删除权限的部署身份。以下操作删除测试数据；仅处理本次部署专用资源。保留 `.state/deployment.json` 到清理结束，部分部署失败时按实际存在的资源执行。

## 1. 停止任务与应用

停用定时任务，等待或取消未完成任务，然后停止应用：

```bash
docker compose down
export AWS_DEFAULT_REGION=cn-northwest-1
export PROJECT=$(python -c 'import json; print(json.load(open(".state/deployment.json"))["project"])')
python - <<'PY'
import json
c = json.load(open('.state/deployment.json'))
for key in ('project', 'gateway_id', 'functions', 'layers', 'runtimes', 'instance_profile', 'model_secret_arn', 'image_uri', 'common'):
    print(key, json.dumps(c.get(key), ensure_ascii=False))
PY
```

输出是本次部署的资源清单。对待删除资源核对 `Project=$PROJECT` 和 `Repository=CloudOps-AgentCore-China` Tag。以下占位值从清单或对应服务控制台获取，不使用其他应用的资源 ID。

仍有已知活跃 Runtime 会话时，先逐个停止：

```bash
aws bedrock-agentcore stop-runtime-session \
  --agent-runtime-arn '<RUNTIME_ARN>' --runtime-session-id '<SESSION_ID>' --qualifier DEFAULT
```

## 2. Runtime 与 Gateway

对清单中六个 Runtime 分别执行，异步删除完成后再清理角色和存储：

```bash
aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id '<RUNTIME_ID>'
```

列出 Gateway Target，逐个删除；等待 Target 全部移除后删除 Gateway：

```bash
aws bedrock-agentcore-control list-gateway-targets --gateway-identifier '<GATEWAY_ID>'
aws bedrock-agentcore-control delete-gateway-target \
  --gateway-identifier '<GATEWAY_ID>' --target-id '<TARGET_ID>'
aws bedrock-agentcore-control delete-gateway --gateway-identifier '<GATEWAY_ID>'
```

删除是异步操作。通过控制台或 `get-agent-runtime`、`list-gateway-targets`、`get-gateway` 确认完成；仍在删除时不要继续依赖资源的清理。

## 3. EventBridge、Lambda 与 Layer

先停止 Health 事件入口：

```bash
aws events remove-targets --rule "$PROJECT-health" --ids archive
aws events delete-rule --name "$PROJECT-health"
```

对 `.state/deployment.json` 中 `functions` 的每个函数 ARN 执行：

```bash
aws lambda delete-function --function-name '<FUNCTION_ARN>'
```

Layer 名为 `$PROJECT-core`、`$PROJECT-pricing`、`$PROJECT-aws-api`、`$PROJECT-agentcore-mcp`。列出并删除每个名称的全部版本，不能只删除状态文件中记录的最新版：

```bash
aws lambda list-layer-versions --layer-name "$PROJECT-core"
aws lambda delete-layer-version --layer-name "$PROJECT-core" --version-number '<VERSION>'
```

## 4. 存储、镜像与密钥

### S3

确认历史不再需要后清空两个项目桶。工作桶启用了版本控制，`aws s3 rm --recursive` 不会删除全部历史版本。下面代码校验 Tag、删除对象/版本/删除标记后删除桶：

```bash
python - <<'PY'
import json, boto3
c = json.load(open('.state/deployment.json'))
s3 = boto3.client('s3', region_name=c['region'])
for key in ('CLOUDOPS_BUCKET', 'CLOUDOPS_CHAT_BUCKET'):
    bucket = c['common'][key]
    tags = {t['Key']: t['Value'] for t in s3.get_bucket_tagging(Bucket=bucket)['TagSet']}
    if tags.get('Project') != c['project'] or tags.get('Repository') != 'CloudOps-AgentCore-China':
        raise RuntimeError('Bucket ownership mismatch')
    for page in s3.get_paginator('list_object_versions').paginate(Bucket=bucket):
        objects = [{'Key': x['Key'], 'VersionId': x['VersionId']}
                   for field in ('Versions', 'DeleteMarkers') for x in page.get(field, [])]
        for start in range(0, len(objects), 1000):
            response = s3.delete_objects(Bucket=bucket, Delete={'Objects': objects[start:start + 1000]})
            if response.get('Errors'):
                raise RuntimeError(response['Errors'])
    for page in s3.get_paginator('list_multipart_uploads').paginate(Bucket=bucket):
        for upload in page.get('Uploads', []):
            s3.abort_multipart_upload(Bucket=bucket, Key=upload['Key'], UploadId=upload['UploadId'])
    s3.delete_bucket(Bucket=bucket)
    print('Deleted bucket:', bucket)
PY
```

### DynamoDB、ECR 和 Secret

仅删除本项目专用的表、镜像仓库和模型 Secret。Secret 默认保留七天恢复窗口：

```bash
aws dynamodb delete-table --table-name "$PROJECT-tasks"
aws ecr delete-repository --repository-name "$PROJECT-runtime" --force
aws secretsmanager delete-secret --secret-id '<MODEL_SECRET_ARN>' --recovery-window-in-days 7
```

## 5. EC2 Profile 与 IAM

先解除测试 EC2 的 instance profile 关联。查询输出中选择本项目的关联 ID：

```bash
aws ec2 describe-iam-instance-profile-associations \
  --filters Name=instance-id,Values='<TEST_INSTANCE_ID>'
aws ec2 disassociate-iam-instance-profile --association-id '<ASSOCIATION_ID>'
aws iam remove-role-from-instance-profile \
  --instance-profile-name "$PROJECT-web" --role-name "$PROJECT-web"
aws iam delete-instance-profile --instance-profile-name "$PROJECT-web"
```

项目角色包括 `$PROJECT-runtime`、`$PROJECT-web`、`$PROJECT-gateway`，以及八个 `$PROJECT-lambda-<TARGET>`。对每个角色列出并删除内联策略；如附加了托管策略，先解除关联，再删除角色：

```bash
aws iam list-role-policies --role-name '<PROJECT_ROLE>'
aws iam delete-role-policy --role-name '<PROJECT_ROLE>' --policy-name '<INLINE_POLICY>'
aws iam list-attached-role-policies --role-name '<PROJECT_ROLE>'
aws iam detach-role-policy --role-name '<PROJECT_ROLE>' --policy-arn '<ATTACHED_POLICY_ARN>'
aws iam delete-role --role-name '<PROJECT_ROLE>'
```

通常内联策略为 `reference-scoped`；配置遥测后 Runtime/Web 还可能有 `CloudOpsTaskObservability`。不要删除 AWS 服务关联角色或共享托管策略。

## 6. 日志、测试主机与残留

- 删除本项目 Lambda 日志组 `/aws/lambda/<FUNCTION_NAME>` 和相应 Runtime 专用日志组；按实际资源名逐个核对。
- `aws/spans`、X-Ray 目的地和 `CloudOpsTransactionSearch` 日志资源策略可能由账号内其他应用共享。保留共享配置；仅在专用测试账号确认无其他使用方后恢复原设置。
- EC2 为手工创建，脚本不会删除。停止或终止专用测试实例，检查 EBS、快照、弹性 IP 和手工创建的网络资源。停止实例仍可能产生磁盘等费用。
- 检查手工创建的 DynamoDB 备份、日志和额外镜像；确认清理后再删除本地私有状态及 Secret 文件。

通过标准：项目 Runtime、Gateway、Target、Lambda、Layer 版本、桶、表和 IAM 资源均不存在；Secret 进入计划删除；专用 EC2 及附属资源已按需释放。最后检查账单；资源删除与费用显示存在延迟。
