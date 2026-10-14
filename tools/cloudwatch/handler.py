"""cloudwatch Gateway Lambda target —— 查询 CloudWatch 指标与告警。

Gateway Lambda 契约同 cost-explorer:工具名从 client_context 取并去前缀,dict 分发,返回纯 JSON。
"""
import json
import os
from datetime import datetime, timedelta, timezone

import boto3

_REGION = os.environ.get("AWS_REGION", "cn-northwest-1")
_cw = boto3.client("cloudwatch", region_name=_REGION)


def _tool_name(context) -> str:
    try:
        full = context.client_context.custom["bedrockAgentCoreToolName"]
        return full.split("___", 1)[1] if "___" in full else full
    except (AttributeError, KeyError, TypeError):
        return ""


def list_alarms(args: dict) -> dict:
    """列出告警。args: state(可选 OK|ALARM|INSUFFICIENT_DATA)。"""
    kwargs = {"MaxRecords": 100, 'AlarmTypes': ['MetricAlarm', 'CompositeAlarm']}
    if args.get("state"):
        kwargs["StateValue"] = args["state"]
    collected = []
    while True:
        resp = _cw.describe_alarms(**kwargs)
        collected.extend(resp.get('MetricAlarms', []) + resp.get('CompositeAlarms', []))
        if not resp.get('NextToken'):
            break
        kwargs['NextToken'] = resp['NextToken']
    alarms = [
        {
            "name": a["AlarmName"],
            "state": a["StateValue"],
            "metric": a.get("MetricName"),
            "namespace": a.get("Namespace"),
            "reason": a.get("StateReason", ""),
            'arn': a.get('AlarmArn'),
            'type': 'composite' if 'AlarmRule' in a else 'metric',
            'rule': a.get('AlarmRule'),
        }
        for a in collected
    ]
    return {"count": len(alarms), "alarms": alarms}


def get_metric_statistics(args: dict) -> dict:
    """查询指标统计。args: namespace, metric_name, stat(默认 Average),
    hours(回看小时,默认 24), dimensions(可选 [{Name,Value}])。"""
    hours = int(args.get("hours", 24))
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    period = 3600 if hours <= 48 else 86400
    resp = _cw.get_metric_statistics(
        Namespace=args["namespace"],
        MetricName=args["metric_name"],
        Dimensions=args.get("dimensions", []),
        StartTime=start,
        EndTime=end,
        Period=period,
        Statistics=[args.get("stat", "Average")],
    )
    stat = args.get("stat", "Average")
    points = sorted(
        (
            {"time": dp["Timestamp"].isoformat(), "value": round(dp[stat], 4), "unit": dp.get("Unit", "")}
            for dp in resp.get("Datapoints", [])
        ),
        key=lambda x: x["time"],
    )
    return {"namespace": args["namespace"], "metric": args["metric_name"], "stat": stat, "datapoints": points}


_DISPATCH = {
    "list_alarms": list_alarms,
    "get_metric_statistics": get_metric_statistics,
}


def handler(event, context):
    tool = _tool_name(context)
    fn = _DISPATCH.get(tool)
    if not fn:
        return {"error": f"未知工具: {tool}", "available": list(_DISPATCH)}
    try:
        args = event if isinstance(event, dict) else json.loads(event)
        return fn(args)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "tool": tool}
