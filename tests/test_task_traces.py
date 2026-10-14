import os
os.environ['AWS_EC2_METADATA_DISABLED']='true'
import json
import time
import unittest
from unittest.mock import Mock, patch
from control.store import Store
from control import task_traces


class TaskTraceTests(unittest.TestCase):
    def setUp(self):
        self.db=Store()
    def add(self, i, **fields):
        return self.db.put({'id':str(i),'kind':'task','owner':'supervisor','user_id':'alice','created_at':time.time()+i,
            'status':'succeeded','prompt':'request','attempts':1,**fields})
    def test_pagination_isolates_users_children_and_filters(self):
        for i in range(46):self.add(i)
        self.add(50,user_id='bob')
        self.add(51,parent_id='0')
        first=self.db.task_page('alice',0)
        second=self.db.task_page('alice',0,first['cursor'])
        third=self.db.task_page('alice',0,second['cursor'])
        self.assertEqual([len(x['items']) for x in (first,second,third)],[20,20,6])
        self.assertIsNone(third['cursor'])
        self.assertEqual(len({r['id'] for p in (first,second,third) for r in p['items']}),46)
        with self.assertRaises(ValueError):self.db.task_page('bob',0,first['cursor'])
        self.assertEqual(self.db.task_page('alice',0,status='failed')['items'],[])
        self.add(52,plan=[{'agent':'monitoring'}])
        self.assertEqual(self.db.task_page('alice',0,expert='monitoring')['items'][0]['id'],'52')
    def test_cloud_list_queries_only_summary_projection_with_bounded_reads(self):
        db=Store();db.table_name='test';db.db=Mock()
        db.db.query.return_value={'Items':[{'summary':json.dumps(Store.task_summary(self.add(0)))}]}
        with patch.object(db,'all',side_effect=AssertionError('No scans')):
            self.assertEqual(len(db.task_page('alice',0)['items']),1)
        args=db.db.query.call_args.kwargs
        self.assertEqual(args['IndexName'],'task-timeline-index')
        self.assertEqual(args['ProjectionExpression'],'summary')
        self.assertFalse(args['ScanIndexForward'])
    def test_sparse_index_contains_no_tool_results_and_no_child(self):
        root=Store.item(self.add(0,tool_results={'secret':'must not index'},result='big'))
        self.assertIn('timeline_scope',root)
        self.assertNotIn('secret',root['summary'])
        self.assertNotIn('timeline_scope',Store.item(self.add(1,parent_id='0')))
    def test_trace_reader_drops_nonmetadata_spans_and_sensitive_attributes(self):
        t=self.add(0,trace_id='a'*32)
        span={'traceId':'a'*32,'spanId':'1','name':'MCP lookup','startTimeUnixNano':'1000000000','endTimeUnixNano':'2000000000',
              'attributes':{'cloudops.task.id':'0','cloudops.owner':'monitoring','tool.arguments':'secret'},'status':{'code':'OK'}}
        client=Mock();client.filter_log_events.return_value={'events':[{'message':json.dumps(span)},
            {'message':json.dumps({**span,'spanId':'2','attributes':{'prompt':'secret'}})}]}
        task_traces._cache.clear()
        with patch.object(task_traces.boto3,'client',return_value=client):
            rows,note=task_traces.spans(t)
        self.assertEqual(len(rows),1)
        self.assertNotIn('secret',json.dumps(rows))
        self.assertEqual(rows[0]['end']-rows[0]['start'],1)
    def test_old_tasks_show_lifecycle_without_fabricated_sdk_spans(self):
        task=self.add(0,started_at=time.time(),finished_at=time.time()+2)
        detail=task_traces.detail(task,self.db)
        self.assertEqual(detail['spans'],[])
        self.assertEqual(len(detail['lifecycle']),2)
        self.assertIn('旧任务',detail['note'])

    def test_supervisor_metrics_use_websocket_operation_and_connection_duration(self):
        from control import api
        agents=[{'name':'supervisor','runtime_arn':'supervisor-arn'},{'name':'monitoring','runtime_arn':'expert-arn'}]
        catalog=[]
        for arn,op in [('supervisor-arn','InvokeAgentRuntimeWithWebSocketStream'),('expert-arn','InvokeAgentRuntime')]:
            for metric in ('Invocations','Latency','ConnectionDuration','Duration'):
                catalog.append({'MetricName':metric,'Dimensions':[{'Name':'Resource','Value':arn},{'Name':'Operation','Value':op}]})
        with patch.object(api.registry,'active',return_value=agents),patch.object(api,'_obs_catalog',return_value=catalog), \
             patch.object(api.boto3,'client'),patch.object(api,'_obs_series',return_value=[{'at':'now','value':3}]) as series:
            result=api.observability(None,24)
        self.assertEqual(result['agents'][0]['metric_operation'],'InvokeAgentRuntimeWithWebSocketStream')
        self.assertEqual(result['agents'][0]['metrics']['Invocations'][0]['value'],3)
        self.assertEqual(result['agents'][1]['metric_operation'],'InvokeAgentRuntime')
        self.assertTrue(any(c.args[1]=='ConnectionDuration' for c in series.call_args_list))

    def test_sdk_spans_keep_trace_parent_and_error_type_without_exception_message(self):
        from framework import telemetry
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        exporter=InMemorySpanExporter();provider=TracerProvider();processor=SimpleSpanProcessor(exporter)
        provider.add_span_processor(processor)
        ident,root=telemetry.identity()
        task=self.add(0,trace_id=ident,root_span_id=root,finished_at=time.time()+1)
        with patch.object(telemetry,'_provider',provider),patch.object(telemetry,'_processor',processor):
            with self.assertRaises(RuntimeError):
                with telemetry.span(task,'模型调用'):
                    raise RuntimeError('secret token must never be exported')
            telemetry.complete(task)
        spans=exporter.get_finished_spans()
        self.assertEqual(len(spans),2)
        self.assertEqual(spans[0].context.trace_id,int(ident,16))
        self.assertEqual(spans[0].parent.span_id,int(root,16))
        self.assertEqual(spans[0].attributes['error.type'],'RuntimeError')
        self.assertEqual(spans[0].events,())
        self.assertNotIn('secret',str(spans[0].attributes))
        self.assertEqual(spans[1].context.span_id,int(root,16))
        provider.shutdown()


if __name__=='__main__':unittest.main()
