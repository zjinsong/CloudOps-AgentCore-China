"""Read this user's platform Terraform history without an AWS audit/model roundtrip."""
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from control.store import store


def requested(prompt, owner):
    text = prompt.lower()
    if owner not in ('supervisor', 'terraform') or (owner != 'terraform' and not re.search(r'terraform|本平台|平台.*(部署|审批)', text)):
        return False
    if re.search(r'cloudtrail|aws\s*操作|谁.*(删|改)|生成|创建|修改|删除|更新|销毁|批准执行|帮我部署|请部署|\b(apply|destroy)\b', text):
        return False
    return bool(re.search(r'部署|deployment|deploy|计划|plan|审批|批准', text) and re.search(r'今天|今日|昨天|历史|最近|记录|上次|待决|待批|前.*次|history|recent|last', text))


def query(user_id, prompt='', limit=20):
    """Owned, bounded public metadata. This never creates approvals or runs Terraform."""
    limit = max(1, min(100, int(limit)))
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    date = None
    if re.search(r'今天|今日|\btoday\b', prompt, re.I):
        date = now.date()
    elif re.search(r'昨天|\byesterday\b', prompt, re.I):
        date = (now - timedelta(days=1)).date()
    else:
        match = re.search(r'\b(20\d{2}-\d{2}-\d{2})\b', prompt)
        if match:
            date = datetime.strptime(match[1], '%Y-%m-%d').date()
    start = datetime.combine(date, datetime.min.time(), ZoneInfo('Asia/Shanghai')).timestamp() if date else None
    def included(ts):
        return start is None or start <= ts < start + 86400
    projects = {p['id']: p for p in store.all('terraform') if p.get('user_id', 'admin') == user_id}
    deployments, plans, approvals = [], [], []
    for p in projects.values():
        applied = bool(p.get('apply_started_at') or p.get('status') in ('succeeded', 'applying', 'needs_review') or p.get('result', {}).get('status') == 'applied')
        when = p.get('apply_started_at') or p.get('finished_at') or p.get('created_at', 0)
        public = {k: p[k] for k in ('id', 'name', 'status', 'created_at', 'apply_started_at', 'finished_at', 'planned_at') if k in p}
        public['recorded_at'] = when
        if included(when):
            (deployments if applied else plans).append(public)
    for a in store.all('approval'):
        if a.get('user_id', 'admin') != user_id:
            continue
        p = projects.get(a.get('action', {}).get('project_id'))
        if not p:
            continue
        when = a.get('decided_at') or a.get('created_at', 0)
        if not included(when):
            continue
        action = a.get('action', {})
        from control.security import fingerprint
        valid = (a.get('status') == 'pending' and a.get('expires_at', 0) > now.timestamp()
                 and p.get('status') == 'planned' and p.get('plan_expires_at', 0) > now.timestamp()
                 and action.get('plan_sha256') == p.get('plan_sha256')
                 and action.get('config_fingerprint') == fingerprint(p.get('configuration', {}))
                 and a.get('fingerprint') == fingerprint(action))
        approvals.append({'id': a['id'], 'project_id': p['id'], 'status': a.get('status'),
                          'recorded_at': when, 'actionable': valid,
                          'resource_change_count': len(p.get('changes', []))})
    counts = {'deployments': len(deployments), 'plans': len(plans), 'approvals': len(approvals),
              'pending_actionable_approvals': sum(a['actionable'] for a in approvals)}
    def page(rows):
        return sorted(rows, key=lambda x: x['recorded_at'], reverse=True)[:limit]
    return {'source': '当前用户在本平台保存的记录；不包含平台外 AWS 操作', 'timezone': 'Asia/Shanghai',
            'date': str(date) if date else None, 'counts': counts, 'limit': limit,
            'has_more': any(len(r) > limit for r in (deployments, plans, approvals)),
            'deployments': page(deployments), 'plans': page(plans), 'approvals': page(approvals)}


