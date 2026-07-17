"""
mcp_server.py

An MCP (Model Context Protocol) server that exposes our storage capacity/
cost analytics as a "tool" Claude can call conversationally, instead of
someone running capacity_report.py by hand and reading terminal output.

MCP servers talk to clients (like Claude Desktop or Claude Code) over
stdio (standard in/out) using JSON-RPC under the hood -- the `mcp` SDK's
FastMCP class handles that protocol plumbing for us. All we do is:
  1. create a FastMCP server instance,
  2. register a Python function as a "tool" with @mcp.tool(),
  3. call mcp.run() to start listening.

A "tool" in MCP terms is a function Claude can decide to call mid-
conversation. The function's docstring and type-hinted parameters are
what Claude reads to know what the tool does and how to call it -- they
are part of the interface, not just documentation for humans.
"""

from datetime import datetime, timezone
from typing import Optional, TypedDict

import boto3
from mcp.server.fastmcp import FastMCP

# We import (not reimplement) all capacity/cost logic from the existing
# CLI tool, so the MCP server and `python capacity_report.py` always
# agree on numbers.
from capacity_report import (
    ACCESS_KEY,
    DEFAULT_TIER,
    MINIO_ENDPOINT,
    PRICING_PER_GB_MONTH,
    SECRET_KEY,
    SNAPSHOT_LOG_PATH,
    estimate_monthly_cost,
    format_money,
    get_demo_buckets,
    get_demo_history,
    get_live_buckets,
    human_readable_size,
    read_snapshot_history,
    suggest_tier,
    summarize_buckets,
)
from capacity_report import forecast_growth as _forecast_growth
from capacity_report import record_snapshot as _write_snapshot_rows

# One FastMCP instance = one MCP server. The name shows up in MCP client
# UIs (e.g. Claude Desktop's tool/server list) so users can tell which
# server a tool came from.
mcp = FastMCP("storage-insights")


def _connect():
    """
    Build a boto3 S3 client pointed at MinIO, using the same
    endpoint/credential env vars (with the same local-dev defaults) as
    capacity_report.py's main(). Shared by every tool below so connection
    setup lives in exactly one place.
    """
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
    )


def _get_buckets(demo: bool):
    """Fetch bucket data -- demo (synthetic) or live (real MinIO) -- as a list of dicts."""
    if demo:
        return get_demo_buckets()
    return get_live_buckets(_connect())


def _days_since(last_modified) -> Optional[int]:
    """Days between now and a bucket's last-modified timestamp, or None if unknown."""
    if last_modified is None:
        return None
    return (datetime.now(timezone.utc) - last_modified).days


class StorageSummary(TypedDict):
    """
    The shape of get_storage_summary()'s return value. FastMCP reads this
    type to build a JSON schema for the tool's output -- that's what lets
    the MCP client (Claude) receive real "structured content" (typed
    fields) instead of a plain block of text it would have to parse
    itself. A bare `-> dict` return type doesn't carry enough information
    to do this, which is why we spell out the fields here.
    """

    mode: str
    bucket_count: int
    total_objects: int
    total_bytes: int
    total_size_human: str
    cost_tier: str
    estimated_monthly_cost_usd: float
    estimated_monthly_cost_display: str


@mcp.tool()
def get_storage_summary(demo: bool = False) -> StorageSummary:
    """
    Get total capacity, total object count, and estimated monthly storage
    cost across all buckets.

    Args:
        demo: If True, return synthetic demo data instead of connecting
            to a live MinIO server. Useful when no real MinIO server is
            reachable, e.g. for demonstrations.
    """
    buckets = _get_buckets(demo)

    totals = summarize_buckets(buckets)
    monthly_cost = estimate_monthly_cost(
        totals["total_bytes"], PRICING_PER_GB_MONTH[DEFAULT_TIER]
    )

    # A plain dict return value becomes MCP "structured content" -- the
    # client (Claude) gets real fields to reason over and present, not
    # just a blob of text it has to parse itself.
    return {
        "mode": "demo" if demo else "live",
        "bucket_count": len(buckets),
        "total_objects": totals["total_objects"],
        "total_bytes": totals["total_bytes"],
        "total_size_human": human_readable_size(totals["total_bytes"]),
        "cost_tier": DEFAULT_TIER,
        "estimated_monthly_cost_usd": round(monthly_cost, 2),
        "estimated_monthly_cost_display": format_money(monthly_cost),
    }


class BucketDetail(TypedDict):
    """The shape of one bucket's entry in get_bucket_details()'s return value."""

    name: str
    object_count: int
    total_bytes: int
    total_size_human: str
    days_since_last_access: Optional[int]
    estimated_monthly_cost_usd: float
    estimated_monthly_cost_display: str
    suggested_tier: Optional[str]


