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

from typing import TypedDict

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
    estimate_monthly_cost,
    format_money,
    get_demo_buckets,
    get_live_buckets,
    human_readable_size,
    summarize_buckets,
)

# One FastMCP instance = one MCP server. The name shows up in MCP client
# UIs (e.g. Claude Desktop's tool/server list) so users can tell which
# server a tool came from.
mcp = FastMCP("storage-insights")


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
    if demo:
        buckets = get_demo_buckets()
    else:
        # Same connection setup as capacity_report.py's main() -- reads
        # MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY from the environment (or
        # their local-MinIO defaults) rather than hardcoding anything.
        s3 = boto3.client(
            "s3",
            endpoint_url=MINIO_ENDPOINT,
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=SECRET_KEY,
        )
        buckets = get_live_buckets(s3)

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


# Starts the server listening on stdio. This call blocks -- an MCP
# client (Claude Desktop, Claude Code, or our own test client) launches
# this script as a subprocess and talks to it over stdin/stdout, so
# there's no network port to configure.
if __name__ == "__main__":
    mcp.run()
