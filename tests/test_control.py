import concurrent.futures
import hashlib
import json
import os
import struct
import time
import datetime as dt
import io
import threading
import signal
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock

os.environ.update(SESSION_SECRET='unit-test-session-secret-at-least-32-characters', CLOUDOPS_ENABLE_APPLY='true', CLOUDOPS_ENABLE_TERRAFORM_PLAN='true', CLOUDOPS_ENABLE_DEFAULT_ROUTINES='true', ADMIN_PASSWORD='test-password', CLOUDOPS_USERS='{"admin":"test-password","alice":"alice-password","bob":"bob-password"}', CLOUDOPS_LOCAL_WS='test', AWS_EC2_METADATA_DISABLED='true')
from fastapi.testclient import TestClient
from control import api, domain, worker, executor, execution, terraform, registry, messages, schedules, titles, deploy_history, routine_results
from control.store import Store, Conflict
from control.transcribe import frame, unframe
from control.security import fingerprint
from framework.validator import validate_hierarchy


class ControlTests(unittest.TestCase):
    def test_expert_ack_without_execution_has_bounded_wait(self):
        t = worker.claim(self.task())
        self.db.update('agent-finops', lambda x: {**x, 'runtime_arn': 'test-arn'})
        dp = Mock()
        dp.invoke_agent_runtime.return_value = {'response': io.BytesIO(b'{"result": {}}')}
        with patch.object(worker.boto3, 'client', return_value=dp), \
             patch.object(execution, 'get', return_value=None), \
             patch.object(worker.time, 'monotonic', side_effect=[0, 1, 91]), \
             patch.object(worker.time, 'sleep'):
            with self.assertRaisesRegex(TimeoutError, '90秒'):
                worker.call_expert(t, self.c, '')
        self.assertEqual(dp.invoke_agent_runtime.call_count, 1)

    def test_capabilities_follow_enabled_experts_mounted_tools_and_gateway_inventory(self):
        from control import capabilities
        names=capabilities.schema_tools()
        capabilities.record_inventory(self.db,names)
        def rows():return {r['id']:r for r in capabilities.catalog(self.db.all('agent'),self.db)}
        self.assertTrue(rows()['query_resource_metrics']['available'])
        self.assertEqual(rows()['query_resource_metrics']['verification']['status'],'gateway_registered')
        capabilities.record_inventory(self.db,names-{'aws-api___call_aws'})
        self.assertFalse(rows()['query_resource_metrics']['available'])
        self.assertTrue(rows()['inspect_alarms_health']['available'])
        capabilities.record_inventory(self.db,names)
        self.db.update('agent-monitoring',lambda x:{**x,'enabled':False})
        self.assertFalse(rows()['query_resource_metrics']['available'])
        self.db.update('agent-agentcore',lambda x:{**x,'config':{**x['config'],'tools':[]}})
        self.assertFalse(rows()['discover_agentcore_runtimes']['available'])

    def test_capability_plan_rejects_missing_explicit_expert_unknown_tools_and_invalid_dependencies(self):
        from control import capabilities
        abilities=capabilities.available(self.db.all('agent'),self.db)
        good={'agent':'agentcore','capability':'discover_agentcore_runtimes','prompt':'查资源',
              'deliverables':['runtime_resources'],'depends_on':[]}
        for step,explicit in (({**good,'capability':'invented'},[]),({**good,'agent':'monitoring'},[]),
                              ({**good,'depends_on':[0]},[]),({**good,'region':'cn-north-1'},[]),
                              ({**good,'deliverables':['invented']},[]),(good,['agentcore','monitoring'])):
            with self.assertRaises(ValueError):capabilities.validate_steps([step],abilities,explicit)

    def test_capability_plan_allows_one_expert_to_discover_then_query(self):
        from control import capabilities
        abilities=capabilities.available(self.db.all('agent'),self.db)
        plan=[
            {'agent':'monitoring','capability':'discover_monitoring_resources','prompt':'列出两台运行中的 EC2',
             'deliverables':['resource_identifiers'],'depends_on':[]},
            {'agent':'monitoring','capability':'query_resource_metrics','prompt':'使用上游实例 ID 查询 CPU',
             'deliverables':['metric_evidence'],'depends_on':[0]},
            {'agent':'cloudtrail','capability':'query_audit_events','prompt':'查询今天关机操作',
             'deliverables':['audit_evidence'],'depends_on':[]},
        ]
        capabilities.validate_steps(plan,abilities,[])
        duplicate=plan+[{**plan[1],'depends_on':[0,1]}]
        with self.assertRaises(ValueError):capabilities.validate_steps(duplicate,abilities,[])

    def test_capability_planner_repairs_once_before_dispatch(self):
        from agents.supervisor import coordinator
        c=domain.create_conversation('alice','supervisor')
        t=worker.claim(domain.task(c,'查看运行状态','supervisor',metadata={'explicit_owners':['agentcore']}))
        good={'steps':[{'agent':'agentcore','capability':'discover_agentcore_runtimes','prompt':'查询当前资源',
                        'deliverables':['runtime_resources'],'depends_on':[]}]}
        with patch.object(coordinator,'store',self.db),patch.object(worker,'model',side_effect=['{"steps":[]}',json.dumps(good)]) as model:
            coordinator.execute_stage(t,c,'')
            self.assertEqual(model.call_count,2)
            self.assertIn('上次计划校验错误',model.call_args.args[0])
        child=self.db.get(t['id']+'-step-0')
        self.assertEqual(child['user_id'],'alice')
        self.assertEqual(child['capability_id'],'discover_agentcore_runtimes')

    def test_supervisor_summarizes_routine_outputs_without_experts(self):
        from agents.supervisor import coordinator
        from control import routine_results
        r = self.db.get('routine-default-finops')
        run = domain.task(self.c, r['prompt'], 'finops', ident='task-routine-default-finops-1', metadata={'routine_id': r['id']})
        self.db.update(run['id'], lambda x: {**x, 'status': 'succeeded', 'result': '七天费用 12.3 CNY', 'finished_at': time.time()})
        mine = self.db.put({'id': 'routine-summary', 'kind': 'routine', 'name': '汇总', 'prompt': '汇总各专家定时任务输出',
                            'owner': 'supervisor', 'user_id': 'admin', 'enabled': True})
        data = routine_results.query('admin', '汇总今天定时任务', exclude_routine=mine['id'])
        names = {x['routine'] for x in data['routines']}
        self.assertNotIn('汇总', names)
        finops = [x for x in data['routines'] if x['expert'] == 'finops'][0]
        self.assertIn('12.3 CNY', finops['runs'][0]['result'])
        self.assertEqual(routine_results.query('bob')['routines'], [])
        old = domain.task(self.c, r['prompt'], 'finops', ident='task-routine-default-finops-0', metadata={'routine_id': r['id']})
        self.db.update(old['id'], lambda x: {**x, 'status': 'succeeded', 'result': '昨天费用 9.9 CNY', 'finished_at': time.time() - 86400})
        self.db.update(run['id'], lambda x: {**x, 'finished_at': time.time() - 86400 + 60})
        empty_day = [x for x in routine_results.query('admin', '汇总今天定时任务')['routines'] if x['expert'] == 'finops' and x['routine'] == r['name']][0]
        self.assertIn('最近一次', empty_day['note'])
        self.assertEqual(len(empty_day['runs']), 1)
        self.db.update(run['id'], lambda x: {**x, 'finished_at': time.time()})
        t = worker.claim(domain.task(self.c, '汇总各专家的定时任务输出', 'supervisor', metadata={'routine_id': mine['id']}))
        plan = {'steps': [{'agent': 'supervisor', 'capability': 'read_routine_results', 'prompt': '读取今天各定时任务输出',
                           'deliverables': ['routine_results'], 'depends_on': []}]}
        with patch.object(coordinator, 'store', self.db), patch.object(routine_results, 'store', self.db), \
             patch.object(worker, 'model', side_effect=[json.dumps(plan), '汇总：费用 12.3 CNY']) as model:
            coordinator.execute_stage(t, self.c, '')
        done = self.db.get(t['id'])
        self.assertEqual(done['status'], 'succeeded')
        self.assertIn('12.3 CNY', model.call_args_list[1].args[0])
        self.assertFalse([x for x in self.db.all('task') if x.get('parent_id') == t['id']])

    def test_routine_results_feed_dependent_expert_step(self):
        from agents.supervisor import coordinator
        from control import routine_results
        t = worker.claim(domain.task(self.c, '根据定时巡检结果深入查询费用', 'supervisor'))
        plan = {'steps': [{'agent': 'supervisor', 'capability': 'read_routine_results', 'prompt': '读取最近巡检',
                           'deliverables': ['routine_results'], 'depends_on': []},
                          {'agent': 'finops', 'capability': 'analyze_cost_usage', 'prompt': '基于巡检深入查询',
                           'deliverables': ['cost_evidence'], 'depends_on': [0]}]}
        from control import capabilities
        capabilities.record_inventory(self.db, capabilities.schema_tools())
        abilities = {a['id']: a for a in capabilities.available(self.db.all('agent'), self.db)}
        if 'analyze_cost_usage' not in abilities:
            cap = [a for a in abilities.values() if a['expert'] == 'finops'][0]
            plan['steps'][1].update(capability=cap['id'], deliverables=cap['outputs'][:1])
        with patch.object(coordinator, 'store', self.db), patch.object(routine_results, 'store', self.db), \
             patch.object(worker, 'model', return_value=json.dumps(plan)):
            coordinator.execute_stage(t, self.c, '')
        child = self.db.get(t['id'] + '-step-1')
        self.assertEqual(child['dependencies'], [])
        self.assertIn('上游定时任务输出', child['prompt'])
        self.assertIsNone(self.db.get(t['id'] + '-step-0'))
        self.assertEqual(self.db.get(t['id'])['status'], 'waiting_for_dependencies')

    def test_terminal_supervisor_planning_failure_writes_user_visible_reply(self):
        task=worker.claim(domain.task(self.c,'查询实例指标','supervisor'))
        error=ValueError('能力计划校验失败：计划引用不可用能力')
        with patch('agents.supervisor.coordinator.execute_stage',side_effect=error):
            worker.execute(task)
        retry=self.db.get(task['id'])
        self.assertEqual(retry['status'],'retry_scheduled')
        self.assertEqual(messages.list_for(self.c['id']),[])
        self.db.update(task['id'],lambda x:{**x,'retry_at':0})
        with patch('agents.supervisor.coordinator.execute_stage',side_effect=error):
            worker.execute(worker.claim(self.db.get(task['id'])))
        final=self.db.get(task['id'])
        self.assertEqual(final['status'],'failed')
        self.assertIn('能力计划校验失败',final['error'])
        reply=messages.list_for(self.c['id'])[-1]
        self.assertEqual(reply['role'],'assistant')
        self.assertIn('没有启动专家或工具调用',reply['text'])

    def test_cloudwatch_cli_normalization_preserves_readonly_boundary(self):
        from tools.aws_api.policy import arguments
        for command in ('cloudwatch list-metrics --namespace AWS/Bedrock-AgentCore --max-results 20',
                        'aws cloudwatch list-metrics --namespace AWS/Bedrock-AgentCore --max-results=20'):
            args=arguments('call_aws',{'cli_command':command})
            self.assertTrue(args['cli_command'].startswith('aws cloudwatch list-metrics '))
            self.assertNotIn('--max-results',args['cli_command'])
            self.assertEqual(args['max_results'],20)
        for command in ('lambda delete-function --function-name x','cloudwatch list-metrics --region us-east-1',
                        'cloudwatch list-metrics --profile admin','cloudwatch list-metrics ; aws lambda delete-function'):
            with self.assertRaises(PermissionError):arguments('call_aws',{'cli_command':command})
        from tools.mcp_lambda import handler
        ctx=SimpleNamespace(client_context=SimpleNamespace(custom={'bedrockAgentCoreToolName':'aws-api___call_aws'}))
        with patch.dict(os.environ,{'MCP_TARGET':'aws-api'}),patch.object(handler,'Bridge') as bridge:
            result=handler.handler({'cli_command':'bedrock-agent list-agents'},ctx)
            self.assertEqual(result['error']['code'],'CommandRejected')
            self.assertEqual(result['error']['source'],'mcp-adapter')
            bridge.assert_not_called()

    def test_runtime_metrics_collaboration_passes_metadata_to_monitoring(self):
        from agents.supervisor import coordinator
        c=domain.create_conversation('alice','supervisor')
        t=worker.claim(domain.task(c,'请协作看看agentcore runtime的cloudwatch指标','supervisor',
                                 metadata={'explicit_owners':['monitoring','agentcore']}))
        plan={'steps':[{'agent':'agentcore','capability':'discover_agentcore_runtimes','prompt':'列举资源','deliverables':['runtime_resources'],'depends_on':[]},
                       {'agent':'monitoring','capability':'query_resource_metrics','prompt':'使用上游ARN查指标','deliverables':['metric_evidence'],'depends_on':[0]}]}
        with patch.object(coordinator,'store',self.db),patch.object(worker,'model',return_value=json.dumps(plan)) as model:
            coordinator.execute_stage(t,c,'')
            self.assertIn('可用能力',model.call_args.args[0])
        first=self.db.get(t['id']+'-step-0');second=self.db.get(t['id']+'-step-1')
        self.assertEqual(first['owner'],'agentcore')
        self.assertEqual(second['owner'],'monitoring')
        self.assertEqual(second['dependencies'],[first['id']])
        self.assertEqual(second['user_id'],'alice')
        self.assertEqual(second['capability_id'],'query_resource_metrics')
        self.assertEqual(self.db.get(t['id'])['planning_mode'],'capability')

    def test_aws_api_rejects_mutation_credentials_files_and_non_china(self):
        from tools.aws_api.policy import arguments
        for command in ('aws lambda delete-function --function-name x',
                        'aws sts assume-role --role-arn arn:aws-cn:iam::123:role/x',
                        'aws secretsmanager get-secret-value --secret-id x',
                        'aws lambda list-functions --region us-east-1',
                        'aws lambda list-functions --region=us-east-1 --region cn-northwest-1',
                        'aws lambda list-functions --profile admin',
                        'aws lambda list-functions --endpoint-url=https://example.com',
                        'aws lambda list-functions --query file:///tmp/x',
                        'aws lambda list-functions ; aws lambda delete-function'):
            with self.assertRaises(PermissionError, msg=command):arguments('call_aws',{'cli_command':command})
        self.assertIn('cn-northwest-1',arguments('call_aws',{'cli_command':'aws lambda list-functions'})['cli_command'])
        self.assertIn('cn-north-1',arguments('call_aws',{'cli_command':'aws lambda list-functions --region cn-north-1'})['cli_command'])
        safe=arguments('call_aws',{'cli_command':'aws lambda get-function-configuration --function-name x --query Environment.Variables --output text'})['cli_command']
        self.assertNotIn('--query',safe);self.assertNotIn('--output',safe)

    def test_agentcore_mcp_blocks_writes_and_redacts_nested_credentials(self):
        from tools.agentcore.policy import arguments, redact
        for name in ('create_agent_runtime','gateway_delete','identity_create_oauth2_provider','invoke_agent_runtime'):
            with self.assertRaises(PermissionError):arguments(name,{})
        with self.assertRaises(PermissionError):arguments('gateway_get',{'gateway_identifier':'arn:aws:bedrock-agentcore:us-east-1:123:gateway/g'})
        result=redact({'environmentVariables':{'API_KEY':'secret'},'text':json.dumps({'clientSecret':'secret','name':'safe'})})
        self.assertNotIn('secret',json.dumps(result));self.assertIn('safe',result['text'])

    def test_worker_connection_timeout_and_saved_result_prevent_resubmit(self):
        t=worker.claim(self.task())
        self.db.update('agent-finops',lambda x:{**x,'runtime_arn':'test-arn'})
        with patch.object(worker.boto3,'client') as client,patch.object(execution,'get',return_value={'status':'completed','result':'saved'}):
            self.assertEqual(worker.call_expert(t,self.c,''),'saved')
            self.assertEqual(client.call_args.kwargs['config'].connect_timeout,4)
            client.return_value.invoke_agent_runtime.assert_not_called()

    def test_worker_retry_is_short_and_bounded(self):
        t=worker.claim(self.task())
        before=time.time()
        with patch.object(worker,'call_expert',side_effect=TimeoutError('connection timeout')):
            worker.execute(t)
        current=self.db.get(t['id'])
        self.assertEqual(current['status'],'retry_scheduled')
        self.assertGreaterEqual(current['retry_at']-before,0.5)
        self.assertLess(current['retry_at']-before,2)
        self.db.update(t['id'],lambda x:{**x,'retry_at':0})
        with patch.object(worker,'call_expert',side_effect=TimeoutError('connection timeout')):
            worker.execute(worker.claim(self.db.get(t['id'])))
        self.assertEqual(self.db.get(t['id'])['status'],'failed')

    def test_worker_does_not_repeat_existing_terraform_plan(self):
        c=domain.create_conversation('admin','terraform')
        t=worker.claim(domain.task(c,'生成 S3 plan','terraform'))
        self.db.put({'id':'tf-existing','kind':'terraform','task_id':t['id'],'status':'planned'})
        with patch.object(worker,'call_expert',side_effect=TimeoutError('result unknown')):
            worker.execute(t)
        self.assertEqual(self.db.get(t['id'])['status'],'failed')

    def test_new_agentcore_expert_reaches_existing_supervisor_conversations(self):
        c=domain.create_conversation('alice','supervisor')
        self.db.update('agent-agentcore',lambda x:{**x,'enabled':False})
        registry.refresh_team()
        self.assertNotIn('agentcore',self.db.get(c['id'])['members'])
        from pathlib import Path
        config=json.loads(Path('hierarchy.json').read_text(encoding='utf-8'))['agents']['agentcore']
        registry.register('agentcore',config,'arn:aws-cn:bedrock-agentcore:cn-northwest-1:123:runtime/test',1)
        self.assertIn('agentcore',self.db.get(c['id'])['members'])
        from agents.supervisor import coordinator
        task=worker.claim(domain.task(self.db.get(c['id']),'查询 AgentCore runtimes','supervisor',metadata={'explicit_owners':['agentcore']}))
        plan={'steps':[{'agent':'agentcore','capability':'discover_agentcore_runtimes','prompt':'查询Runtime','deliverables':['runtime_resources'],'depends_on':[]}]}
        with patch.object(coordinator,'store',self.db),patch.object(worker,'model',return_value=json.dumps(plan)):
            coordinator.execute_stage(task,self.db.get(c['id']),'')
        self.assertEqual(self.db.get(task['id']+'-step-0')['owner'],'agentcore')
        self.assertEqual(self.db.get(task['id']+'-step-0')['user_id'],'alice')

    def test_price_questions_never_bypass_live_tools_after_greeting(self):
        from control import routing
        for prompt in ('你好，t3.medium 一小时多少钱？','你好，宁夏 t3.medium 的价格','hello, what is the price of t3.medium?'):
            self.assertFalse(routing.conversational({'owner':'finops','prompt':prompt}))
        self.assertTrue(routing.conversational({'owner':'finops','prompt':'什么是 Savings Plans？'}))

    def test_all_expert_business_tools_use_gateway_and_filter_other_targets(self):
        from framework import expert_tools
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        prefixes = ('cost-explorer','pricing','cloudwatch','health-events','cloudtrail-mcp','terraform-mcp')
        client.list_tools_sync.return_value = [SimpleNamespace(tool_name=p+'___read') for p in prefixes] + [SimpleNamespace(tool_name='cfn-exec___deploy')]
        with patch.dict(os.environ, {'GATEWAY_URL':'https://example.test/mcp'}), patch.object(expert_tools,'get_gateway_mcp_client',return_value=client) as gateway:
            for targets in (['cost-explorer','pricing'],['cloudwatch','health-events'],['cloudtrail-mcp'],['terraform-mcp']):
                with expert_tools.tools_for({'tools':targets}) as loaded:
                    self.assertEqual({t.tool_name for t in loaded},{p+'___read' for p in targets})
            self.assertEqual(gateway.call_count,4)
            client.list_tools_sync.return_value=[]
            with self.assertRaisesRegex(RuntimeError,'声明工具未加载：pricing'):
                with expert_tools.tools_for({'tools':['pricing']}): pass
        with patch.object(expert_tools,'get_gateway_mcp_client') as gateway:
            with expert_tools.tools_for({'tools':[]}) as loaded: self.assertEqual(loaded,[])
            gateway.assert_not_called()

    def test_mcp_backend_readonly_allowlists_and_pricing_query_limits(self):
        from tools.mcp_lambda.handler import ALLOWED as UPSTREAM_TOOLS, parameters as upstream_parameters, bounded_arguments
        self.assertNotIn('generate_cost_report',UPSTREAM_TOOLS['pricing'])
        self.assertEqual(UPSTREAM_TOOLS['cloudtrail-mcp'],{'lookup_events'})
        self.assertNotIn('execute_terraform_command',UPSTREAM_TOOLS['terraform-mcp'])
        with patch.dict(os.environ,{'AWS_PROFILE':'local-only','AWS_CONTAINER_CREDENTIALS_RELATIVE_URI':'/role'}):
            env=upstream_parameters('pricing').env
            self.assertNotIn('AWS_PROFILE',env)
            self.assertEqual(env['AWS_CONTAINER_CREDENTIALS_RELATIVE_URI'],'/role')
            self.assertEqual(env['PYTHONPATH'],'/opt/pricing-mcp')
        args=bounded_arguments('pricing','get_pricing',{'service_code':'AmazonEC2','max_results':1000,'max_allowed_characters':-1})
        self.assertEqual(args['max_results'],10)
        self.assertEqual(args['max_allowed_characters'],1000)
        self.assertEqual(args['output_options']['pricing_terms'],['OnDemand'])
        self.assertEqual(args['region'],'cn-northwest-1')
        args=bounded_arguments('pricing','get_pricing',{'regions':['cn-north-1','cn-northwest-1']})
        self.assertEqual(args['region'],['cn-north-1','cn-northwest-1'])
        with self.assertRaises(ValueError): bounded_arguments('pricing','get_pricing',{'regions':['x']*4})
        with self.assertRaises(PermissionError): bounded_arguments('pricing','generate_cost_report',{})
        self.assertEqual(bounded_arguments('cloudtrail-mcp','lookup_events',{'region':'us-east-1'})['region'],'cn-northwest-1')

    def test_lambda_mcp_reuses_warm_session_and_classifies_api_errors(self):
        from tools.mcp_lambda import handler
        ctx=SimpleNamespace(client_context=SimpleNamespace(custom={'bedrockAgentCoreToolName':'pricing___get_pricing'}))
        bridge=Mock()
        bridge.thread.is_alive.return_value=True
        bridge.invoke.return_value=SimpleNamespace(isError=False,structuredContent={'result':{'status':'success','price':'0.1917','currency':'CNY'}},content=[])
        with patch.dict(os.environ,{'MCP_TARGET':'pricing'}),patch.object(handler,'_bridge',None),patch.object(handler,'Bridge',return_value=bridge) as construct:
            self.assertEqual(handler.handler({'service_code':'AmazonEC2'},ctx)['currency'],'CNY')
            self.assertEqual(handler.handler({'service_code':'AmazonEC2'},ctx)['price'],'0.1917')
            construct.assert_called_once_with('pricing')
            self.assertEqual(bridge.invoke.call_args.args[1]['max_results'],10)
            self.assertEqual(bridge.invoke.call_args.args[1]['output_options']['pricing_terms'],['OnDemand'])
            bridge.invoke.return_value=SimpleNamespace(isError=False,structuredContent={'result':{'status':'error'}},content=[])
            with self.assertRaises(RuntimeError):handler.handler({'service_code':'AmazonEC2'},ctx)
            bad=SimpleNamespace(client_context=SimpleNamespace(custom={'bedrockAgentCoreToolName':'pricing___generate_cost_report'}))
            with self.assertRaises(PermissionError):handler.handler({},bad)

    def test_successful_retry_clears_stale_error_and_keeps_history(self):
        task=domain.task(self.c,'查资源','agentcore')
        self.db.update(task['id'],lambda x:{**x,'error':'Connection error.'})
        task=worker.claim(self.db.get(task['id']))
        self.db.event(task,'Connection error.',event_type='task.retry')
        worker.finish(task,'查询完成')
        self.assertEqual(self.db.get(task['id'])['status'],'succeeded')
        self.assertNotIn('error',self.db.get(task['id']))
        self.assertTrue(any(e.get('event_type')=='task.retry' for e in self.db.all('event')))

    def test_failed_dependency_blocks_downstream_without_invoking_expert(self):
        upstream=domain.task(self.c,'资源发现','agentcore')
        self.db.update(upstream['id'],lambda x:{**x,'status':'failed','error':'discovery unavailable'})
        downstream=domain.task(self.c,'查询指标','monitoring',dependencies=[upstream['id']])
        handlers={};finished=threading.Event();original=worker.finish
        def finish(t,*args,**kwargs):
            original(t,*args,**kwargs)
            finished.set();handlers[signal.SIGTERM](None,None)
        with patch.dict(os.environ,{'CLOUDOPS_QUEUE':''}),patch.object(worker.signal,'signal',side_effect=lambda sig,fn:handlers.update({sig:fn})),patch.object(domain,'seed'),patch.object(worker,'tick'),patch.object(worker,'maintain'),patch.object(titles,'pump',return_value={}),patch.object(worker,'execute') as execute,patch.object(worker,'finish',side_effect=finish):
            thread=threading.Thread(target=worker.main);thread.start()
            try:self.assertTrue(finished.wait(15),'failed dependency was not resolved')
            finally:
                if signal.SIGTERM in handlers:handlers[signal.SIGTERM](None,None)
                thread.join(5)
            execute.assert_not_called()
            self.assertFalse(thread.is_alive())
        result=self.db.get(downstream['id'])
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['blocked_by_dependency'],[upstream['id']])

    def test_worker_dispatch_continues_while_maintenance_is_blocked(self):
        task = domain.task(self.c, '你好，请用一句话介绍你的职责。', 'finops')
        entered, release, dispatched = threading.Event(), threading.Event(), threading.Event()
        handlers = {}
        failures = []
        def tick(maintenance=True):
            if maintenance:
                entered.set()
                release.wait(10)
        def execute(t):
            if not entered.wait(3):
                failures.append('maintenance did not start')
            dispatched.set()
            handlers[signal.SIGTERM](None, None)
        with patch.dict(os.environ, {'CLOUDOPS_QUEUE':''}), patch.object(worker.signal,'signal',side_effect=lambda sig,fn:handlers.update({sig:fn})), patch.object(domain,'seed'), patch.object(worker,'tick',side_effect=tick), patch.object(titles,'pump',return_value={}), patch.object(worker,'execute',side_effect=execute):
            thread = threading.Thread(target=worker.main)
            thread.start()
            try:
                self.assertTrue(dispatched.wait(5),'interactive dispatch blocked by maintenance')
                self.assertTrue(entered.is_set())
                self.assertFalse(release.is_set())
            finally:
                release.set()
                if signal.SIGTERM in handlers:
                    handlers[signal.SIGTERM](None,None)
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures,[])

    def test_archive_summarizes_conversation_and_preserves_manual_names(self):
        c = domain.create_conversation('alice', 'finops')
        messages.create(c, 'user', '检查最近七天费用，重点找 EC2 异常增长')
        messages.create(c, 'user', '再看看 EBS')
        messages.create(c, 'assistant', 'EC2 是费用最高服务，建议检查闲置实例。')
        domain.create_conversation('alice', 'finops', c['id'])
        job = self.db.get('conversation-title-' + c['id'])
        self.assertEqual(job['status'], 'queued')
        with patch.object(titles, 'summarize', return_value=('EC2 费用增长排查', '检查七天费用并定位 EC2 增长。')) as summarize:
            titles.execute(job['id'])
        self.assertIn('检查最近七天费用', summarize.call_args.args[0])
        self.assertNotIn('再看看 EBS', summarize.call_args.args[0])
        self.assertNotIn('EC2 是费用最高服务', summarize.call_args.args[0])
        named = self.db.get(c['id'])
        self.assertEqual(named['title'], 'EC2 费用增长排查')
        self.assertEqual(named['title_version'], 2)
        self.assertTrue(named['summary'])
        self.assertEqual(named['user_id'], 'alice')
        manual = domain.create_conversation('alice', 'monitoring')
        messages.create(manual, 'user', '检查当前告警')
        titles.enqueue(manual)
        def rename_during_model(text):
            self.db.update(manual['id'], lambda x: {**x, 'title_manual': True, 'title': '我自己的标题'})
            return '模型新名字', '摘要'
        with patch.object(titles, 'summarize', side_effect=rename_during_model):
            titles.execute('conversation-title-' + manual['id'])
        self.assertEqual(self.db.get(manual['id'])['title'], '我自己的标题')

    def test_delete_history_is_owned_hidden_and_cannot_resume(self):
        c = domain.create_conversation('admin', 'finops')
        task = domain.task(c, '检查费用')
        messages.create(c, 'user', '检查费用')
        bob = TestClient(api.app)
        bob.post('/api/login', json={'username': 'bob', 'password': 'bob-password'})
        self.assertEqual(bob.delete('/api/conversations/' + c['id'], headers=self.headers).status_code, 403)
        self.assertEqual(self.client.delete('/api/conversations/' + c['id'], headers=self.headers).status_code, 200)
        self.assertNotIn(c['id'], [v['id'] for v in self.client.get('/api/bootstrap').json()['conversations']])
        self.assertEqual(self.client.get('/api/conversations/' + c['id']).status_code, 403)
        self.assertEqual(self.client.post('/api/conversations/' + c['id'] + '/activate', json={}, headers=self.headers).status_code, 403)
        self.assertNotIn('finops', self.db.get('preferences-admin')['active'])
        self.assertEqual(self.db.get(task['id'])['status'], 'queued')
        self.assertEqual(domain.require_conversation(c['id'])['id'], c['id'])

    def test_empty_conversations_do_not_fill_history(self):
        for _ in range(5):
            domain.create_conversation('admin', 'finops')
        populated = domain.create_conversation('admin', 'finops')
        messages.create(populated, 'user', '检查最近七天费用')
        current = domain.create_conversation('admin', 'finops')
        boot = self.client.get('/api/bootstrap').json()
        self.assertEqual({c['id'] for c in boot['conversations'] if c['owner'] == 'finops'}, {populated['id'], current['id']})
        self.assertEqual(len(self.db.all('conversation_title')), 1)

    def test_old_history_backfill_and_title_job_recovery(self):
        c = domain.create_conversation('bob', 'cloudtrail')
        messages.create(c, 'user', '查最新三个 CloudTrail 操作')
        titles.backfill()
        job_id = 'conversation-title-' + c['id']
        titles.backfill()
        self.assertEqual(len(self.db.all('conversation_title')), 1)
        self.db.update(job_id, lambda x: {**x, 'status': 'running', 'lease_until': 0})
        with patch.object(titles, 'summarize', return_value=('近期 CloudTrail 操作', '查看最近三个审计事件。')):
            titles.execute(job_id)
        self.assertEqual(self.db.get(job_id)['status'], 'succeeded')
        self.assertEqual(self.db.get(c['id'])['title'], '近期 CloudTrail 操作')
        alice = TestClient(api.app)
        alice.post('/api/login', json={'username': 'alice', 'password': 'alice-password'})
        denied = alice.post('/api/conversations', json={'owner': 'cloudtrail', 'previous_id': c['id']}, headers=self.headers)
        self.assertEqual(denied.status_code, 403)

    def test_current_conversation_is_saved_per_user_and_agent(self):
        alice = TestClient(api.app)
        bob = TestClient(api.app)
        for client, name in ((alice, 'alice'), (bob, 'bob')):
            client.post('/api/login', json={'username': name, 'password': name + '-password'})
        first = alice.post('/api/conversations', json={'owner': 'finops'}, headers=self.headers).json()
        second = alice.post('/api/conversations', json={'owner': 'finops'}, headers=self.headers).json()
        alice.post('/api/conversations/' + first['id'] + '/activate', json={}, headers=self.headers)
        fresh_browser = TestClient(api.app)
        fresh_browser.post('/api/login', json={'username': 'alice', 'password': 'alice-password'})
        self.assertEqual(fresh_browser.get('/api/bootstrap').json()['active_conversations']['finops'], first['id'])
        self.assertNotIn(first['id'], [c['id'] for c in bob.get('/api/bootstrap').json()['conversations']])
        self.assertEqual(bob.post('/api/conversations/' + second['id'] + '/activate', json={}, headers=self.headers).status_code, 403)
        self.assertEqual(alice.patch('/api/conversations/' + first['id'], json={'title': '费用检查'}, headers=self.headers).status_code, 200)
        self.assertEqual(fresh_browser.get('/api/bootstrap').json()['conversations'][0]['title'], '费用检查')

    def test_calendar_schedule_and_dst(self):
        schedule = schedules.validate({'start_date': '2026-01-31', 'time': '09:00', 'timezone': 'Asia/Shanghai', 'repeat': 'monthly'})
        after = dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc).timestamp()
        expected = dt.datetime(2026, 2, 28, 1, tzinfo=dt.timezone.utc).timestamp()
        self.assertEqual(schedules.next_time(schedule, after), expected)
        weekly = schedules.validate({'start_date': '2026-10-05', 'time': '09:00', 'timezone': 'Asia/Shanghai', 'repeat': 'weekly'})
        self.assertEqual(schedules.next_time(weekly, dt.datetime(2026, 10, 6, tzinfo=dt.timezone.utc).timestamp()), dt.datetime(2026, 10, 12, 1, tzinfo=dt.timezone.utc).timestamp())
        with self.assertRaises(ValueError):
            schedules.validate({'start_date': '2026-03-08', 'time': '02:30', 'timezone': 'America/New_York'})
        daily = schedules.validate({'start_date': '2026-03-07', 'time': '02:30', 'timezone': 'America/New_York', 'repeat': 'daily'})
        self.assertEqual(schedules.next_time(daily, dt.datetime(2026, 3, 8, tzinfo=dt.timezone.utc).timestamp()), dt.datetime(2026, 3, 8, 7, tzinfo=dt.timezone.utc).timestamp())

    def test_custom_schedule_survives_restart_and_run_is_independent(self):
        value = {'name': '自建巡检', 'prompt': '检查告警', 'owner': 'monitoring', 'start_date': '2099-01-01', 'time': '09:00', 'repeat': 'once', 'timezone': 'Asia/Shanghai'}
        response = self.client.post('/api/routines', json=value, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        record = response.json()
        self.assertNotIn('expires_at', record)
        domain.seed()
        self.assertTrue(self.db.get(record['id'])['enabled'])
        now = record['next_at'] + 1
        schedules.tick(now)
        schedules.tick(now)
        tasks = [t for t in self.db.all('task') if t.get('routine_id') == record['id']]
        self.assertEqual(len(tasks), 1)
        self.assertTrue(self.db.get(tasks[0]['conversation_id'])['scheduled'])
        self.assertFalse(self.db.get(record['id'])['enabled'])
        self.assertNotIn(tasks[0]['conversation_id'], [c['id'] for c in self.client.get('/api/bootstrap').json()['conversations']])
        self.assertEqual(len(self.client.get('/api/routines/' + record['id']).json()['runs']), 1)
        bob = TestClient(api.app)
        bob.post('/api/login', json={'username': 'bob', 'password': 'bob-password'})
        self.assertEqual(bob.get('/api/routines/' + record['id']).status_code, 403)
        self.assertEqual(bob.get('/api/routines/' + record['id'] + '/runs/' + tasks[0]['id']).status_code, 403)
        value.update(name='修改后巡检', start_date='2099-02-01', repeat='weekly')
        edited = self.client.patch('/api/routines/' + record['id'], json=value, headers=self.headers)
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(edited.json()['schedule_revision'], 2)
        self.assertEqual(len(self.client.get('/api/routines/' + record['id']).json()['runs']), 1)

    def test_scheduled_result_is_in_s3_and_hydrates(self):
        c = {**self.c, 'id': 'scheduled-test', 'scheduled': True, 'routine_id': 'routine-test', 'expires_at': int(time.time() + 86400)}
        self.db.put(c)
        task = worker.claim(domain.task(c, 'check', owner='finops'))
        fake = Mock()
        objects = {}
        fake.put_object.side_effect = lambda **kw: objects.__setitem__(kw['Key'], kw['Body'])
        fake.get_object.side_effect = lambda **kw: {'Body': io.BytesIO(objects[kw['Key']])}
        with patch.object(messages, 'bucket', return_value='chat-bucket'), patch.object(messages, 's3', return_value=fake):
            worker.finish(task, '完整调查结果' * 1000)
            saved = self.db.get(task['id'])
            self.assertNotIn('result', saved)
            self.assertNotIn('reply_text', saved)
            self.assertLess(len(saved['result_summary']), 181)
            self.assertEqual(worker.results.read(saved), '完整调查结果' * 1000)
            self.assertIn('/routines/routine-test/', saved['result_ref']['key'])
            self.assertEqual(self.db.scoped('event', c['id'])[-1]['expires_at'], c['expires_at'])

    def test_transcribe_language_signing(self):
        from botocore.credentials import Credentials
        from control.transcribe import signed_url
        session = Mock()
        session.get_credentials.return_value = Credentials('test-key', 'test-secret')
        with patch('control.transcribe.boto3.Session', return_value=session):
            for language in ('zh-CN', 'zh-HK', 'en-US'):
                self.assertIn('language-code=' + language, signed_url(language))
            with self.assertRaises(ValueError):
                signed_url('auto')

    def setUp(self):
        self.db = Store()
        self.patches = [patch.object(module, 'store', self.db) for module in (api, domain, worker, executor, execution, terraform, registry, messages, schedules, titles, deploy_history, routine_results)]
        self.patches += [patch('control.ws.store', self.db), patch('control.events.store', self.db)]
        for p in self.patches:
            p.start()
        domain.seed()
        api._login_attempts.clear()
        self.c = self.db.get('cloudops')
        self.client = TestClient(api.app)
        self.client.post('/api/login', json={'password': 'test-password'})
        self.headers = {'X-CloudOps-CSRF': '1'}

    def test_deleted_schedule_is_owned_hidden_and_never_recreated(self):
        r = self.db.get('routine-default-finops')
        task = domain.task(self.c, 'already triggered', 'finops', metadata={'routine_id': r['id']})
        bob = TestClient(api.app)
        bob.post('/api/login', json={'username': 'bob', 'password': 'bob-password'})
        self.assertEqual(bob.delete('/api/routines/' + r['id'], headers=self.headers).status_code, 403)
        self.assertEqual(self.client.delete('/api/routines/' + r['id'], headers=self.headers).status_code, 200)
        self.assertEqual(self.client.delete('/api/routines/' + r['id'], headers=self.headers).status_code, 200)
        self.assertNotIn(r['id'], [x['id'] for x in self.client.get('/api/bootstrap').json()['routines']])
        self.assertEqual(self.client.post('/api/routines/' + r['id'] + '/toggle', json={}, headers=self.headers).status_code, 403)
        registry.seed_routines()
        schedules.tick(time.time() + 10 * 86400)
        self.assertTrue(self.db.get(r['id'])['deleted_at'])
        self.assertFalse(self.db.get(r['id'])['enabled'])
        self.assertEqual(self.db.get(task['id'])['status'], 'queued')
        self.assertEqual(len([t for t in self.db.all('task') if t.get('routine_id') == r['id']]), 1)

    def test_delete_wins_against_stale_schedule_dispatch(self):
        r = self.db.get('routine-default-finops')
        self.db.update(r['id'], lambda x: {**x, 'next_at': time.time() - 1})
        original = domain.task
        def delete_before_commit(*args, **kwargs):
            if kwargs.get('metadata', {}).get('routine_id') == r['id']:
                schedules.delete(r['id'], 'admin')
            return original(*args, **kwargs)
        with patch.object(domain, 'task', side_effect=delete_before_commit):
            schedules.tick()
        self.assertEqual([t for t in self.db.all('task') if t.get('routine_id') == r['id']], [])
        self.assertEqual([o for o in self.db.all('outbox') if r['id'] in o['task_id']], [])

    def test_plain_chat_uses_same_role_without_runtime_or_tools(self):
        from control.routing import conversational, interactive
        for owner in ('supervisor', 'finops', 'monitoring', 'cloudtrail', 'terraform'):
            c = domain.create_conversation('admin', owner)
            t = worker.claim(domain.task(c, '你好，请用一句话介绍你的职责。'))
            self.assertTrue(conversational(t))
            self.assertTrue(interactive(t))
            with patch.object(worker, 'model', return_value='职责说明') as model, patch.object(worker, 'call_expert', side_effect=AssertionError('Runtime must be bypassed')):
                worker.execute(t)
            self.assertEqual(self.db.get(t['id'])['route'], 'direct_conversation')
            self.assertEqual(self.db.get(t['id'])['status'], 'succeeded')
            self.assertIn(self.db.get('agent-' + owner)['config']['prompt'], model.call_args.args[1])
        for prompt in ('你好，请查询今天费用', '什么是我的实例当前告警', '请创建一个 S3 桶', '查询最近两次部署'):
            self.assertFalse(conversational({'prompt': prompt}))
        self.assertFalse(conversational({'prompt': '解释费用', 'file_ids': ['attached']}))
        self.assertFalse(conversational({'prompt': '你好', 'routine_id': 'routine'}))
        for prompt in ('说一下你的职责', '请问什么是 CloudWatch', 'Who are you?', 'Please explain Savings Plans'):
            self.assertTrue(conversational({'prompt': prompt}))
        self.assertFalse(conversational({'prompt': 'Hello, check my current costs'}))

    def test_reclaim_preserves_first_start_time(self):
        t = worker.claim(self.task())
        first = t['started_at']
        self.db.update(t['id'], lambda x: {**x, 'status': 'queued', 'lease_until': 0})
        second = worker.claim(self.db.get(t['id']))
        self.assertEqual(second['started_at'], first)
        self.assertGreaterEqual(second['last_started_at'], first)

    def test_message_cache_keeps_user_keys_separate_and_closes_body(self):
        messages._body.cache_clear()
        bodies = [io.BytesIO(b'{"text":"alice"}'), io.BytesIO(b'{"text":"bob"}')]
        client = Mock()
        client.get_object.side_effect = [{'Body': body} for body in bodies]
        with patch.object(messages, 's3', return_value=client):
            alice = {'text_ref': {'bucket': 'chat', 'key': 'users/alice/message'}}
            bob = {'text_ref': {'bucket': 'chat', 'key': 'users/bob/message'}}
            self.assertEqual(messages.text(alice), 'alice')
            self.assertEqual(messages.text(alice), 'alice')
            self.assertEqual(messages.text(bob), 'bob')
        self.assertEqual(client.get_object.call_count, 2)
        self.assertTrue(all(body.closed for body in bodies))
        messages._body.cache_clear()

    def tearDown(self):
        self.client.close()
        for p in reversed(self.patches):
            p.stop()

    def task(self, name='finops'):
        return domain.task(self.c, 'test evidence', owner=name)

    def test_message_retry_does_not_duplicate_task_or_message(self):
        body = {'text': 'test cost', 'request_id': 'request-123'}
        first = self.client.post('/api/conversations/cloudops/messages', json=body, headers=self.headers)
        second = self.client.post('/api/conversations/cloudops/messages', json=body, headers=self.headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()['id'], second.json()['id'])
        self.assertEqual(len(self.db.all('task')), 1)
        self.assertEqual(len(self.db.all('message')), 1)
        body['text'] = 'different'
        self.assertEqual(self.client.post('/api/conversations/cloudops/messages', json=body, headers=self.headers).status_code, 400)

    def test_only_supervisor_accepts_multiple_registered_mentions(self):
        body = {'text':'compare evidence','owners':['finops','monitoring'],'request_id':'multi-owner-1'}
        response = self.client.post('/api/conversations/cloudops/messages', json=body, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['owner'], 'supervisor')
        self.assertEqual(response.json()['explicit_owners'], ['finops','monitoring'])
        direct = self.client.post('/api/conversations', json={'owner':'finops'}, headers=self.headers).json()
        self.assertEqual(self.client.post('/api/conversations/' + direct['id'] + '/messages', json={**body,'request_id':'multi-owner-2'}, headers=self.headers).status_code, 400)

    def test_mentions_are_present_before_task_becomes_dispatchable_and_survive_history(self):
        published = []
        create = self.db.create_task
        def capture(task, **kwargs):
            published.append(task.copy())
            return create(task, **kwargs)
        body = {'text': '@monitoring 你好，请介绍职责', 'request_id': 'mention-atomic-1'}
        with patch.object(self.db, 'create_task', side_effect=capture):
            response = self.client.post('/api/conversations/cloudops/messages', json=body, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(published[0]['explicit_owners'], ['monitoring'])
        self.assertEqual(published[0]['owner'], 'monitoring')
        self.assertEqual(published[0]['phase'], 'execute')
        from control.routing import conversational
        self.assertFalse(conversational(published[0]))
        history = self.client.get('/api/conversations/cloudops').json()['messages']
        self.assertEqual(history[-1]['explicit_owners'], ['monitoring'])
        self.assertEqual(history[-1]['text'], body['text'])
        # Older messages had no assignment metadata; recover it from the original task.
        self.db.update(history[-1]['id'], lambda x: {k:v for k,v in x.items() if k != 'explicit_owners'})
        self.assertEqual(self.client.get('/api/conversations/cloudops').json()['messages'][-1]['explicit_owners'], ['monitoring'])

    def test_text_mentions_aliases_deduplicate_and_email_is_not_assignment(self):
        body = {'text':'@AgentCore @监控专家 @CloudWatch 查询状态', 'owners':['agentcore'], 'request_id':'mention-alias-1'}
        response = self.client.post('/api/conversations/cloudops/messages', json=body, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['explicit_owners'], ['agentcore', 'monitoring'])
        response = self.client.post('/api/conversations/cloudops/messages', json={'text':'联系 user@finops.example', 'request_id':'mention-email-1'}, headers=self.headers)
        self.assertEqual(response.json()['explicit_owners'], [])

    def test_changed_assignment_cannot_reuse_message_id(self):
        body = {'text':'查询状态', 'owners':['monitoring'], 'request_id':'mention-idempotent-1'}
        self.assertEqual(self.client.post('/api/conversations/cloudops/messages', json=body, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post('/api/conversations/cloudops/messages', json={**body,'owners':['agentcore']}, headers=self.headers).status_code, 400)

    def test_mentions_reject_unknown_and_non_member_experts(self):
        self.assertEqual(self.client.post('/api/conversations/cloudops/messages', json={'text':'@unknown 查询', 'request_id':'mention-unknown-1'}, headers=self.headers).status_code, 400)
        self.db.update('cloudops', lambda x: {**x, 'members':['supervisor','monitoring']})
        response = self.client.post('/api/conversations/cloudops/messages', json={'text':'@agentcore 查询', 'request_id':'mention-member-1'}, headers=self.headers)
        self.assertEqual(response.status_code, 403)

    def test_stale_worker_cannot_commit_after_lease_reclaimed(self):
        t = self.task()
        first = worker.claim(t)
        self.db.update(t['id'], lambda x: {**x, 'lease_until': 0, 'status': 'queued'})
        second = worker.claim(self.db.get(t['id']))
        with self.assertRaises(Conflict):
            worker.finish(first, 'stale result')
        worker.finish(second, 'fresh evidence')
        self.assertEqual(self.db.get(t['id'])['result'], 'fresh evidence')

    def test_streamed_reply_is_durable_and_lease_fenced(self):
        t = worker.claim(self.task('supervisor'))
        class FakeAgent:
            def __init__(self, **kw): self.callback = kw['callback_handler']
            def __call__(self, prompt):
                self.callback(data='你好，')
                self.callback(data='已收到。')
                return '你好，已收到。'
        with patch('strands.Agent', FakeAgent), patch.dict('sys.modules', {'framework.model': SimpleNamespace(build_model=lambda *a, **kw: Mock())}):
            self.assertEqual(worker.model('hello', task=t), '你好，已收到。')
            self.assertEqual(self.db.get(t['id'])['reply_text'], '你好，已收到。')
            events = self.db.events_after('cloudops', 0)
            self.assertEqual(''.join(e.get('delta', '') for e in events), '你好，已收到。')
            self.db.update(t['id'], lambda x: {**x, 'lease_until': 0})
            with self.assertRaises(Conflict): worker.model('hello', task=t)

    def test_completion_event_follows_saved_answer(self):
        t = worker.claim(self.task('supervisor'))
        original = self.db.event
        def publish(task, text, *args, **kw):
            answer = self.db.get('answer-' + task['id'])
            self.assertIsNotNone(answer, 'completion published before the chat answer')
            self.assertEqual(messages.text(answer), '已核对最近两次部署。')
            self.assertEqual(self.db.get(task['id'])['status'], 'succeeded')
            return original(task, text, *args, **kw)
        with patch.object(self.db, 'event', side_effect=publish):
            worker.finish(t, '已核对最近两次部署。')

    def test_slow_progress_storage_does_not_block_model_stream(self):
        import threading
        t = worker.claim(self.task('supervisor'))
        entered, release = threading.Event(), threading.Event()
        original = self.db.event
        def delayed(*args, **kwargs):
            if kwargs.get('event_type') == 'reply.delta':
                entered.set()
                self.assertTrue(release.wait(2))
            return original(*args, **kwargs)
        case = self
        class FakeAgent:
            def __init__(self, **kw): self.callback = kw['callback_handler']
            def __call__(self, prompt):
                self.callback(data='第一段')
                case.assertTrue(entered.wait(1))
                begun = time.monotonic()
                self.callback(data='第二段')
                case.assertLess(time.monotonic()-begun, 0.1)
                release.set()
                return '第一段第二段'
        with patch('strands.Agent', FakeAgent), patch.dict('sys.modules', {'framework.model': SimpleNamespace(build_model=lambda *a, **kw: Mock())}), patch.object(self.db, 'event', side_effect=delayed):
            self.assertEqual(worker.model('hello', task=t), '第一段第二段')
        self.assertEqual(self.db.get(t['id'])['reply_text'], '第一段第二段')
        self.assertIn('stream_persistence_seconds', self.db.get(t['id']))

    def test_transactions_reuse_bounded_low_level_client(self):
        client = Mock()
        with patch.dict(os.environ, {'CLOUDOPS_TABLE':'mock-table'}), patch('control.store.boto3.resource'), patch('control.store.boto3.client', return_value=client) as factory:
            db = Store()
            row = {'id':'record-one','kind':'task','version':1,'created_at':time.time()}
            db.atomic([(row,None)])
            db.atomic([(row,None)])
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(factory.call_args.kwargs['config'].read_timeout, 5)
        self.assertTrue(factory.call_args.kwargs['config'].tcp_keepalive)
        self.assertEqual(client.transact_write_items.call_count, 2)
        encoded = client.transact_write_items.call_args.kwargs['TransactItems'][0]['Put']['Item']
        self.assertEqual(encoded['id'], {'S':'record-one'})

    def test_deploy_history_bypasses_models_and_experts_and_is_user_scoped(self):
        self.db.put({'id':'tf-own','kind':'terraform','user_id':'admin','name':'我的部署','status':'succeeded','apply_started_at':time.time()})
        self.db.put({'id':'tf-other','kind':'terraform','user_id':'bob','name':'其他用户部署','status':'succeeded','apply_started_at':time.time()})
        for owner in ('supervisor','terraform'):
            t=worker.claim(domain.task(self.c,'帮我看看最近两次terraform的部署',owner=owner))
            with patch.object(worker,'call_expert') as expert, patch.object(worker,'model') as model:
                worker.execute(t)
                expert.assert_not_called();model.assert_not_called()
            saved=self.db.get(t['id'])
            self.assertEqual(saved['status'],'succeeded')
            self.assertEqual(saved['route'],'platform_terraform_history')
            self.assertIn('我的部署',saved['result']);self.assertNotIn('其他用户部署',saved['result'])
        self.db.update('tf-own',lambda x:{k:v for k,v in {**x,'status':'planned'}.items() if k!='apply_started_at'})
        answer=deploy_history.read(t)
        self.assertIn('没有实际部署执行记录',answer);self.assertIn('尚未部署',answer);self.assertNotIn('部署成功',answer.split('| 项目')[1])
        for prompt in ('请部署 terraform 的新脚本','生成最近使用的terraform配置','用CloudTrail审计最近两次terraform部署','删除最近的Terraform部署'):
            self.assertFalse(deploy_history.requested(prompt,'supervisor'))

    def test_readonly_history_api_is_owned_and_separates_expired_approvals(self):
        now = time.time()
        config = {'resource': {}}
        p = self.db.put({'id':'tf-history','kind':'terraform','user_id':'admin','status':'planned',
                         'configuration':config,'plan_sha256':'abc','plan_expires_at':now+1000})
        action = {'type':'terraform_apply','project_id':p['id'],'plan_sha256':'abc','config_fingerprint':fingerprint(config)}
        for ident, expiry in [('valid',now+1000),('expired',now-1)]:
            self.db.put({'id':ident,'kind':'approval','user_id':'admin','status':'pending',
                         'action':action,'fingerprint':fingerprint(action),'expires_at':expiry})
        self.db.put({'id':'other','kind':'terraform','user_id':'bob','status':'succeeded','apply_started_at':now})
        client = TestClient(api.app)
        client.post('/api/login',json={'username':'admin','password':'test-password'})
        response = client.get('/api/terraform/history',params={'query':'今天','limit':1})
        self.assertEqual(response.status_code,200)
        evidence = response.json()
        self.assertEqual(evidence['counts'],{'deployments':0,'plans':1,'approvals':2,'pending_actionable_approvals':1})
        self.assertTrue(evidence['has_more'])
        self.assertNotIn('configuration',str(evidence)); self.assertNotIn('other',str(evidence))
        self.assertEqual(self.db.get('valid')['status'],'pending')
        self.assertTrue(deploy_history.requested('本平台今日 Terraform 部署和审批记录','supervisor'))
        self.assertFalse(deploy_history.requested('批准执行今天的Terraform部署','supervisor'))

    def test_history_beijing_date_boundary_and_explicit_date(self):
        midnight = dt.datetime(2026,10,4,tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp()
        for ident, ts in [('before',midnight-1),('inside',midnight),('after',midnight+86400)]:
            self.db.put({'id':ident,'kind':'terraform','user_id':'admin','status':'succeeded','apply_started_at':ts})
        evidence = deploy_history.query('admin','2026-10-04')
        self.assertEqual([p['id'] for p in evidence['deployments']],['inside'])
        self.assertEqual(evidence['counts']['deployments'],1)

    def test_expert_context_hydrates_only_latest_eight_messages(self):
        for n in range(20):messages.create(self.c,'user',f'背景 {n}')
        t=worker.claim(self.task())
        self.db.update('agent-finops',lambda x:{**x,'runtime_arn':'test-arn'})
        original=messages.public
        with patch.object(messages,'public',wraps=original) as hydrate, patch.object(worker.boto3,'client'), patch.object(execution,'get',return_value={'status':'completed','result':'结果'}):
            self.assertEqual(worker.call_expert(t,self.c,''),'结果')
            self.assertEqual(hydrate.call_count,8)
            self.assertEqual(messages.text(hydrate.call_args_list[0].args[0]),'背景 12')

    def test_legacy_successful_deploy_without_start_or_approval_is_not_a_plan(self):
        self.db.put({'id':'tf-legacy','kind':'terraform','user_id':'admin','name':'旧成功部署','status':'succeeded','finished_at':time.time()})
        answer=deploy_history.read({'user_id':'admin','prompt':'最近两次terraform部署'})
        self.assertIn('部署成功',answer);self.assertIn('历史确认记录缺失',answer)
        self.assertNotIn('没有实际部署执行记录',answer)

    def test_checkpoint_s3_region_is_explicit_even_with_us_default(self):
        t=worker.claim(self.task());run,_=execution.claim(t)
        fake=Mock();fake.put_object.return_value={'VersionId':'v1'}
        fake.get_object.return_value={'Body':io.BytesIO(b'[]')}
        with patch.dict(os.environ,{'CLOUDOPS_BUCKET':'test-bucket','AWS_REGION':'cn-northwest-1','AWS_DEFAULT_REGION':'us-east-1'}), patch.object(execution.boto3,'client',return_value=fake) as client:
            execution.checkpoint(run,[]);self.assertEqual(execution.history(execution.get(t['id'])),[])
            for call in client.call_args_list:self.assertEqual(call.kwargs['region_name'],'cn-northwest-1')

    def test_expert_records_model_and_tool_times(self):
        with patch.dict('sys.modules', {'framework.model': SimpleNamespace(build_model=Mock())}):
            from agents.leaf.a2a_main import Progress
        t=worker.claim(self.task());run,_=execution.claim(t);progress=Progress(run)
        progress.before_model(SimpleNamespace());progress.after_model(SimpleNamespace())
        tool={'name':'cloudtrail-mcp___lookup_events','toolUseId':'use-1','input':{}}
        progress.before_tool(SimpleNamespace(tool_use=tool))
        progress.after_tool(SimpleNamespace(tool_use=tool,result={'toolUseId':'use-1','status':'success','content':[]}))
        saved=execution.get(t['id'])
        self.assertEqual(saved['tool_calls'],1);self.assertGreaterEqual(saved['model_seconds'],0);self.assertGreaterEqual(saved['tool_seconds'],0)
        self.assertEqual(tool['input']['region'],'cn-northwest-1')

    def test_same_socket_sync_recovers_late_answer_without_new_event(self):
        t = worker.claim(self.task('supervisor'))
        messages.create(self.c, 'user', '核对部署记录')
        worker.finish(t, '部署记录已核对。')
        complete = messages.list_for('cloudops')
        # Model an index read that misses the answer when the final event arrives.
        reads = 0
        def lagged_messages(*args, **kw):
            nonlocal reads
            reads += 1
            return complete[:1] if reads == 1 else complete
        with patch.object(self.db, 'events_after', return_value=[]), patch.object(messages, 'list_for', side_effect=lagged_messages):
            with self.client.websocket_connect('/api/ws/cloudops', headers={'origin': 'http://testserver'}) as ws:
                self.assertEqual(ws.receive_json()['type'], 'session.ready')
                self.assertEqual(len(ws.receive_json()['messages']), 1)
                ws.send_json({'type': 'sync'})
                recovered = ws.receive_json()
                self.assertEqual(recovered['type'], 'snapshot')
                self.assertEqual(recovered['messages'][-1]['text'], '部署记录已核对。')
                self.assertEqual(recovered['tasks'][0]['status'], 'succeeded')
        self.assertEqual(len(self.db.all('task')), 1)

    def test_pause_does_not_lose_checkpoint(self):
        t = worker.claim(self.task())
        domain.transition(t['id'], 'paused')
        worker.finish(t, 'evidence preserved')
        self.assertEqual(self.db.get(t['id'])['status'], 'paused')
        resumed = worker.claim(domain.transition(t['id'], 'queued'))
        worker.finish(resumed, resumed['checkpoint_result'])
        self.assertEqual(self.db.get(t['id'])['status'], 'succeeded')

    def test_active_index_tracks_only_unfinished_work_and_records_expire(self):
        from control.store import Store
        t = self.task()
        self.assertIn(t['id'], [x['id'] for x in self.db.active('task')])
        self.assertIn('out-' + t['id'], [x['id'] for x in self.db.active('outbox')])
        self.assertEqual(Store.item(self.db.get(t['id']))['active'], 'task')
        self.db.update(t['id'], lambda x: {**x, 'status': 'succeeded'})
        self.assertNotIn(t['id'], [x['id'] for x in self.db.active('task')])
        self.assertNotIn('active', Store.item(self.db.get(t['id'])))
        self.db.update('out-' + t['id'], lambda x: {**x, 'sent': True})
        self.assertFalse(self.db.active('outbox'))
        e = self.db.event(t, 'progress')
        self.assertGreater(e['expires_at'], time.time() + 29 * 86400)
        self.assertLess(e['expires_at'], time.time() + 31 * 86400)
        self.assertLess(self.db.get('out-' + t['id'])['expires_at'], time.time() + 8 * 86400)

    def test_concurrent_event_order_and_replay(self):
        t = self.task()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda n: self.db.event(t, str(n)), range(24)))
        events = self.db.events_after('cloudops', 0)
        self.assertEqual([e['sequence'] for e in events], list(range(1, 25)))
        self.assertEqual(len(self.db.events_after('cloudops', 20)), 4)

    def test_task_and_outbox_are_atomic(self):
        self.task()
        t = self.db.all('task')[0]
        with self.assertRaises(Conflict):
            self.db.create_task(t)
        self.assertEqual(len(self.db.all('task')), 1)
        self.assertEqual(len(self.db.all('outbox')), 1)

    def test_customer_file_isolation(self):
        self.db.put({'id': 'file-other', 'kind': 'file', 'customer_id': 'other', 'status': 'ready'})
        c = {**self.c, 'file_ids': ['file-other']}
        with self.assertRaises(PermissionError):
            domain.permitted_files(c, ['file-other'])
        self.assertEqual(self.client.post('/api/conversations/cloudops/files/file-other/share', json={}, headers=self.headers).status_code, 404)

    def test_approval_cannot_be_model_boolean_or_reused(self):
        action = {'type': 'cfn_preview', 'stack_name': 'cloudops-test-demo', 'template_body': json.dumps({'Resources': {'Bucket': {'Type': 'AWS::S3::Bucket', 'Properties': {'BucketName': 'cloudops-test-demo'}}}})}
        a = self.db.put({'id': 'approval-test', 'kind': 'approval', 'status': 'pending', 'action': action, 'fingerprint': fingerprint(action), 'expires_at': time.time() + 60})
        with self.assertRaises(PermissionError):
            executor.execute_approval(a['id'])
        self.db.update(a['id'], lambda x: {**x, 'status': 'approved', 'user': 'admin', 'expires_at': 0})
        with self.assertRaises(PermissionError):
            executor.execute_approval(a['id'])
        self.db.update(a['id'], lambda x: {**x, 'expires_at': time.time() + 60, 'action': {**action, 'stack_name': 'cloudops-test-changed'}})
        with self.assertRaises(PermissionError):
            executor.execute_approval(a['id'])
        with self.assertRaises(ValueError):
            executor.validate_action({'type': 'delete_stack', 'approved': True})

    def test_registry_drives_mentions_and_independent_chat(self):
        registry.register('security', {'type':'worker', 'description':'Security investigator', 'prompt':'Use evidence', 'tools':['cloudtrail-mcp']})
        boot = self.client.get('/api/bootstrap').json()
        self.assertIn('security', [a['name'] for a in boot['agents']])
        self.assertIn('direct-security', [c['id'] for c in boot['conversations']])
        self.assertFalse(any(c.get('group') for c in boot['conversations']))
        self.assertNotIn('cdk', self.db.get('cloudops')['members'])
        self.assertNotIn('harness', [a['name'] for a in boot['agents']])
        self.assertEqual(self.client.post('/api/conversations', json={}, headers=self.headers).status_code, 200)
        t = domain.task(self.db.get('cloudops'), 'investigate', owner='security')
        self.assertEqual(t['owner'], 'security')
        with self.assertRaises(PermissionError):
            domain.task(self.db.get('cloudops'), 'do old cdk task', owner='cdk')

    def test_routines_are_distinct_and_toggle_preserves_history(self):
        routines = self.db.all('routine')
        self.assertEqual({r['owner'] for r in routines}, {'finops','monitoring','cloudtrail'})
        self.assertEqual(len({r['prompt'] for r in routines}), 3)
        r = routines[0]
        self.db.update(r['id'], lambda x: {**x, 'last_task':'historic', 'last_at':123})
        value = self.client.post('/api/routines/' + r['id'] + '/toggle', json={}, headers=self.headers).json()
        self.assertFalse(value['enabled'])
        self.assertEqual(value['last_task'], 'historic')
        value = self.client.post('/api/routines/' + r['id'] + '/toggle', json={}, headers=self.headers).json()
        self.assertTrue(value['enabled'])
        self.assertGreater(value['next_at'], time.time())

    def test_arbitrary_users_are_isolated_across_conversations_and_tasks(self):
        alice = TestClient(api.app); bob = TestClient(api.app)
        try:
            self.assertEqual(alice.post('/api/login', json={'username':'alice','password':'alice-password'}).status_code, 200)
            self.assertEqual(bob.post('/api/login', json={'username':'bob','password':'bob-password'}).status_code, 200)
            ac = alice.post('/api/conversations', json={'owner':'supervisor'}, headers=self.headers).json()
            bc = bob.post('/api/conversations', json={'owner':'finops'}, headers=self.headers).json()
            self.assertNotEqual(ac['id'], bc['id'])
            self.assertEqual(ac['user_id'], 'alice'); self.assertEqual(bc['user_id'], 'bob')
            self.assertEqual(bob.get('/api/conversations/' + ac['id']).status_code, 403)
            alice.post('/api/conversations/' + ac['id'] + '/messages', json={'text':'alice only','request_id':'alice-request'}, headers=self.headers)
            self.assertFalse(any(c['id'] == ac['id'] for c in bob.get('/api/bootstrap').json()['conversations']))
            task = self.db.get('task-' + ac['id'] + '-alice-request')
            self.assertEqual(task['user_id'], 'alice')
        finally:
            alice.close(); bob.close()

    def test_expert_execution_deduplicates_and_fences_after_recovery(self):
        t = worker.claim(self.task())
        first, start = execution.claim(t)
        self.assertTrue(start)
        again, start = execution.claim(t)
        self.assertFalse(start)
        self.assertEqual(first['token'], again['token'])
        self.db.update(first['id'], lambda x: {**x, 'lease_until':0, 'checkpoint':{'key':'saved-step'}})
        second, start = execution.claim(t)
        self.assertTrue(start)
        self.assertEqual(second['checkpoint']['key'], 'saved-step')
        with self.assertRaises(Conflict):
            execution.owned(first, lambda x: {**x, 'result':'stale'})

    def test_saved_expert_result_does_not_reinvoke_model(self):
        t = worker.claim(self.task())
        a = self.db.get('agent-finops')
        self.db.update(a['id'], lambda x: {**x, 'runtime_arn':'arn:aws-cn:bedrock-agentcore:cn-northwest-1:123:runtime/test'})
        r, _ = execution.claim(t)
        execution.owned(r, lambda x: {**x, 'status':'completed', 'result':'saved answer', 'lease_until':0})
        with patch('control.worker.boto3.client') as client:
            self.assertEqual(worker.call_expert(t,self.c,''), 'saved answer')
            client.return_value.invoke_agent_runtime.assert_not_called()

    def test_terraform_rejects_local_execution_and_changed_plan(self):
        for cfg in ({'provider':{}}, {'resource':{'null_resource':{}}}, {'resource':{'aws_instance':{'x':{'provisioner':[]}}}}, {'output':{'x':{'value':'${file("/etc/passwd")}'}}}):
            with self.assertRaises(ValueError):
                terraform.validate_configuration(cfg)
        config = {'resource':{'aws_s3_bucket':{'example':{'bucket':'cloudops-test-example'}}}}
        t = self.task('terraform')
        with patch.dict(os.environ, CLOUDOPS_ACCOUNT='123', CLOUDOPS_BUCKET='test'):
            p = terraform.prepare(t,'test',config)
            p = self.db.update(p['id'], lambda x: {**x,'status':'planned','plan_sha256':'sha','plan_expires_at':time.time()+60})
            a = terraform.approval(p['id'])
            self.assertEqual(a['status'],'pending')
            terraform.validate_action(a['action'])
            self.db.update(p['id'],lambda x:{**x,'configuration':{}})
            with self.assertRaises(ValueError):
                terraform.validate_action(a['action'])

    def test_execution_budget_yields_without_discarding_checkpoint(self):
        t = worker.claim(self.task())
        r, _ = execution.claim(t)
        self.db.update(r['id'],lambda x:{**x,'model_calls':20,'checkpoint':{'key':'last-step'}})
        with self.assertRaises(execution.YieldExecution):
            execution.guard(r,model=True)
        self.assertEqual(execution.get(t['id'])['checkpoint']['key'],'last-step')

    def test_later_chat_request_does_not_replace_current_task(self):
        from agents.supervisor import coordinator
        t = worker.claim(self.task('supervisor'))
        t = self.db.update(t['id'], lambda x: {**x,'phase':'spawn','plan':[]})
        self.db.put({'id':'later-request','kind':'message','conversation_id':'cloudops','role':'user','text':'DO DIFFERENT FUTURE TASK','created_at':t['created_at']+1})
        with patch.object(coordinator,'store',self.db), patch('control.worker.model',return_value='current answer') as model:
            coordinator.execute_stage(t,self.c,'')
        prompt=model.call_args.args[0]
        self.assertNotIn('DO DIFFERENT FUTURE TASK',prompt)
        self.assertTrue(prompt.endswith(t['prompt']))

    def test_exact_approved_terraform_plan_is_executed_once_offline(self):
        raw = b'offline-test-plan'
        config = {'resource':{'aws_s3_bucket':{'test':{'bucket':'cloudops-test-offline'}}}}
        with patch.dict(os.environ,CLOUDOPS_ACCOUNT='123',CLOUDOPS_BUCKET='test'):
            p = terraform.prepare(self.task('terraform'),'offline',config)
            p = self.db.update(p['id'],lambda x:{**x,'status':'planned','plan_sha256':hashlib.sha256(raw).hexdigest(),
                'plan_expires_at':time.time()+60,'plan_binary':{'key':'plan'},'provider_lock':{'key':'lock'}})
            a = terraform.approval(p['id'])
            self.db.update(a['id'],lambda x:{**x,'status':'approved','user':'admin'})
            def download(ref):
                return raw if ref['key']=='plan' else b'provider-lock'
            with patch('control.terraform.environment',return_value={}),patch('control.terraform.download',side_effect=download),patch('control.terraform.command',return_value=SimpleNamespace(stdout='{"values":{"root_module":{"resources":[{"address":"aws_s3_bucket.test"}]}}}')) as command:
                terraform.apply(a['id'])
                with self.assertRaises(PermissionError):
                    terraform.apply(a['id'])
            calls=[c.args[0] for c in command.call_args_list if c.args[0][0]=='apply']
            self.assertEqual(calls,[['apply','-no-color','-input=false','approved.tfplan']])
            self.assertEqual(self.db.get(p['id'])['status'],'succeeded')

    def test_non_admin_owner_can_confirm_and_execute_own_plan(self):
        client = TestClient(api.app)
        try:
            client.post('/api/login', json={'username':'alice','password':'alice-password'})
            conversation = client.post('/api/conversations', json={'owner':'terraform'}, headers=self.headers).json()
            task = domain.task(conversation, 'offline owner check', owner='terraform')
            raw = b'alice-plan'
            config = {'resource':{'aws_s3_bucket':{'test':{'bucket':'cloudops-test-alice'}}}}
            with patch.dict(os.environ, CLOUDOPS_ACCOUNT='123', CLOUDOPS_BUCKET='test'):
                project = terraform.prepare(task, 'alice test', config)
                project = self.db.update(project['id'], lambda x: {**x,'status':'planned','plan_sha256':hashlib.sha256(raw).hexdigest(),
                    'plan_expires_at':time.time()+60,'plan_binary':{'key':'plan'},'provider_lock':{'key':'lock'}})
                approval = terraform.approval(project['id'])
                response = client.post('/api/approvals/' + approval['id'] + '/decision', json={'approve':True}, headers=self.headers)
                self.assertEqual(response.status_code, 200)
                with patch('control.terraform.environment',return_value={}), patch('control.terraform.download',side_effect=lambda ref: raw if ref['key']=='plan' else b'lock'), patch('control.terraform.command',return_value=SimpleNamespace(stdout='{"values":{"root_module":{"resources":[]}}}')):
                    terraform.apply(approval['id'])
                self.assertEqual(self.db.get(project['id'])['status'], 'succeeded')
                self.assertEqual(self.db.get(approval['id'])['user'], 'alice')
        finally:
            client.close()

    def test_bootstrap_limits_recent_history_per_agent(self):
        for owner in ('finops', 'monitoring'):
            for _ in range(12):
                c = domain.create_conversation('alice', owner)
                messages.create(c, 'user', '检查费用或告警')
        client = TestClient(api.app)
        try:
            client.post('/api/login', json={'username': 'alice', 'password': 'alice-password'})
            response = client.get('/api/bootstrap')
            self.assertEqual(response.status_code, 200)
            conversations = response.json()['conversations']
            self.assertEqual(sum(c['owner'] == 'finops' for c in conversations), 11)
            self.assertEqual(sum(c['owner'] == 'monitoring' for c in conversations), 11)
            self.assertTrue(all(c['user_id'] == 'alice' for c in conversations))
        finally:
            client.close()

    def test_ws_auth_send_and_reconnect(self):
        with self.client.websocket_connect('/api/ws/cloudops', headers={'origin': 'http://testserver'}) as ws:
            self.assertEqual(ws.receive_json()['type'], 'session.ready')
            self.assertEqual(ws.receive_json()['type'], 'snapshot')
            ws.send_json({'type': 'message.submit', 'message_id': 'ws-message-123', 'text': 'cost evidence'})
            while True:
                value = ws.receive_json()
                if value['type'] == 'message.ack':
                    break
        with self.client.websocket_connect('/api/ws/cloudops', headers={'origin': 'http://testserver'}) as ws:
            ws.receive_json()
            snap = ws.receive_json()
            self.assertEqual(len(snap['messages']), 1)
            ws.send_json({'type': 'message.submit', 'message_id': 'ws-message-123', 'text': 'cost evidence'})
            while ws.receive_json()['type'] != 'message.ack':
                pass
        self.assertEqual(len(self.db.all('task')), 1)
        with self.assertRaises(Exception):
            with self.client.websocket_connect('/api/ws/cloudops', headers={'origin': 'http://evil.example'}):
                pass

    def test_valid_cloud_session_survives_heartbeat_checks(self):
        from control.security import session_token, valid_session, session_user
        token = session_token('alice')
        self.assertTrue(valid_session(token))
        self.assertEqual(session_user(token), 'alice')

    def test_eventstream_crc_and_corruption(self):
        packet = frame(json.dumps({'Transcript': {'Results': []}}).encode())
        self.assertEqual(unframe(packet), {'Transcript': {'Results': []}})
        corrupt = bytearray(packet); corrupt[-5] ^= 1
        with self.assertRaises(ValueError):
            unframe(bytes(corrupt))

    def test_malformed_hierarchy_is_reported(self):
        for value in (None, [], {'agents': {'bad': 2}}, {'agents': {'bad': {'type': 'worker', 'description': 'x', 'children': [[], 4], 'memory': []}}}):
            self.assertFalse(validate_hierarchy(value)['valid'])

    def test_alarm_dedup_and_recovery(self):
        from control.events import ingest
        event = {'id': 'alarm-1', 'source': 'aws.cloudwatch', 'account': '123', 'time': '2026-10-03T01:00:00Z',
                 'detail': {'alarmName': 'test', 'state': {'value': 'ALARM'}}}
        with patch.dict(os.environ, {'CLOUDOPS_ACCOUNT': '123'}):
            ingest(event); ingest(event)
            self.assertEqual(len(self.db.all('task')), 1)
            ingest({**event, 'id': 'alarm-2', 'time': '2026-10-03T01:01:00Z', 'detail': {'alarmName': 'test', 'state': {'value': 'OK'}}})
        self.assertEqual(len(self.db.all('task')), 1)
        self.assertEqual(self.db.all('issue')[0]['state'], 'OK')


class ToolTests(unittest.TestCase):
    def test_cost_pagination_refund_and_exact_total(self):
        def group(service, value):
            return {'Keys': [service], 'Metrics': {'UnblendedCost': {'Amount': value, 'Unit': 'CNY'}}}
        period = {'TimePeriod': {'Start': '2026-09-01', 'End': '2026-10-01'}, 'Estimated': False}
        mock = Mock()
        mock.get_cost_and_usage.side_effect = [
            {'ResultsByTime': [{**period, 'Groups': [group('EC2', '0.005')]}], 'NextPageToken': 'next'},
            {'ResultsByTime': [{**period, 'Groups': [group('Refund', '-0.002')]}]}]
        with patch('boto3.client', return_value=mock):
            from tools.cost_explorer import handler
        with patch.object(handler, '_ce', mock):
            r = handler.get_cost_by_service({'start': '2026-09-01', 'end': '2026-10-01'})
        self.assertEqual(r['results'][0]['total_exact'], '0.003')
        self.assertEqual(len(r['results'][0]['by_service']), 2)
        self.assertEqual(r['summary']['totals'][0]['amount_exact'], '0.003')
        self.assertEqual(mock.get_cost_and_usage.call_args.kwargs['NextPageToken'], 'next')

if __name__ == '__main__':
    unittest.main()