def read(task):
    text = task['prompt'].lower()
    if re.search(r'今天|今日|昨天|审批|批准|待决|待批|20\d{2}-\d{2}-\d{2}|\btoday\b|\byesterday\b', text):
        evidence = query(task.get('user_id', 'admin'), text)
        counts = evidence['counts']
        scope = evidence['date'] or '现存历史'
        lines = [f'已查询当前用户的本平台记录（{scope}，北京时间）：', '',
                 f"- 实际部署执行记录：{counts['deployments']} 条。",
                 f"- 未部署的计划记录：{counts['plans']} 条。",
                 f"- 审批记录：{counts['approvals']} 条，其中有效待审批计划 {counts['pending_actionable_approvals']} 项。", '']
        if not counts['pending_actionable_approvals']:
            lines.append('当前查询范围内没有有效待审批计划，无需确认执行。')
        else:
            lines.append('审批单数量不等于资源变更数量；执行需用户确认具体有效 plan，本次仅查询。')
        for label, key in [('部署执行', 'deployments'), ('计划', 'plans'), ('审批', 'approvals')]:
            rows = evidence[key]
            if not rows:
                continue
            lines += ['', f'{label}记录：', '', '| 记录 | 时间（北京时间） | 状态 |', '| --- | --- | --- |']
            for row in rows:
                name = row.get('name', row['id']).replace('|', '\\|').replace('\n', ' ')
                when = datetime.fromtimestamp(row['recorded_at'], ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
                state = row['status']
                if key == 'approvals' and state == 'pending':
                    state = '有效待审批' if row['actionable'] else '已过期或 plan 已失效，不可执行'
                lines.append(f'| {name} | {when} | {state} |')
        if evidence['has_more']:
            lines += ['', f"各类只列最近 {evidence['limit']} 条，计数覆盖当前查询范围。"]
        lines += ['', '来源：当前用户在本平台现存的跨会话记录，按平台保留策略保存；不代表平台外 AWS 操作。计划不等于已部署，审批不等于部署成功。']
        return '\n'.join(lines)
    count = 2
    match = re.search(r'(?:最近|前|last|recent)\s*([0-9]+|一|两|二|三|四|五|六|七|八|九|十)', text)
    if match:
        n = match[1]
        count = min(10, max(1, int(n) if n.isdigit() else {'一':1,'两':2,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}[n]))
    user = task.get('user_id', 'admin')
    projects = [p for p in store.all('terraform') if p.get('user_id', 'admin') == user]
    deploys = [p for p in projects if p.get('apply_started_at') or p['status'] in ('succeeded','applying','needs_review') or p.get('result',{}).get('status')=='applied']
    plans = bool(re.search(r'计划|\bplan\b', text))
    def recorded_time(p):return p.get('apply_started_at') or p.get('finished_at') or p['created_at']
    rows = sorted(projects if plans else deploys, key=recorded_time, reverse=True)[:count]
    if not projects:
        return '当前用户还没有平台 Terraform 记录。来源：本平台保存的执行历史；不包含在平台外进行的 AWS 操作。'
    heading = f'最近 {count} 次 Terraform 部署执行' if not plans else f'最近 {count} 项 Terraform 计划'
    if not rows and not plans:
        heading = '当前用户没有实际部署执行记录。以下是最近的计划记录，不能视为部署成功'
        rows = sorted(projects, key=lambda p:p['created_at'], reverse=True)[:count]
    approvals = [a for a in store.all('approval') if a.get('user_id','admin') == user]
    labels = {'succeeded':'部署成功','applying':'正在部署','needs_review':'需核对执行结果','failed':'失败','planned':'plan 已生成，尚未部署','planning':'生成 plan 中','plan_queued':'等待生成 plan'}
    lines = [heading + '：', '', '| 项目 | 执行／记录时间（北京时间） | 状态 | 批准情况 |', '| --- | --- | --- | --- |']
    for p in rows:
        approval = next((a for a in reversed(approvals) if a.get('action',{}).get('project_id') == p['id']), None)
        state = labels.get(p['status'],p['status'])
        if p['status']=='failed':state='部署失败' if p.get('apply_started_at') else '计划失败，未部署'
        decision = {'pending':'等待确认','approved':'已批准','executing':'已批准','succeeded':'已批准','rejected':'已拒绝','needs_review':'已批准，结果待核对'}.get((approval or {}).get('status'),'历史确认记录缺失' if p['status']=='succeeded' else '无确认记录')
        if approval and approval.get('status') == 'pending' and approval.get('expires_at', 0) <= time.time():
            decision = '确认已过期，不可执行'
        when = datetime.fromtimestamp(recorded_time(p),ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M:%S')
        name = p.get('name',p['id']).replace('|','\\|').replace('\n',' ')
        lines.append(f'| {name} | {when} | {state} | {decision} |')
    lines += ['', '来源：当前用户在本平台保存的 Terraform 计划、审批和部署执行记录。平台外的 AWS 操作需另查 CloudTrail。']
    return '\n'.join(lines)