class BucketDetailsResult(TypedDict):
    """The shape of get_bucket_details()'s return value."""

    mode: str
    buckets: list[BucketDetail]


def _to_bucket_detail(bucket: dict) -> BucketDetail:
    """
    Build a BucketDetail from one of get_live_buckets()/get_demo_buckets()'s
    raw bucket dicts, using capacity_report.py's own cost and tiering
    logic (estimate_monthly_cost, suggest_tier) so these numbers always
    match what the CLI would print.
    """
    total_bytes = bucket["total_bytes"]
    days_since_last_access = _days_since(bucket["last_modified"])
    monthly_cost = estimate_monthly_cost(total_bytes, PRICING_PER_GB_MONTH[DEFAULT_TIER])

    return {
        "name": bucket["name"],
        "object_count": bucket["object_count"],
        "total_bytes": total_bytes,
        "total_size_human": human_readable_size(total_bytes),
        "days_since_last_access": days_since_last_access,
        "estimated_monthly_cost_usd": round(monthly_cost, 2),
        "estimated_monthly_cost_display": format_money(monthly_cost),
        "suggested_tier": (
            suggest_tier(days_since_last_access)
            if days_since_last_access is not None
            else None
        ),
    }


@mcp.tool()
def get_bucket_details(
    bucket_name: Optional[str] = None, demo: bool = False
) -> BucketDetailsResult:
    """
    Get detailed capacity, cost, and tiering info for a single bucket, or
    for every bucket if no name is given.

    For each bucket this includes: object count, total size, estimated
    monthly cost at S3 Standard, how many days since it was last
    modified/accessed, and a suggested colder storage tier if the bucket
    looks mis-tiered for its age (None if S3 Standard is still the right
    call).

    Args:
        bucket_name: Name of a specific bucket to look up. If omitted,
            details for ALL buckets are returned instead.
        demo: If True, return synthetic demo data instead of connecting
            to a live MinIO server. Useful when no real MinIO server is
            reachable, e.g. for demonstrations.
    """
    buckets = _get_buckets(demo)

    if bucket_name is not None:
        buckets = [b for b in buckets if b["name"] == bucket_name]
        if not buckets:
            raise ValueError(f"No bucket named {bucket_name!r} found.")

    return {
        "mode": "demo" if demo else "live",
        "buckets": [_to_bucket_detail(b) for b in buckets],
    }


class SavingsOpportunity(TypedDict):
    """One mis-tiered bucket found by find_savings(), and what fixing it would save."""

    name: str
    days_since_last_access: int
    current_tier: str
    current_monthly_cost_usd: float
    suggested_tier: str
    suggested_monthly_cost_usd: float
    monthly_savings_usd: float


class FindSavingsResult(TypedDict):
    """The shape of find_savings()'s return value."""

    mode: str
    opportunities: list[SavingsOpportunity]
    total_monthly_savings_usd: float
    total_monthly_savings_display: str


@mcp.tool()
def find_savings(demo: bool = False) -> FindSavingsResult:
    """
    Find buckets that are mis-tiered for how long it's been since they
    were last accessed, and estimate how much moving each one to a
    cheaper tier would save per month.

    Uses the same age-based tiering rule as capacity_report.py's CLI
    output (suggest_tier): buckets untouched for 30+ days should move to
    Standard-IA, 90+ days to Glacier Flexible Retrieval, and 270+ days to
    Glacier Deep Archive. Buckets accessed recently enough that S3
    Standard is still correct are left out of the results.

    Args:
        demo: If True, analyze synthetic demo data instead of connecting
            to a live MinIO server. Useful when no real MinIO server is
            reachable, e.g. for demonstrations.
    """
    buckets = _get_buckets(demo)

    opportunities: list[SavingsOpportunity] = []
    for bucket in buckets:
        days_since_last_access = _days_since(bucket["last_modified"])
        if days_since_last_access is None:
            continue

        suggested_tier = suggest_tier(days_since_last_access)
        if suggested_tier is None:
            continue

        total_bytes = bucket["total_bytes"]
        current_cost = estimate_monthly_cost(
            total_bytes, PRICING_PER_GB_MONTH[DEFAULT_TIER]
        )
        suggested_cost = estimate_monthly_cost(
            total_bytes, PRICING_PER_GB_MONTH[suggested_tier]
        )
        savings = current_cost - suggested_cost

        opportunities.append(
            {
                "name": bucket["name"],
                "days_since_last_access": days_since_last_access,
                "current_tier": DEFAULT_TIER,
                "current_monthly_cost_usd": round(current_cost, 2),
                "suggested_tier": suggested_tier,
                "suggested_monthly_cost_usd": round(suggested_cost, 2),
                "monthly_savings_usd": round(savings, 2),
            }
        )

    opportunities.sort(key=lambda o: o["monthly_savings_usd"], reverse=True)
    total_savings = sum(o["monthly_savings_usd"] for o in opportunities)

    return {
        "mode": "demo" if demo else "live",
        "opportunities": opportunities,
        "total_monthly_savings_usd": round(total_savings, 2),
        "total_monthly_savings_display": format_money(total_savings),
    }


