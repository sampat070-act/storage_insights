"""
dashboard.py

A small web server exposing storage_insights' capacity, cost, savings,
and forecast data as a single-page executive dashboard.

Like mcp_server.py, this is purely a presentation layer: every number on
the page comes from capacity_report.py's existing functions (the same
engine the CLI and MCP server use). This file's only job is to fetch
that data, group/aggregate it for the page, and serve it as JSON plus a
static HTML/CSS/JS page that renders it -- no cost, tiering, or
forecasting math is reimplemented here.
"""

import argparse
import os

import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from capacity_report import (
    DEFAULT_TIER,
    PRICING_PER_GB_MONTH,
    build_bucket_report,
    build_savings_report,
    connect_s3,
    estimate_monthly_cost,
    forecast_growth,
    format_money,
    get_demo_buckets,
    get_demo_history,
    get_live_buckets,
    human_readable_size,
    read_snapshot_history,
    summarize_buckets,
)

DASHBOARD_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
FORECAST_MONTHS_AHEAD = 6

# Set from --demo at startup; only controls which mode the page *opens*
# in -- the UI's Live/Demo toggle can still switch either way afterward
# without restarting the server.
DEFAULT_DEMO = False


def _get_buckets(demo: bool):
    """Fetch bucket data -- demo (synthetic) or live (real MinIO) -- as a list of dicts."""
    if demo:
        return get_demo_buckets()
    return get_live_buckets(connect_s3())


def _build_forecast_timeline(history_rows, forecast):
    """
    Turn the raw per-bucket history rows into one point per SNAPSHOT
    (summed across all buckets at that moment), plus the forecast's
    final projected total -- the series the trend chart plots.

    record_snapshot() writes every bucket in one run under the same
    timestamp, so grouping by exact timestamp naturally groups "one
    snapshot event" across buckets. Each group is summed with the
    existing summarize_buckets() (it already knows how to sum
    total_bytes across a list of bucket-shaped dicts) -- nothing new is
    computed here, just grouped and reused.
    """
    by_timestamp = {}
    for row in history_rows:
        by_timestamp.setdefault(row["timestamp"], []).append(row)

    timeline = []
    for timestamp in sorted(by_timestamp):
        total_bytes = summarize_buckets(by_timestamp[timestamp])["total_bytes"]
        timeline.append(
            {
                "label": timestamp.strftime("%b '%y"),
                "total_bytes": total_bytes,
                "projected": False,
            }
        )

    if forecast["bucket_count"] > 0:
        timeline.append(
            {
                "label": f"+{forecast['months_ahead']}mo",
                "total_bytes": forecast["total_projected_bytes"],
                "projected": True,
            }
        )

    return timeline


async def api_dashboard(request):
    demo = request.query_params.get("demo", "false").lower() == "true"

    buckets = _get_buckets(demo)
    totals = summarize_buckets(buckets)
    monthly_cost = estimate_monthly_cost(totals["total_bytes"], PRICING_PER_GB_MONTH[DEFAULT_TIER])

    savings = build_savings_report(buckets)

    history_rows = get_demo_history() if demo else read_snapshot_history(mode="live")
    forecast = forecast_growth(history_rows, months_ahead=FORECAST_MONTHS_AHEAD)
    forecast["timeline"] = _build_forecast_timeline(history_rows, forecast)

    return JSONResponse(
        {
            "mode": "demo" if demo else "live",
            "summary": {
                "bucket_count": len(buckets),
                "total_objects": totals["total_objects"],
                "total_bytes": totals["total_bytes"],
                "total_size_human": human_readable_size(totals["total_bytes"]),
                "estimated_monthly_cost_usd": round(monthly_cost, 2),
                "estimated_monthly_cost_display": format_money(monthly_cost),
            },
            "buckets": [build_bucket_report(b) for b in buckets],
            "savings": savings,
            "forecast": forecast,
        }
    )


async def index(request):
    with open(DASHBOARD_HTML_PATH) as f:
        html = f.read()
    html = html.replace("{{DEFAULT_DEMO}}", "true" if DEFAULT_DEMO else "false")
    return HTMLResponse(html)


app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/dashboard", api_dashboard),
    ]
)


def main():
    global DEFAULT_DEMO

    parser = argparse.ArgumentParser(description="Run the storage_insights executive dashboard.")
    parser.add_argument("--port", type=int, default=8000, help="Port to serve on (default: 8000).")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Open with synthetic demo data instead of live MinIO data (togglable in the UI either way).",
    )
    args = parser.parse_args()
    DEFAULT_DEMO = args.demo

    print(f"storage_insights dashboard: http://127.0.0.1:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
