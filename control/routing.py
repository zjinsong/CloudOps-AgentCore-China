"""Conservative, tool-free fast lane. Ambiguous requests retain expert execution."""
import re


def conversational(task):
    if task.get('file_ids') or task.get('routine_id') or task.get('parent_id') or task.get('explicit_owners'):
        return False
    text = task['prompt'].strip().lower()
    # A greeting followed by a real investigation must never bypass tools.
    intent = re.sub(r'(?:不需要|不要|不用)(?:查询|检查|调用)[^。！？\n]*', '', text)
    if re.search(r'多少钱|单价|报价|价格是多少|按需价格|\b(?:how much|price of|price for|pricing for)\b', intent):
        return False
    if re.search(r'价格|\bpric(?:e|ing)\b', intent) and re.search(r'cn-|宁夏|北京|\b[a-z][a-z0-9-]*\.(?:nano|micro|small|medium|large|\d*xlarge|metal)\b', intent):
        return False
    if re.search(r'查询|查一下|查下|查找|检查|查看|列出|统计|找出|分析.*(?:费用|告警|异常)|最近|今天|昨日|昨天|当前|我的|本账户|本账号|部署|执行|删除|修改|更新|创建|生成.*(?:脚本|plan)|arn:|\b(?:query|lookup|check|list|fetch|deploy|create|delete|update|execute|run|my|our|current|today|yesterday|recent|last|i-[a-f0-9]+|\d{12})\b', intent):
        return False
    return bool(re.search(r'^(?:请问|请教一下)?(?:你好|您好|嗨|谢谢|hello\b|hi\b|thank\b|你是谁|你能做什么|你是做什么|请?(?:用一句话)?介绍|说一下|说说|讲讲|给我讲讲|什么是|解释|请解释|帮我解释|如何理解|怎么理解|what is\b|what are\b|who are\b|explain\b|please explain\b|tell me about\b|翻译|请翻译)', intent))


def interactive(task):
    return conversational(task) or (task.get('owner') == 'supervisor' and not task.get('routine_id'))
