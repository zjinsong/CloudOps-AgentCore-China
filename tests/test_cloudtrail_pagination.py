import json
import unittest
from unittest.mock import patch
from tools.cloudtrail.pagination import lookup, routine_agentcore_event


class CloudTrailPaginationTests(unittest.TestCase):
    def test_auto_paginates_with_unchanged_parameters_and_deduplicates(self):
        calls=[]
        def fetch(args, timeout):
            calls.append(dict(args))
            return {'events':[{'EventId':'same','EventName':'DeleteBucket'}],
                    'next_token':'next' if len(calls)==1 else None,'query_params':{k:v for k,v in args.items() if k!='next_token'}}
        with patch('tools.cloudtrail.pagination.time.sleep'):
            r=lookup({'start_time':'start','end_time':'end','attribute_key':'EventName','attribute_value':'DeleteBucket'},fetch)
        self.assertTrue(r['complete']);self.assertEqual(r['pages_fetched'],2);self.assertEqual(len(r['events']),1)
        self.assertEqual(calls[1],{**calls[0],'next_token':'next'})
    def test_page_budget_is_partial_and_token_is_reusable(self):
        with patch('tools.cloudtrail.pagination.time.sleep'):
            r=lookup({'max_pages':1},lambda a,t:{'events':[],'next_token':'remaining'})
        self.assertFalse(r['complete']);self.assertEqual(r['next_token'],'remaining')
        self.assertEqual(r['queries'][0]['stopped_reason'],'page_budget')
        self.assertIn('start_time',r['query_params']);self.assertIn('end_time',r['query_params'])
    def test_targeted_filters_use_one_event_name_each(self):
        calls=[]
        def fetch(a,t):calls.append(a);return {'events':[]}
        with patch('tools.cloudtrail.pagination.time.sleep'):
            r=lookup({'event_names':['DeleteBucket','DeleteRole','PutBucketPolicy']},fetch)
        self.assertTrue(r['complete']);self.assertEqual(len(calls),3)
        self.assertEqual([a['attribute_value'] for a in calls],['DeleteBucket','DeleteRole','PutBucketPolicy'])
        self.assertTrue(all(a['attribute_key']=='EventName' and a['region']=='cn-northwest-1' for a in calls))
        self.assertEqual(len(set(a['start_time'] for a in calls)),1)
    def test_noise_is_conservative_and_can_be_included(self):
        service={'EventName':'AssumeRole','CloudTrailEvent':json.dumps({'userIdentity':{'type':'AWSService','invokedBy':'bedrock-agentcore.amazonaws.com'}})}
        human={'EventName':'AssumeRole','CloudTrailEvent':json.dumps({'userIdentity':{'type':'IAMUser'},'requestParameters':{'roleArn':'agentcore-demo-cloudops-expert'}})}
        self.assertTrue(routine_agentcore_event(service));self.assertFalse(routine_agentcore_event(human))
        def fetch(a,t):return {'events':[service,human]}
        r=lookup({},fetch);self.assertEqual(len(r['events']),1);self.assertEqual(r['noise_removed']['AssumeRole'],1)
        self.assertEqual(len(lookup({'include_noise':True},fetch)['events']),2)
        self.assertEqual(len(lookup({'attribute_key':'EventName','attribute_value':'AssumeRole'},fetch)['events']),2)
    def test_midquery_error_preserves_evidence_and_reports_incomplete(self):
        count=0
        def fetch(a,t):
            nonlocal count
            count+=1
            if count==2:raise TimeoutError('backend deadline')
            return {'events':[{'EventId':'1','EventName':'DeleteRole'}],'next_token':'next'}
        with patch('tools.cloudtrail.pagination.time.sleep'):
            r=lookup({},fetch)
        self.assertFalse(r['complete']);self.assertEqual(len(r['events']),1)
        self.assertEqual(r['next_token'],'next');self.assertIn('query_error',r['queries'][0]['stopped_reason'])
    def test_continuation_and_output_limits_are_explicit(self):
        with self.assertRaises(ValueError):lookup({'next_token':'next'},lambda a,t:None)
        r=lookup({'max_events':1},lambda a,t:{'events':[{'EventId':'1'},{'EventId':'2'}]})
        self.assertTrue(r['complete']);self.assertTrue(r['output_truncated']);self.assertEqual(r['omitted_matching_events'],1)
    def test_beijing_is_allowed_for_global_iam_events_and_global_regions_rejected(self):
        calls=[]
        def fetch(a,t):calls.append(a);return {'events':[]}
        result=lookup({'region':'cn-north-1','attribute_key':'EventName','attribute_value':'PutRolePolicy'},fetch)
        self.assertEqual(calls[0]['region'],'cn-north-1')
        self.assertIn('cn-north-1',result['coverage'])
        with self.assertRaises(ValueError):lookup({'region':'us-east-1'},fetch)
    def test_large_saved_evidence_preserves_valid_json_and_query_completeness(self):
        from framework.evidence import bounded_tool_content
        payload={'events':[{'EventName':'PutRolePolicy','CloudTrailEvent':'x'*40000}],
                 'complete':True,'queries':[{'complete':True,'query_params':{'region':'cn-north-1'},'next_token':None}]}
        packed,truncated=bounded_tool_content([{'text':json.dumps(payload)}],20000)
        self.assertTrue(truncated);self.assertLess(len(packed.encode()),20000)
        saved=json.loads(json.loads(packed)[0]['text'])
        self.assertTrue(saved['complete']);self.assertTrue(saved['queries'][0]['complete'])
        self.assertEqual(saved['events_preview'][0]['EventName'],'PutRolePolicy')


if __name__=='__main__':unittest.main()
