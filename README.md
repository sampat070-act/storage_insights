# storage_insights

An MCP-powered storage analytics tool that surfaces capacity, cost, mis-tiering savings, and 6-month growth forecasts for S3-compatible object storage — all queryable in plain English through Claude.

*Built by a storage infrastructure engineer with 15 years across enterprise SAN, NAS, and software-defined object platforms — exploring what AI-native infrastructure tooling looks like.*

<!-- TODO: add 30-second demo GIF here -->

## Why this matters

Object storage bills grow quietly. Buckets accumulate data long after anyone is actively using it, sit in the most expensive tier by default, and nobody notices until finance asks why the cloud bill jumped. Answering "how much are we spending, where is it wasted, and what will it cost in six months?" usually means someone manually pulling reports and building a spreadsheet.

This tool answers those three questions on demand, conversationally, against live infrastructure — turning storage cost management from a quarterly fire drill into a question you can just ask.

## What it does

**Capacity reporting** — objects, size, and last-accessed age for every bucket, live from the storage backend.

**Cost estimation across S3 tiers** — every bucket's monthly cost at S3 Standard, and what it would cost instead at Standard-IA, Glacier Flexible Retrieval, or Glacier Deep Archive.

**Savings analysis** — flags buckets sitting in a hotter (more expensive) tier than their access pattern justifies, and quantifies the fix. On the demo dataset, this finds **$373.33/month in potential savings** across three mis-tiered buckets — one of them (`logs-cold`, untouched for 300 days) accounts for $145.20/month of that on its own.

**Growth forecasting** — fits a linear trend to historical snapshots and projects size and cost forward. On the demo dataset, a 6-month projection takes total storage from **34.30 TB to 49.00 TB**, and monthly cost from **$867.40 to $1,239.15** — a number worth knowing before it shows up as a surprise on next year's budget.

## Example

> **"Where am I wasting money?"**
>
> You're overpaying on 3 of 6 buckets — all sitting in hot S3 Standard storage despite going untouched for months:
>
> | Bucket | Idle for | Move to | Save/mo |
> |---|---|---|---|
> | logs-cold | 300 days | Glacier Deep Archive | $145.20 |
> | media-assets | 45 days | Standard-IA | $138.54 |
> | finance-archive | 120 days | Glacier Flexible Retrieval | $89.59 |
>
> **Total potential savings: $373.33/month** (~43% of current spend).

## Architecture

One shared analytics engine (`capacity_report.py`) with two interfaces on top of it — a CLI for scripting and cron/launchd jobs, and an MCP server (`mcp_server.py`) for conversational access. Every cost calculation, tiering rule, and growth projection lives in exactly one place; the MCP tools import and reuse that logic rather than reimplementing it, so the two interfaces can never disagree on a number.

It is built and tested against a local MinIO server, but MinIO speaks the same S3 API as AWS S3 and NetApp StorageGRID. Pointing `MINIO_ENDPOINT` at a real S3 or StorageGRID endpoint — or dropping it entirely for AWS — works unchanged, with no code changes required.

## How to run it

### Setup

```
pip install -r requirements.txt
```

By default it connects to `http://localhost:9000` using MinIO's standard local dev credentials. Override with environment variables for a real backend:

```
export MINIO_ENDPOINT=http://your-storage-host:9000
export MINIO_ACCESS_KEY=your-access-key
export MINIO_SECRET_KEY=your-secret-key
```

### CLI

```
python capacity_report.py            # live report
python capacity_report.py --demo     # synthetic demo data, no server needed
python capacity_report.py --snapshot # log a data point for forecasting
```

### MCP server (conversational access)

```
python mcp_server.py
```

Add it to Claude Code with:

```
claude mcp add storage-insights -- python /path/to/storage_insights/mcp_server.py
```

Or, for Claude Desktop, add it to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "storage-insights": {
      "command": "python",
      "args": ["/path/to/storage_insights/mcp_server.py"]
    }
  }
}
```

Once connected, ask Claude things like "what's my storage costing me?", "where am I wasting money?", or "what will my storage look like in 6 months?" — no dashboards, no manual reports.

**Tools exposed:** `get_storage_summary`, `get_bucket_details`, `find_savings`, `record_snapshot`, `forecast_growth` — every one supports a `demo` flag to run against synthetic data with no live server required.
