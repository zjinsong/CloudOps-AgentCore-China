"""Immutable Terraform plans with S3 state/locking and one-use user approval."""
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
import boto3
from control.store import store, Conflict, HISTORY_RETENTION_SECONDS
from control.security import fingerprint, approved

REGION = 'cn-northwest-1'

def validate_configuration(cfg):
    if not isinstance(cfg, dict) or set(cfg) - {'resource', 'data', 'output', 'locals', 'variable'}:
        raise ValueError('只提交 resource/data/output/locals/variable；平台管理 provider 和 backend')
    if len(json.dumps(cfg).encode()) > 200000:
        raise ValueError('脚本超过200KB，请拆分任务')
    for section in ('resource', 'data'):
        if not isinstance(cfg.get(section, {}), dict):
            raise ValueError('资源必须为 Terraform JSON 对象')
        if any(not kind.startswith('aws_') for kind in cfg.get(section, {})):
            raise ValueError('仅允许当前账户 AWS provider，不使用外部或本地执行 provider')
    def visit(value):
        if isinstance(value, dict):
            if set(value) & {'provisioner', 'provider', 'connection'}:
                raise ValueError('不允许 provisioner、连接脚本或覆盖 provider')
            if 'region' in value and value['region'] != REGION:
                raise ValueError('本工作区只允许 cn-northwest-1，不覆盖资源 region')
            for v in value.values():
                visit(v)
        elif isinstance(value, list):
            for v in value:
                visit(v)
        elif isinstance(value, str) and re.search(r'\b(file|filebase64|fileexists|fileset|templatefile|pathexpand)\s*\(|path\.(root|module)|terraform\.workspace', value):
            raise ValueError('不允许读取执行器本地文件或动态切换工作区')
    visit(cfg)
    return cfg

def source(project):
    return {**project['configuration'], 'terraform': {'required_version': '>= 1.10, < 2',
        'required_providers': {'aws': {'source': 'hashicorp/aws', 'version': '= 6.18.0'}},
        'backend': {'s3': {'bucket': os.environ['CLOUDOPS_BUCKET'], 'key': 'cloudops/terraform/' + project.get('workspace_id', project['id']) + '/terraform.tfstate',
                           'region': REGION, 'encrypt': True, 'use_lockfile': True}}},
        'provider': {'aws': {'region': REGION, 'allowed_account_ids': [os.environ['CLOUDOPS_ACCOUNT']]}}}

def prepare(task, name, configuration, previous_project_id=''):
    validate_configuration(configuration)
    previous = store.get(previous_project_id) if previous_project_id else None
    if previous_project_id and (not previous or previous.get('kind') != 'terraform' or previous['customer_id'] != task['customer_id'] or previous['conversation_id'] != task['conversation_id']):
        raise PermissionError('原项目不属于当前客户和会话')
    if previous and previous['status'] in ('planning', 'applying', 'needs_review'):
        raise ValueError('原项目仍在执行或需要核对，请先处理')
    ident = 'tf-' + fingerprint({'task': task['id'], 'configuration': configuration, 'previous_project': previous_project_id})[:32]
    old = store.get(ident)
    if old:
        return old
    project = {'id': ident, 'kind': 'terraform', 'name': name[:100], 'task_id': task['id'], 'conversation_id': task['conversation_id'],
               'customer_id': task['customer_id'], 'user_id': task.get('user_id', 'admin'), 'account': os.environ['CLOUDOPS_ACCOUNT'], 'region': REGION,
               'configuration': configuration, 'config_fingerprint': fingerprint(configuration), 'status': 'plan_queued',
               'expires_at': int(time.time() + HISTORY_RETENTION_SECONDS),
               'workspace_id': previous.get('workspace_id', previous['id']) if previous else ident,
               'previous_project_id': previous_project_id}
    try:
        result = store.put(project)
    except Conflict:
        return store.get(ident)
    store.event(task, 'Terraform 脚本已保存，正在排队生成变更预览。', 'terraform')
    return result

def public_project(ident):
    p = store.get(ident)
    if not p or p['kind'] != 'terraform':
        raise ValueError('Terraform 项目不存在')
    return {k:v for k,v in p.items() if k not in ('plan_binary', 'provider_lock')} | {'script': json.dumps(source(p), ensure_ascii=False, indent=2)}

def approval(ident):
    p = store.get(ident)
    if not p or p['status'] != 'planned' or p.get('plan_expires_at', 0) <= time.time():
        raise ValueError('需要有效的已生成 plan')
    action = {'type': 'terraform_apply', 'project_id': ident, 'plan_sha256': p['plan_sha256'], 'config_fingerprint': p['config_fingerprint']}
    approval_id = 'approval-' + ident + '-' + p['plan_sha256'][:12]
    old = store.get(approval_id)
    if old:
        return old
    return store.put({'id': approval_id, 'kind': 'approval', 'task_id': p['task_id'], 'conversation_id': p['conversation_id'],
                      'user_id': p.get('user_id', 'admin'), 'action': action, 'status': 'pending', 'fingerprint': fingerprint(action), 'expires_at': p['plan_expires_at']})

