"""Read-only health check; no model invocations or resource mutations."""
import json,sys
from pathlib import Path
import boto3
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.deploy import load,raw_fields

def main():
    cfg=load();cp=boto3.client('bedrock-agentcore-control',region_name=cfg['region']);raw_fields(cp)
    assert len(cfg.get('runtimes',{}))==6
    for name,r in cfg['runtimes'].items():
        live=cp.get_agent_runtime(agentRuntimeId=r['id'])
        endpoint=cp.get_agent_runtime_endpoint(agentRuntimeId=r['id'],endpointName='DEFAULT')
        assert live['status']=='READY' and live.get('platformVersion')=='V2'
        assert endpoint['status']=='READY' and endpoint['targetVersion']==live['agentRuntimeVersion']
        print(name,'READY','V2',live['agentRuntimeVersion'])
    g=cp.get_gateway(gatewayIdentifier=cfg['gateway_id']);assert g['authorizerType']=='AWS_IAM' and g['status']=='READY'
    targets=[t for p in cp.get_paginator('list_gateway_targets').paginate(gatewayIdentifier=g['gatewayId']) for t in p['items']]
    assert len(targets)==8 and all(t['status']=='READY' for t in targets)
    print('Gateway: 8 IAM targets READY')

if __name__=='__main__':main()