class SnapshotResult(TypedDict):
    """The shape of record_snapshot()'s return value."""

    mode: str
    timestamp_utc: str
    bucket_count: int
    snapshot_path: str


@mcp.tool()
def record_snapshot(demo: bool = False) -> SnapshotResult:
    """
    Record a point-in-time snapshot of every bucket's size and object
    count to the snapshot log (one CSV row per bucket, appended to
    capacity_report.py's snapshot log file). Call this to build up a
    history of bucket sizes over time.

    A single snapshot can't show growth by itself -- it's only useful
    once several snapshots have accumulated (e.g. one per day). Once
    there's enough history, that log is what a future growth-projection
    feature would read from.

    Args:
        demo: If True, record a snapshot of synthetic demo data instead
            of connecting to a live MinIO server. Useful for trying out
            the snapshot mechanism without a live server.
    """
    buckets = _get_buckets(demo)
    mode = "demo" if demo else "live"
    rows = _write_snapshot_rows(buckets, mode=mode)

    return {
        "mode": mode,
        "timestamp_utc": rows[0]["timestamp_utc"] if rows else datetime.now(timezone.utc).isoformat(),
        "bucket_count": len(rows),
        "snapshot_path": SNAPSHOT_LOG_PATH,
    }


class BucketForecast(TypedDict):
    """One bucket's entry in forecast_growth()'s return value."""

    name: str
    data_points: int
    current_total_bytes: int
    current_size_human: str
    current_monthly_cost_usd: float
    monthly_growth_bytes: int
    monthly_growth_human: str
    projected_total_bytes: int
    projected_size_human: str
    projected_monthly_cost_usd: float


class ForecastGrowthResult(TypedDict):
    """The shape of forecast_growth()'s return value."""

    mode: str
    months_ahead: int
    bucket_count: int
    buckets: list[BucketForecast]
    total_current_bytes: int
    total_current_size_human: str
    total_current_monthly_cost_usd: float
    total_projected_bytes: int
    total_projected_size_human: str
    total_projected_monthly_cost_usd: float
    assumption: str


@mcp.tool()
def forecast_growth(months: int = 6, demo: bool = True) -> ForecastGrowthResult:
    """
    Project each bucket's storage size and monthly cost `months` months
    into the future by fitting a simple linear trend (ordinary least
    squares over time vs. size) to its historical snapshots, then
    reading that line's value at a future point in time. Reuses
    capacity_report.py's existing cost math (estimate_monthly_cost) for
    both current and projected cost -- pricing itself is never
    recomputed here.

    Demo mode (the default) forecasts against ~6 months of
    fabricated-but-varied synthetic history for the 6 demo buckets
    (media-assets and logs-cold grow fastest, active-workloads slowest)
    -- use this to see a forecast today without waiting for real history
    to accumulate.

    Live mode forecasts against the real snapshot log written by
    record_snapshot(). A bucket needs at least 2 recorded snapshots to
    be forecastable; call record_snapshot() repeatedly over time (e.g. a
    daily cron job) to build that history up.

    IMPORTANT: the result always includes an "assumption" field stating
    that this projects PAST linear growth forward unchanged. It is a
    directional estimate, not a guarantee -- always surface that caveat
    alongside the numbers, never present a forecast as certain.

    Args:
        months: How many months ahead to project. Defaults to 6.
        demo: If True (the default), forecast against synthetic demo
            history. If False, forecast against the real snapshot log,
            which may have too little history yet to forecast anything.
    """
    if demo:
        history_rows = get_demo_history()
    else:
        history_rows = read_snapshot_history(mode="live")

    result = _forecast_growth(history_rows, months_ahead=months)

    return {
        "mode": "demo" if demo else "live",
        **result,
    }


# Starts the server listening on stdio. This call blocks -- an MCP
# client (Claude Desktop, Claude Code, or our own test client) launches
# this script as a subprocess and talks to it over stdin/stdout, so
# there's no network port to configure.
if __name__ == "__main__":
    mcp.run()
