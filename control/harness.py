"""Read, check, draft and export. Publishing is independently authorized."""
import copy
import json
import os
import re
import time

import boto3

from control.store import store, uid
from framework.validator import validate_hierarchy

STANDARD = 'cloudops-harness/1.0'


def cp():
    region = os.getenv('AWS_REGION', 'cn-northwest-1')
    return boto3.client('bedrock-agentcore-control', region_name=region,
                        endpoint_url=f'https://bedrock-agentcore-control.{region}.amazonaws.com.cn')


def redact(value):
    if isinstance(value, dict):
        return {k: ('[REDACTED]' if any(s in k.lower() for s in ('secret', 'password', 'api_key', 'access_key', 'token')) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(x) for x in value]
    return value


def inspect(name):
    a = store.get('agent-' + name)
    if not a:
        raise ValueError('专家不存在')
    checks = []
    def add(rule, status, detail, suggestion=''):
        checks.append({'rule': rule, 'status': status, 'detail': detail, 'suggestion': suggestion})
    cfg = a['config']
    validate = validate_hierarchy({'agents': {name: {**cfg, 'children': []}}})
    add('配置结构', 'failed' if validate['errors'] else 'passed', '; '.join(validate['errors']) or '配置结构检查通过')
    for warning in validate['warnings']:
        add('配置质量', 'warning', warning, '补齐领域工具与审批声明')
    add('职责与提示词', 'passed' if cfg.get('prompt') and cfg.get('description') else 'failed', '检查职责描述及系统提示词', '补齐 prompt 和 description')
    add('声明协议', 'passed' if a['protocol'] == 'A2A' else 'warning', a['protocol'], '迁移为 A2A')
    if 'cfn-exec' in cfg.get('tools', []):
        add('写操作审批声明', 'passed' if 'cfn-exec' in cfg.get('approval_required', []) else 'failed', 'CFN 写操作需独立审批', '补充 approval_required；仍需执行入口验证')
    actual = None
    if a.get('runtime_arn'):
        try:
            r = cp().get_agent_runtime(agentRuntimeId=a['runtime_arn'].split('/')[-1])
            actual = {k: r.get(k) for k in ('agentRuntimeArn', 'agentRuntimeVersion', 'status', 'roleArn', 'protocolConfiguration', 'networkConfiguration', 'agentRuntimeArtifact')}
            add('部署就绪', 'passed' if r['status'] == 'READY' else 'failed', r['status'])
            protocol = r.get('protocolConfiguration', {}).get('serverProtocol')
            add('协议一致性', 'passed' if protocol == a['protocol'] else 'failed', f'声明 {a["protocol"]}，实际 {protocol}', '发布对应协议服务并复检')
            role = r.get('roleArn', '').split('/')[-1]
            if role:
                iam = boto3.client('iam', region_name=os.getenv('AWS_REGION', 'cn-northwest-1'))
                inline = iam.list_role_policies(RoleName=role).get('PolicyNames', [])
                attached = iam.list_attached_role_policies(RoleName=role).get('AttachedPolicies', [])
                actual['iam'] = {'inline': inline, 'attached': [x['PolicyName'] for x in attached]}
                wildcard = False
                for policy in inline:
                    doc = iam.get_role_policy(RoleName=role, PolicyName=policy)['PolicyDocument']
                    for s in doc.get('Statement', []):
                        actions = s.get('Action', [])
                        actions = [actions] if isinstance(actions, str) else actions
                        if s.get('Effect') == 'Allow' and any(x in ('*', 'cloudformation:*', 'iam:*') for x in actions):
                            wildcard = True
                add('IAM 权限范围', 'warning' if wildcard or attached else 'passed', '发现宽泛权限或附加策略需进一步审查' if wildcard or attached else '内联策略未发现全服务授权', '遵守最小权限；附加策略与条件仍需独立核查')
        except Exception as e:
            add('实际部署读取', 'unverified', f'无法核验：{type(e).__name__}', '提供读取 Runtime/IAM 的权限')
    else:
        add('实际部署读取', 'unverified', '尚未部署或未登记 Runtime ARN')
    add('提示词与代码部署一致性', 'unverified', 'Runtime 控制面不返回容器内代码，需构建版本与配置指纹核验', '纳管发布后记录配置指纹')
    add('运行行为', 'unverified', '静态检查不能证明工具正确、审批或恢复有效', '执行受控调用与验收测试')
    return {'standard': STANDARD, 'agent': name, 'checked_at': time.time(), 'config': redact(cfg), 'actual': actual, 'checks': checks,
            'passed': not any(x['status'] == 'failed' for x in checks)}


def draft(name, config, description=''):
    if not re.fullmatch(r'[a-z][a-z0-9_]{1,40}', name):
        raise ValueError('名称需为 2–41 位小写字母、数字、下划线')
    if not isinstance(config, dict):
        raise ValueError('配置必须为对象')
    allowed = {'type', 'description', 'prompt', 'model', 'tools', 'approval_required', 'protocol', 'memory', 'children', 'stage'}
    if set(config) - allowed:
        raise ValueError('配置含未支持字段')
    cfg = copy.deepcopy(config)
    if cfg.get('type') != 'worker' and name != 'supervisor':
        raise ValueError('当前新建模板仅支持 worker 专家')
    if name == 'supervisor':
        cfg['children'] = cfg.get('children', [])
    elif cfg.get('children'):
        raise ValueError('当前仅 Supervisor 可配置下级专家')
    if any(x in ('coding', 'browser') for x in cfg.get('children', [])):
        raise ValueError('当前仅支持 CloudOps 专家')
    cfg['protocol'] = 'A2A'
    cfg.setdefault('model', 'deepseek-v4-pro')
    check = validate_hierarchy({'agents': {**{a['name']: a['config'] for a in store.all('agent')}, name: cfg}})
    if not cfg.get('prompt'):
        check['errors'].append('缺少 prompt')
    if cfg.get('model') not in ('deepseek-v4-pro', 'deepseek-chat', 'deepseek-reasoner'):
        check['errors'].append('模型未在允许清单中')
    allowed_targets = {'cost-explorer', 'cloudwatch', 'harness', 'iac-mcp', 'cfn-exec', 'health-events'}
    if not isinstance(cfg.get('tools', []), list) or any(x not in allowed_targets for x in cfg.get('tools', [])):
        check['errors'].append('工具 target 未获平台授权')
    if 'cfn-exec' in cfg.get('tools', []) and 'cfn-exec' not in cfg.get('approval_required', []):
        check['errors'].append('写工具必须声明 approval_required')
    check['valid'] = not check['errors']
    old = store.get('agent-' + name)
    return store.put({'id': uid('draft'), 'kind': 'draft', 'name': name, 'config': cfg, 'check': check,
                       'before': old['config'] if old else None, 'base_version': old['version'] if old else None,
                       'status': 'draft', 'description': description, 'standard': STANDARD})


def export(name):
    report = inspect(name)
    return json.dumps({'format': 'cloudops-agent-config/1', 'scope': 'configuration-snapshot-not-full-source',
                       'report': report}, ensure_ascii=False, indent=2)
