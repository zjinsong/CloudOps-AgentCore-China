"""Plan against verified task abilities; persist dependencies before dispatch."""
import json
from control import domain, messages, capabilities, routine_results
from control.results import read as read_result
from control.store import store
from framework.aggregation import aggregate_results


def execute_stage(t, c, context):
    from control.worker import owned, model, finish, report
    if t['phase'] == 'plan':
        report(t, '已接收，正在根据可用能力安排分工。', '安排专家')
        members=[a for a in store.all('agent') if a['name'] in c['members'] and a['name']!='supervisor' and a['enabled']]
        explicit=t.get('explicit_owners',[])
        if set(explicit)-{a['name'] for a in members}:raise PermissionError('指定专家已停用或未授权')
        abilities=capabilities.available(members,store)
        if explicit:abilities=[a for a in abilities if a['expert'] in explicit]
        # Platform abilities run in the control plane and are always available to the Supervisor
        # unless the user explicitly assigned experts.
        if not explicit:abilities=abilities+[routine_results.ABILITY]
        if not abilities:
            finish(t,'当前授权团队没有已校验且可用的任务能力，未启动专家。请检查工具挂载和能力清单。')
            return
        summary=[{k:a[k] for k in ('id','expert','description','inputs','outputs','regions','access')} for a in abilities]
        history=messages.list_for(c['id'],6,before=t['created_at'])
        request='任务：'+t['prompt']+'\n用户指定专家：'+json.dumps(explicit)+'\n可用能力：'+json.dumps(summary,ensure_ascii=False)+'\n附件背景：'+context[:8000]
        request+='\n历史仅用于解析指代，不能代替本次实时查询：\n'+'\n'.join(m['role']+': '+m['text'][:1500] for m in history)
        system=('你是 CloudOps Supervisor。按实际能力规划，不按专家名称猜工具。只输出 JSON：'
                '{"steps":[{"agent":"专家名","capability":"能力id","prompt":"具体目标与范围",'
                '"deliverables":["能力outputs中的数据名"],"region":"cn-northwest-1","depends_on":[]}],"answer":"无需执行或无法完成时的说明"}。'
                '最多六步。同一专家可执行多个不同能力，但同一能力只能一次；有先后数据依赖时必须用 depends_on。只选择可用能力；region 要匹配能力范围。'
                '用户指定专家时必须保留全部指定专家，并给每位不同且有用的分工；不要把同一原始问题复制给每位。'
                '先想最终答案需要哪些数据。下游需要上游资源标识或证据时必须使用 depends_on，取前面步骤的0起始序号；独立调查并行。'
                '分派前从历史解析这一个、上次、同期等指代，专家需要自包含目标，不要只发含糊指代。'
                '工具查实时数据，文档能力仅给定义和解释，不能替代实时证据。默认只选一位，必要的数据依赖或专业分工才选多位。'
                '不得把工具不存在或能力不覆盖伪装成服务故障；无法满足时说明缺口，不编造实时结果。'
                '基础设施变更只准备具体plan，apply仍需用户审批。平台历史已有专门能力。'
                '汇总、回顾或对比定时任务/巡检输出时，使用 expert 为 supervisor 的 read_routine_results，不要让专家重新查询；'
                '需要最新实时数据时才另派专家。'
                '简短问答输出空steps和answer，用户指定专家时不能跳过指定专家。')
        error=''
        for attempt in range(2):
            raw=model(request+('\n上次计划校验错误，请只修正计划：'+error if error else ''),system,max_tokens=3072)
            try:
                value=json.loads(raw.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip())
                steps=value['steps']
                if not steps and not explicit and isinstance(value.get('answer'),str) and value['answer'].strip():
                    finish(t,value['answer'].strip());return
                capabilities.validate_steps(steps,abilities,explicit)
                if not steps:raise ValueError('没有可执行计划或完整回答')
                break
            except (ValueError,KeyError,TypeError) as e:
                error=str(e)
                if attempt:raise ValueError('能力计划校验失败：'+error) from None
        child_ids=[f"{t['id']}-step-{i}" for i in range(len(steps))]
        by_id={a['id']:a for a in abilities}
        for step in steps:
            cap=by_id[step['capability']]
            if cap.get('platform'):continue
            step['prompt']+='\n本次能力：'+cap['description']+'\n交付：'+', '.join(step['deliverables'])
            step['prompt']+='\n可用工具：'+', '.join(cap['tools'])+'\n'+cap.get('guidance','')
            if cap['regions']:step['prompt']+='\n查询区域：'+step.get('region','cn-northwest-1')
            step['prompt']+='\n仅完成分派目标，准确区分实际数据、文档依据、空数据和工具错误；历史回答不能代替本次查询。'
        t=owned(t,lambda x:{**x,'plan':steps,'children':child_ids,'phase':'spawn','planning_mode':'capability',
                            'capability_snapshot':[a['id'] for a in abilities]})
        spawn(t,c)
    elif t['phase']=='spawn':
        spawn(t,c)
    elif t['phase']=='aggregate':
        aggregate(t)


