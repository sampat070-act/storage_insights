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