def validate_action(action):
    if action.get('type') != 'terraform_apply':
        raise ValueError('本阶段仅支持 Terraform plan 的部署确认')
    p = store.get(action.get('project_id', ''))
    if not p or p['status'] != 'planned' or p.get('plan_expires_at', 0) <= time.time():
        raise ValueError('plan 已过期、已使用或不存在')
    if p['account'] != os.environ.get('CLOUDOPS_ACCOUNT') or p['region'] != REGION:
        raise PermissionError('plan 不属于当前账户和区域')
    if action.get('plan_sha256') != p['plan_sha256'] or action.get('config_fingerprint') != fingerprint(p['configuration']):
        raise ValueError('脚本或 plan 已变化，需要重新确认')

def credentials(deploy=False):
    role = os.environ['CLOUDOPS_TERRAFORM_DEPLOY_ROLE' if deploy else 'CLOUDOPS_TERRAFORM_PLAN_ROLE']
    cred = boto3.client('sts', region_name=REGION).assume_role(RoleArn=role, RoleSessionName='cloudops-terraform', DurationSeconds=3600)['Credentials']
    return {'AWS_ACCESS_KEY_ID': cred['AccessKeyId'], 'AWS_SECRET_ACCESS_KEY': cred['SecretAccessKey'], 'AWS_SESSION_TOKEN': cred['SessionToken']}

def command(args, directory, env, timeout=300):
    result = subprocess.run(['terraform', *args], cwd=directory, env=env, capture_output=True, text=True, timeout=timeout)
    if result.returncode not in (0, 2):
        # Only provider/CLI messages are returned, never environment credentials.
        raise RuntimeError((result.stderr or result.stdout)[-6000:])
    return result

def environment(deploy=False):
    env = dict(os.environ)
    for key in ('AWS_PROFILE', 'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI', 'AWS_CONTAINER_CREDENTIALS_FULL_URI'):
        env.pop(key, None)
    env.update(credentials(deploy), AWS_REGION=REGION, AWS_DEFAULT_REGION=REGION, TF_IN_AUTOMATION='1', TF_INPUT='0', CHECKPOINT_DISABLE='1')
    return env

def upload(key, raw):
    r = boto3.client('s3', region_name=REGION).put_object(Bucket=os.environ['CLOUDOPS_BUCKET'], Key=key, Body=raw, ServerSideEncryption='AES256')
    return {'key': key, 'version': r.get('VersionId')}

def download(ref):
    kw = {'Bucket': os.environ['CLOUDOPS_BUCKET'], 'Key': ref['key']}
    if ref.get('version'):
        kw['VersionId'] = ref['version']
    return boto3.client('s3', region_name=REGION).get_object(**kw)['Body'].read()

def plan(ident):
    def claim(x):
        if x['status'] != 'plan_queued':
            raise Conflict()
        return {**x, 'status': 'planning', 'started_at': time.time()}
    p = store.update(ident, claim)
    try:
        env = environment()
        with tempfile.TemporaryDirectory(prefix='cloudops-tf-') as d:
            directory = Path(d)
            (directory / 'main.tf.json').write_text(json.dumps(source(p), ensure_ascii=False), encoding='utf-8')
            command(['init', '-no-color', '-input=false'], d, env)
            command(['validate', '-no-color'], d, env)
            command(['plan', '-no-color', '-input=false', '-lock-timeout=20s', '-out=approved.tfplan'], d, env, timeout=900)
            raw = (directory / 'approved.tfplan').read_bytes()
            report = json.loads(command(['show', '-json', 'approved.tfplan'], d, env).stdout)
            changes = [{'address': r['address'], 'actions': r['change']['actions']} for r in report.get('resource_changes', []) if r['change']['actions'] != ['no-op']]
            ref = upload('cloudops/terraform/' + ident + '/approved.tfplan', raw)
            lock = upload('cloudops/terraform/' + ident + '/.terraform.lock.hcl', (directory / '.terraform.lock.hcl').read_bytes())
            script = upload('cloudops/terraform/' + ident + '/main.tf.json', (directory / 'main.tf.json').read_bytes())
            final = store.update(ident, lambda x: {**x, 'status': 'planned', 'plan_binary': ref, 'provider_lock': lock,
                'script_ref': script, 'plan_sha256': hashlib.sha256(raw).hexdigest(), 'changes': changes,
                'summary': f'已校验脚本并生成真实 plan：{len(changes)} 项资源变更。等待用户确认。', 'planned_at': time.time(), 'plan_expires_at': time.time() + 3600})
            t = store.get(p['task_id'])
            store.event(t, final['summary'], 'terraform')
            if t['status'] not in ('cancelled', 'failed'):
                approval(ident)
    except Exception as e:
        store.update(ident, lambda x: {**x, 'status': 'failed', 'error': str(e)[:6000]})
        store.event(store.get(p['task_id']), 'Terraform plan 失败，请在当前对话查看错误。', 'terraform')