def aggregate(t):
    from control.worker import model, finish, report
    report(t,'专家执行已结束，正在核对目标与证据。','核对结果')
    t=store.get(t['id'])
    platform=t.get('platform_results',{})
    subs=[store.get(x) for i,x in enumerate(t['children']) if str(i) not in platform]
    evidence=[capabilities.result_envelope(s) for s in subs]
    evidence+=[{'agent_name':'supervisor','status':'success','data':json.dumps(v,ensure_ascii=False)[:30000],
                'expert':'supervisor','capability':routine_results.ABILITY['id'],'execution_status':'succeeded',
                'tool_evidence':[{'name':'platform.read_routine_results','status':'success'}]} for v in platform.values()]
    answer=model('原始目标：'+t['prompt']+'\n用户补充：'+t.get('supplement','')+'\n实际分工：'+
                 json.dumps(t['plan'],ensure_ascii=False)+'\n结果分类：'+json.dumps(aggregate_results(evidence),ensure_ascii=False)[:35000]+
                 '\n工具证据：'+json.dumps([{k:e[k] for k in ('expert','capability','execution_status','tool_evidence')} for e in evidence],ensure_ascii=False)[:15000],
                 '你是 CloudOps Supervisor。检查交付是否回答原始目标，简洁汇总实际数值、范围、证据与明确缺口。'
                 '先给结论和关键数值，通常150至400字；不要逐项复述交付检查、达成度和原始问题。'
                 '只指出影响原始目标的缺口，用户未要求的额外指标不算缺口。'
                 '单次工具成功不能概括为整个链路没有重试或网络错误，不要作这种未核验承诺。'
                 '执行成功不等于目标全部完成，部分完成必须标明。不得把一位专家工具范围说成平台整体能力。'
                 '定时任务输出是已保存的历史结果，注明各自运行时间，不得说成本次实时查询；没有运行记录的定时任务要明确列出。'
                 '参数/适配器错误不能推断AWS服务故障；空数据不等于零。文档定义不能冒充账户查询。'
                 '不声称未执行的部署或审批已完成，不要求开放已有接口，不编造待决事项，不展示内部推理。',task=t)
    finish(t,answer,'failed' if any(s['status']=='failed' for s in subs) else 'succeeded')


def spawn(t,c):
    from control.worker import owned, model, finish
    if store.get(t['id'])['status'] in ('paused','cancelled'):return
    steps=t['plan']
    if not steps:
        history=[messages.public(m) for m in store.scoped('message',c['id']) if m['created_at']<=t['created_at']][-12:]
        finish(t,model('历史仅供背景：\n'+'\n'.join(m['role']+': '+m['text'][:1500] for m in history)+'\n当前任务：'+t['prompt'],task=t));return
    current={a['id']:a for a in capabilities.available([a for a in store.all('agent') if a['name'] in c['members']],store)}
    platform=dict(t.get('platform_results',{}))
    for i,step in enumerate(steps):
        if step.get('capability')==routine_results.ABILITY['id'] and str(i) not in platform:
            data=routine_results.query(t.get('user_id','admin'),t['prompt'],exclude_routine=t.get('routine_id'))
            platform[str(i)]=data
            store.event(t,'读取定时任务输出：'+data['scope']+f"，{len(data['routines'])} 个定时任务",'supervisor')
    if platform!=t.get('platform_results',{}):
        t=owned(t,lambda x:{**x,'platform_results':platform})
    for i,step in enumerate(steps):
        if str(i) in platform:continue
        ident=t['children'][i]
        if not store.get(ident):
            if step.get('capability') and (step['capability'] not in current or current[step['capability']]['expert']!=step['agent']):
                raise PermissionError('能力已撤销，停止分派')
            deps=step.get('depends_on',[])
            prompt=step['prompt']
            upstream=[platform[str(j)] for j in deps if str(j) in platform]
            if upstream:prompt+='\n上游定时任务输出（平台已保存结果）：\n'+json.dumps(upstream,ensure_ascii=False)[:20000]
            domain.task(c,prompt,step['agent'],parent=t['id'],files=t['file_ids'],ident=ident,
                        dependencies=[t['children'][j] for j in deps if str(j) not in platform],
                        metadata={'capability_id':step.get('capability'),'deliverables':step.get('deliverables',[])})
            store.event(t,'分派给 '+step['agent']+'：'+step['prompt'],step['agent'])
    if all(str(i) in platform for i in range(len(steps))):
        return aggregate(store.get(t['id']))
    owned(t,lambda x:{**x,'phase':'aggregate','status':'waiting_for_dependencies','lease_until':0}
          if x['status'] not in ('paused','cancelled') else x)
