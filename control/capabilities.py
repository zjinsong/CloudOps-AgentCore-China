"""Task abilities are constrained by enabled agents, mounted tools and inventory."""
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {'cost-explorer':'cost_explorer','pricing':'pricing','cloudwatch':'cloudwatch',
           'health-events':'health_events','cloudtrail-mcp':'cloudtrail','terraform-mcp':'terraform',
           'aws-api':'aws_api','agentcore-mcp':'agentcore'}
PLATFORM = {'terraform':{'prepare_terraform','read_platform_terraform_history'},
            'cloudtrail':{'read_platform_terraform_history'}}


def definitions(agents):
    rows=[{**row,'expert':agent['name']} for agent in agents for row in agent.get('config',{}).get('capabilities',[])]
    seen=set()
    for row in rows:
        if row['id'] in seen or not row['tools'] or row['access'] not in {'read_only','documentation','plan_only'}:
            raise ValueError('Invalid capability definition: '+row['id'])
        seen.add(row['id'])
    return rows


def schema_tools():
    names=set()
    for target,folder in TARGETS.items():
        for tool in json.loads((ROOT/'tools'/folder/'tools.json').read_text(encoding='utf-8')):
            names.add(target+'___'+tool['name'])
    return names


def catalog(agents, db):
    inventory=db.get('capability-tool-inventory')
    # Production uses the actual Gateway inventory. Offline tests validate
    # against checked-in schemas; do not claim these were live-loaded.
    live=bool(inventory)
    names=set(inventory['tools']) if live else (set() if db.table_name else schema_tools())
    by_name={a['name']:a for a in agents}
    rows=[]
    for definition in definitions(agents):
        agent=by_name.get(definition['expert'])
        mounted=set((agent or {}).get('config',{}).get('tools',[]))
        platform=set(PLATFORM.get(definition['expert'],set()))
        if os.getenv('CLOUDOPS_ENABLE_TERRAFORM_PLAN')!='true':platform.discard('prepare_terraform')
        missing=[name for name in definition['tools'] if name not in platform and
                 (name not in names or name.split('___')[0] not in mounted)]
        enabled=bool(agent and agent.get('enabled') and (agent.get('runtime_arn') or not db.table_name))
        rows.append({**definition,'available':enabled and not missing,'missing_tools':missing,
                     'verification':{'status':'gateway_registered' if live else 'schema_only',
                                     'checked_at':(inventory or {}).get('checked_at')},
                     'unavailable_reason':'expert_disabled_or_missing' if not enabled else ('tool_not_loaded' if missing else '')})
    return rows


def available(agents, db):
    return [row for row in catalog(agents,db) if row['available']]


def validate_steps(steps, abilities, explicit):
    if not isinstance(steps,list) or len(steps)>6:
        raise ValueError('协调计划需要最多六个步骤')
    by_id={row['id']:row for row in abilities}
    # An expert can take more than one *different* capability in a DAG.  For
    # example, Monitoring must first discover EC2 instance IDs and then query
    # their metrics.  A capability itself may only occur once per plan, which
    # prevents an unbounded duplicate fan-out.
    seen=set()
    for i,step in enumerate(steps):
        cap=by_id.get(step.get('capability'))
        if not cap or step.get('agent')!=cap['expert'] or cap['id'] in seen:
            raise ValueError('计划引用不可用能力、错误专家或重复能力')
        if not isinstance(step.get('prompt'),str) or not step['prompt'].strip():raise ValueError('缺少调查目标')
        deps=step.get('depends_on',[])
        if not isinstance(deps,list) or any(type(d) is not int or not 0<=d<i for d in deps):raise ValueError('任务依赖无效')
        if not isinstance(step.get('deliverables'),list) or not step['deliverables'] or any(x not in cap['outputs'] for x in step['deliverables']):
            raise ValueError('交付数据与能力定义不一致')
        if cap['regions'] and step.get('region','cn-northwest-1') not in cap['regions']:
            raise ValueError('能力不支持所选区域')
        seen.add(cap['id'])
    if set(explicit)-{step['agent'] for step in steps}:raise ValueError('未保留用户指定专家')
    return steps


def record_inventory(db, names):
    row={'id':'capability-tool-inventory','kind':'capability_inventory','tools':sorted(set(names)),
         'checked_at':time.time()}
    old=db.get(row['id'])
    return db.put(row,old['version'] if old else None)


def result_envelope(task):
    from control.results import read
    response=read(task)[:12000]
    return {'agent_name':task['owner'],'status':'success' if task['status']=='succeeded' else 'error',
            'data':response,'error_message':task.get('error') or (response if task['status']!='succeeded' else ''),
            'task_id':task['id'],'expert':task['owner'],'capability':task.get('capability_id'),
            'execution_status':task['status'],'requested_deliverables':task.get('deliverables',[]),
            'response':response,'error':task.get('error'),
            'tool_evidence':[{'name':r.get('name'),'status':r.get('status')}
                             for e in task.get('evidence',[]) for r in e.get('tool_results',[])]}
