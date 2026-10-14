"""OpenAI-compatible model adapter; secrets are outside the public catalog."""
import json
import os
import boto3
from strands.models.openai import OpenAIModel

def build_model(model_id='configured-model', **params):
    api_key=os.getenv('MODEL_API_KEY')
    if not api_key and os.getenv('MODEL_SECRET_ARN'):
        response=boto3.client('secretsmanager',region_name=os.getenv('AWS_REGION','cn-northwest-1')).get_secret_value(SecretId=os.environ['MODEL_SECRET_ARN'])
        api_key=json.loads(response['SecretString'])['api_key']
    if not api_key:
        raise RuntimeError('Configure MODEL_SECRET_ARN (cloud) or MODEL_API_KEY (local)')
    base_url=os.getenv('MODEL_BASE_URL')
    if not base_url or not base_url.startswith('https://'):
        raise RuntimeError('MODEL_BASE_URL must be an approved HTTPS model endpoint')
    options={'temperature':0.3,'max_tokens':4096,**params}
    if os.getenv('MODEL_EXTRA_BODY'):
        options['extra_body']=json.loads(os.environ['MODEL_EXTRA_BODY'])
    return OpenAIModel(client_args={'api_key':api_key,'base_url':base_url,'timeout':180,'max_retries':0},
                       model_id=os.getenv('MODEL_ID') or model_id,params=options)
