# storage_insights

Connects to a MinIO server (S3-compatible object storage) and reports, for
every bucket, how many objects it holds, how much space they use, and what
that costs per month at S3 Standard vs. colder tiers (Standard-IA, Glacier
Flexible Retrieval, Glacier Deep Archive).

## Setup

```
pip install -r requirements.txt
```

By default the script connects to `http://localhost:9000` using MinIO's
standard local dev credentials. Override with environment variables if
needed:

```
export MINIO_ENDPOINT=http://your-minio-host:9000
export MINIO_ACCESS_KEY=your-access-key
export MINIO_SECRET_KEY=your-secret-key
```

## Usage

```
python capacity_report.py
```

### Demo mode

```
python capacity_report.py --demo
```

Runs the report against synthetic, production-scale bucket data instead of
connecting to MinIO -- useful for demos or trying out the tool without a
live server. Output is clearly marked with a `=== DEMO MODE (synthetic
data) ===` header. Demo mode reuses the exact same cost and formatting
logic as live mode; only the data source changes.

### Snapshot logging

```
python capacity_report.py --snapshot
```

Appends one row per bucket (timestamp, object count, total size) to
`snapshots.csv` instead of printing the full report -- intended for a
periodic cron job. Building up rows over time is what lets the MCP
server's `forecast_growth` tool (below) project future growth from real
history instead of a single point-in-time snapshot.

## MCP server

`mcp_server.py` exposes the same capacity, cost, and forecasting logic as
an MCP (Model Context Protocol) tool server, so an MCP client (e.g. Claude
Desktop, Claude Code) can query it conversationally instead of running the
CLI by hand. It reuses every calculation from `capacity_report.py` --
there is no separate cost or tiering logic to keep in sync.

```
python mcp_server.py
```

This starts the server listening on stdio; it's meant to be launched as a
subprocess by an MCP client's config, not run standalone.

### Tools

- **`get_storage_summary(demo=False)`** -- total capacity, object count,
  and estimated monthly cost across all buckets.
- **`get_bucket_details(bucket_name=None, demo=False)`** -- capacity,
  cost, last-accessed age, and suggested tier for one bucket, or for
  every bucket if `bucket_name` is omitted.
- **`find_savings(demo=False)`** -- buckets that are mis-tiered for how
  long it's been since they were last accessed, with current cost,
  suggested tier, cost at that tier, and monthly savings (plus a total
  across all buckets).
- **`record_snapshot(demo=False)`** -- append a snapshot of current
  bucket sizes to `snapshots.csv`, the same log `--snapshot` writes to.
  Call this repeatedly over time (e.g. via a daily cron job) to build up
  the history `forecast_growth` needs.
- **`forecast_growth(months=6, demo=True)`** -- fit a simple linear trend
  to each bucket's snapshot history and project its size and cost
  `months` ahead. Demo mode (the default) uses ~6 months of synthetic
  history with varied per-bucket growth rates, so it works immediately
  without waiting for real data. Live mode reads `snapshots.csv` and
  needs at least 2 real snapshots per bucket before a bucket is
  forecastable. Every result includes an `assumption` field stating the
  projection assumes current linear growth continues unchanged --
  it's a directional estimate, not a guarantee.

Every tool accepts `demo: bool` to run against synthetic demo data
instead of connecting to a live MinIO server, mirroring the CLI's
`--demo` flag.
