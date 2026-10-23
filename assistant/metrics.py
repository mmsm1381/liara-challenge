"""Turn Liara's raw metric series into the percentages the assistant reasons about."""


def _latest(series):
    """series: [{"applet"|"name": .., "value": [[ts, "num"], ...]}] -> sum of each applet's newest value."""
    total, seen = 0.0, False
    for row in series or []:
        values = row.get("value") if isinstance(row, dict) else None
        if not values:
            continue
        try:
            newest = max(values, key=lambda pair: float(pair[0]))
            total += float(newest[1])
            seen = True
        except (TypeError, ValueError, IndexError):
            continue
    return total if seen else None


def usage_percent(metrics: dict, plan: dict | None) -> dict:
    """{"ram_percent": .., "cpu_percent": ..}; a value stays None when it can't be computed."""
    out = {"ram_percent": None, "cpu_percent": None}
    if not isinstance(metrics, dict) or not plan:
        return out
    mem = _latest(metrics.get("memoryUsage"))
    if mem is not None and plan.get("ram_mb"):
        out["ram_percent"] = round(100 * mem / (plan["ram_mb"] * 1024 * 1024), 1)
    cpu = _latest(metrics.get("cpuUsage"))
    if cpu is not None and plan.get("cpu"):
        out["cpu_percent"] = round(100 * cpu / plan["cpu"], 1)
    return out


def site_usage(client, app_name, plan_id) -> dict:
    """Fetch metrics and plans and return usage percentages; never raises on Liara errors."""
    from .liara_client import LiaraError

    try:
        metrics = client.get_metrics_summary(app_name)
        plan = next((p for p in client.list_plans() if p.get("id") == plan_id), None)
    except LiaraError:
        return {"ram_percent": None, "cpu_percent": None}
    return usage_percent(metrics, plan)
