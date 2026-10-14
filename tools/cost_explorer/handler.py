"""cost-explorer Gateway Lambda target —— 查询真实 AWS 成本。

Gateway Lambda 契约(借鉴原项目):
- 工具名从 context.client_context.custom["bedrockAgentCoreToolName"] 取,
  格式 "<target>___<tool>",用 split("___")[1] 去掉前缀。
- 按工具名 dict 分发,返回纯 JSON(Gateway 负责 MCP 协议封装)。
"""
import json
from decimal import Decimal
import os

import boto3

_REGION = os.environ.get("AWS_REGION", "cn-northwest-1")
_ce = boto3.client("ce", region_name=_REGION)


def _tool_name(context) -> str:
    try:
        full = context.client_context.custom["bedrockAgentCoreToolName"]
        return full.split("___", 1)[1] if "___" in full else full
    except (AttributeError, KeyError, TypeError):
        return ""


def get_cost_by_service(args: dict) -> dict:
    """按服务查询某时间段成本。args: start(YYYY-MM-DD), end(YYYY-MM-DD), granularity(可选)。"""
    start = args["start"]
    end = args["end"]
    granularity = args.get("granularity", "MONTHLY")
    kwargs = dict(
        TimePeriod={"Start": start, "End": end},
        Granularity=granularity,
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    periods = {}
    while True:
        resp = _ce.get_cost_and_usage(**kwargs)
        for period in resp['ResultsByTime']:
            key = (period['TimePeriod']['Start'], period['TimePeriod']['End'])
            merged = periods.setdefault(key, {**period, 'Groups': []})
            merged['Groups'].extend(period['Groups'])
            merged['Estimated'] = merged.get('Estimated', False) or period.get('Estimated', False)
        if not resp.get('NextPageToken'):
            break
        kwargs['NextPageToken'] = resp['NextPageToken']
    out = []
    for period in sorted(periods.values(), key=lambda x: x['TimePeriod']['Start']):
        groups = [
            {
                "service": g["Keys"][0],
                "amount": float(Decimal(g["Metrics"]["UnblendedCost"]["Amount"]).quantize(Decimal('0.01'))),
                "amount_exact": g["Metrics"]["UnblendedCost"]["Amount"],
                "unit": g["Metrics"]["UnblendedCost"]["Unit"],
            }
            for g in period["Groups"]
        ]
        groups.sort(key=lambda x: x["amount"], reverse=True)
        out.append({
            "period": period["TimePeriod"],
            "estimated": period.get("Estimated", False),
            "total": float(sum((Decimal(g['amount_exact']) for g in groups), Decimal(0)).quantize(Decimal('0.01'))),
            "total_exact": str(sum((Decimal(g['amount_exact']) for g in groups), Decimal(0))),
            "by_service": groups,
        })
    totals = {}
    for period in out:
        for group in period['by_service']:
            key = (group['service'], group['unit'])
            totals[key] = totals.get(key, Decimal(0)) + Decimal(group['amount_exact'])
    currencies = {}
    for (_, unit), amount in totals.items():
        currencies[unit] = currencies.get(unit, Decimal(0)) + amount
    return {"results": out, "summary": {
        "period": {"Start": start, "End": end}, "estimated": any(p['estimated'] for p in out),
        "totals": [{"unit": unit, "amount_exact": str(amount), "amount": str(amount.quantize(Decimal('0.01')))} for unit, amount in currencies.items()],
        "by_service": [{"service": service, "unit": unit, "amount_exact": str(amount), "amount": str(amount.quantize(Decimal('0.01')))}
                       for (service, unit), amount in sorted(totals.items(), key=lambda x: x[1], reverse=True)],
        "note": "跨日累计先汇总原始精度，再舍入；不要累加各日已舍入金额。不同币种不相加。"}}


def get_cost_forecast(args: dict) -> dict:
    """预测未来成本。args: start(YYYY-MM-DD, 未来), end(YYYY-MM-DD)。"""
    resp = _ce.get_cost_forecast(
        TimePeriod={"Start": args["start"], "End": args["end"]},
        Metric="UNBLENDED_COST",
        Granularity=args.get("granularity", "MONTHLY"),
    )
    return {
        "total": round(float(resp["Total"]["Amount"]), 2),
        "unit": resp["Total"]["Unit"],
        "forecasts": [
            {"period": f["TimePeriod"], "amount": round(float(f["MeanValue"]), 2)}
            for f in resp.get("ForecastResultsByTime", [])
        ],
    }


_DISPATCH = {
    "get_cost_by_service": get_cost_by_service,
    "get_cost_forecast": get_cost_forecast,
}


def handler(event, context):
    tool = _tool_name(context)
    fn = _DISPATCH.get(tool)
    if not fn:
        return {"error": f"未知工具: {tool}", "available": list(_DISPATCH)}
    try:
        args = event if isinstance(event, dict) else json.loads(event)
        return fn(args)
    except Exception as e:  # noqa: BLE001  工具层把错误作为数据返回给 agent
        return {"error": str(e), "tool": tool}