def apply(approval_id):
    if os.getenv('CLOUDOPS_ENABLE_APPLY') != 'true':
        raise PermissionError('Resource apply is disabled in this reference deployment')
    a = store.get(approval_id)
    if not approved(a, a['action']):
        raise PermissionError('需要用户对具体 plan 的有效确认')
    validate_action(a['action'])
    p = store.get(a['action']['project_id'])
    if a.get('user_id', 'admin') != p.get('user_id', 'admin'):
        raise PermissionError('部署确认与项目用户不匹配')
    def claim(x):
        if not approved(x, a['action']) or x.get('user') != x.get('user_id', 'admin') or x.get('user_id', 'admin') != p.get('user_id', 'admin'):
            raise PermissionError('确认已使用')
        return {**x, 'status': 'executing', 'started_at': time.time()}
    a = store.update(approval_id, claim)
    def claim_project(x):
        if x['status'] != 'planned' or x['plan_sha256'] != a['action']['plan_sha256']:
            raise Conflict('plan 已被其他执行使用')
        return {**x, 'status': 'applying', 'approval_id': a['id'], 'apply_started_at': time.time()}
    try:
        store.update(p['id'], claim_project)
    except Conflict:
        store.update(a['id'], lambda x: {**x, 'status': 'needs_review', 'error': 'plan 已被其他执行使用，本确认未执行。'})
        return
    try:
        raw = download(p['plan_binary'])
        if hashlib.sha256(raw).hexdigest() != a['action']['plan_sha256']:
            raise PermissionError('plan 文件校验失败')
        env = environment(deploy=True)
        with tempfile.TemporaryDirectory(prefix='cloudops-apply-') as d:
            directory = Path(d)
            (directory / 'main.tf.json').write_text(json.dumps(source(p), ensure_ascii=False), encoding='utf-8')
            (directory / '.terraform.lock.hcl').write_bytes(download(p['provider_lock']))
            (directory / 'approved.tfplan').write_bytes(raw)
            command(['init', '-no-color', '-input=false', '-lockfile=readonly'], d, env)
            command(['apply', '-no-color', '-input=false', 'approved.tfplan'], d, env, timeout=3300)
            state = json.loads(command(['show', '-json'], d, env).stdout)
            result = {'status': 'applied', 'resources': [r['address'] for r in state.get('values', {}).get('root_module', {}).get('resources', [])],
                      'account': p['account'], 'region': REGION}
            store.update(p['id'], lambda x: {**x, 'status': 'succeeded', 'result': result, 'finished_at': time.time()})
            store.update(a['id'], lambda x: {**x, 'status': 'succeeded', 'result': result, 'finished_at': time.time()})
            t = store.get(p['task_id'])
            store.event(t, 'Terraform 部署成功，已核验保存的 state。', 'terraform')
            try:
                from control import messages, domain
                messages.create(domain.require_conversation(p['conversation_id']), 'assistant',
                                'Terraform 部署成功。\n' + json.dumps(result, ensure_ascii=False),
                                task_id=t['id'], agent='terraform', ident='answer-' + a['id'])
            except Conflict:
                pass
    except Exception as e:
        store.update(p['id'], lambda x: {**x, 'status': 'needs_review', 'error': str(e)[:6000]})
        store.update(a['id'], lambda x: {**x, 'status': 'needs_review', 'error': str(e)[:6000]})
        store.event(store.get(p['task_id']), '部署结果需要核对，不会自动重复 apply。', 'terraform')

def main():
    # An interrupted apply must be reconciled, never replayed on restart.
    for a in store.all('approval'):
        if a['status'] == 'executing' and a.get('action', {}).get('type') == 'terraform_apply':
            store.update(a['id'], lambda x: {**x, 'status': 'needs_review', 'error': '执行器重启，需要核对 Terraform state 和实际资源；没有重放 apply。'})
            store.update(a['action']['project_id'], lambda x: {**x, 'status': 'needs_review', 'error': '部署中断，需要核对实际资源。'})
    for p in store.all('terraform'):
        if p['status'] == 'planning':
            store.update(p['id'], lambda x: {**x, 'status': 'plan_queued'})
    while True:
        for p in store.all('terraform'):
            if p['status'] == 'plan_queued':
                try:
                    plan(p['id'])
                except Conflict:
                    pass
        for a in store.all('approval'):
            if a['status'] == 'approved' and a.get('action', {}).get('type') == 'terraform_apply':
                try:
                    apply(a['id'])
                except (PermissionError, ValueError, Conflict):
                    continue
        time.sleep(3)

if __name__ == '__main__':
    main()
