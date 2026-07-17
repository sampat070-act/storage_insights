"""
capacity_report.py

Connects to a MinIO server (S3-compatible object storage) and prints,
for every bucket, how many objects it holds and how much space they use.

MinIO speaks the same API as Amazon S3, so we can use boto3 (the AWS SDK)
to talk to it -- we just have to point boto3 at our local server instead
of the real AWS.
"""

# boto3 is the AWS SDK for Python. It knows how to make S3 API calls.
import boto3

# os.environ lets us read environment variables (settings passed in from
# outside the script, e.g. via the shell) instead of hardcoding secrets.
import os

# argparse gives us the --demo command-line flag.
import argparse

# csv lets us append snapshot rows without hand-rolling comma escaping.
import csv

# Used to build/compare "last accessed" timestamps, both for the real
# LastModified dates MinIO returns and for the synthetic demo dates.
from datetime import datetime, timedelta, timezone

# --- Connection settings -----------------------------------------------
# We read credentials from environment variables rather than hardcoding
# them, so nothing sensitive ends up committed to git. The values after
# the comma are defaults used if the environment variable isn't set --
# they match MinIO's standard local development credentials, so the
# script still works out of the box on a fresh local MinIO instance.
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://localhost:9000")
ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")

# --- S3 pricing (approximate, US-East-1, per GB per month) -------------
# A dict keyed by tier name, so it's easy to see all tiers at a glance
# and update a single price without touching any other code. These are
# list prices as of writing -- check the AWS S3 pricing page for current
# numbers before using this for real budgeting.
#
# Note on units: AWS bills using *decimal* GB (1 GB = 1,000,000,000 bytes),
# not the *binary* GiB (1024^3 bytes) that human_readable_size() below
# uses for display. That's standard practice for cloud billing -- it just
# means our cost math and our "MB/GB" display use slightly different
# definitions of a gigabyte. We handle the conversion in
# estimate_monthly_cost() below.
PRICING_PER_GB_MONTH = {
    "S3 Standard": 0.023,
    "S3 Standard-IA": 0.0125,
    "S3 Glacier Flexible Retrieval": 0.0036,
    "S3 Glacier Deep Archive": 0.00099,
}

# The tier we treat as the "default"/current cost, shown alongside capacity.
DEFAULT_TIER = "S3 Standard"

# Bytes in one decimal GB, used for cost calculations (see note above).
BYTES_PER_GB = 1_000_000_000

# Bytes in one binary TB (TiB), used to build the synthetic demo sizes so
# they match the units human_readable_size() displays (see that function).
BYTES_PER_TB = 1024**4

# --- Snapshot logging ----------------------------------------------------
# Where periodic snapshots get appended, one CSV row per bucket per run.
# A single snapshot only shows a moment in time; growth-trend analysis
# needs a *series* of snapshots (e.g. one per day via cron) to fit a
# trend against. Kept next to this script by default so behavior doesn't
# depend on the caller's working directory; overridable via env var for
# tests or custom deployments.
SNAPSHOT_LOG_PATH = os.environ.get(
    "SNAPSHOT_LOG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots.csv"),
)

SNAPSHOT_CSV_FIELDS = ["timestamp_utc", "mode", "bucket_name", "object_count", "total_bytes"]


def suggest_tier(days_since_access):
    """
    Suggest a cheaper storage tier based on how long it's been since a
    bucket was last touched. Returns a key from PRICING_PER_GB_MONTH, or
    None if the bucket is accessed recently enough that S3 Standard is
    still the right call.

    The age thresholds are a simplified version of common S3 lifecycle
    policies -- real ones also consider retrieval frequency and object
    size, not just age.
    """
    if days_since_access >= 270:
        return "S3 Glacier Deep Archive"
    if days_since_access >= 90:
        return "S3 Glacier Flexible Retrieval"
    if days_since_access >= 30:
        return "S3 Standard-IA"
    return None


def estimate_monthly_cost(total_bytes, price_per_gb):
    """
    Estimate the monthly storage cost for a given number of bytes at a
    given price-per-GB. Returns a dollar amount as a float.
    """
    gigabytes = total_bytes / BYTES_PER_GB
    return gigabytes * price_per_gb


def format_money(amount):
    """Format a dollar amount consistently, e.g. 1.5 -> "$1.50"."""
    return f"${amount:,.2f}"


def human_readable_size(num_bytes):
    """
    Convert a raw byte count (e.g. 1536) into a friendly string
    (e.g. "1.50 KB").

    We keep dividing by 1024 and moving up a unit (KB -> MB -> GB -> TB)
    until the number is small enough to read comfortably.
    """
    size = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    # If we somehow have more than 1024 TB, just show it in PB.
    return f"{size:.2f} PB"


def human_readable_signed_size(num_bytes):
    """
    Like human_readable_size(), but keeps a +/- sign -- for describing a
    CHANGE in size (e.g. growth per month) rather than an absolute size,
    where the direction matters as much as the magnitude.
    """
    sign = "-" if num_bytes < 0 else "+"
    return f"{sign}{human_readable_size(abs(num_bytes))}"


# --- Demo data -----------------------------------------------------------
# Hand-picked to tell a believable story: a mix of hot, warm, and cold
# buckets so the tiering suggestions in print_report() have something
# interesting to say. Object counts are chosen to match how each bucket
# would realistically be used (a database-snapshot bucket has a handful of
# huge files; a logs bucket has millions of tiny ones) -- they don't need
# to be exact, just plausible enough for a demo.
#
# Format: (name, size_in_tb, days_since_last_access, object_count)
DEMO_BUCKETS = [
    ("finance-archive", 4.2, 120, 125_000),
    ("engineering-backups", 8.5, 15, 340),
    ("prod-database-snapshots", 2.1, 2, 58),
    ("media-assets", 12, 45, 890_000),
    ("logs-cold", 6, 300, 4_200_000),
    ("active-workloads", 1.5, 0, 12_500),
]


def get_demo_buckets():
    """
    Build synthetic bucket data with the same shape get_live_buckets()
    returns, so print_report() can't tell (and doesn't need to know)
    whether it's looking at real MinIO data or made-up demo data.
    """
    now = datetime.now(timezone.utc)
    buckets = []
    for name, size_tb, days_ago, object_count in DEMO_BUCKETS:
        buckets.append(
            {
                "name": name,
                "object_count": object_count,
                "total_bytes": int(size_tb * BYTES_PER_TB),
                "last_modified": now - timedelta(days=days_ago),
            }
        )
    return buckets


# --- Demo history (for forecast_growth) -----------------------------------
# Fabricated MONTHLY growth rates (TB/month) per demo bucket, chosen to
# tell a varied story: media-assets and logs-cold grow fastest (lots of
# new media / constant log accumulation), prod-database-snapshots and
# active-workloads grow slowest (snapshots get pruned, active data gets
# rotated out). This is what makes forecast_growth() project different
# outcomes per bucket instead of just scaling everything by the same
# amount.
DEMO_HISTORY_GROWTH_TB_PER_MONTH = {
    "finance-archive": 0.2,
    "engineering-backups": 0.3,
    "prod-database-snapshots": 0.1,
    "media-assets": 1.0,
    "logs-cold": 0.8,
    "active-workloads": 0.05,
}


def get_demo_history(months=6):
    """
    Build ~`months` synthetic MONTHLY snapshots per demo bucket, ending
    at the exact current sizes get_demo_buckets() uses (so "now" agrees
    between the two), and growing backward in time at each bucket's rate
    from DEMO_HISTORY_GROWTH_TB_PER_MONTH.

    This is fabricated history for DEMO PURPOSES ONLY -- it lives
    entirely in memory and is never written to snapshots.csv, the real
    log record_snapshot() writes to. forecast_growth() can't tell the
    difference between this and real history because both come back in
    the same shape: a list of {timestamp, bucket_name, object_count,
    total_bytes} dicts.
    """
    now = datetime.now(timezone.utc)
    current_by_name = {
        name: (size_tb, object_count) for name, size_tb, _, object_count in DEMO_BUCKETS
    }

    history = []
    for bucket_name, growth_tb_per_month in DEMO_HISTORY_GROWTH_TB_PER_MONTH.items():
        current_size_tb, current_object_count = current_by_name[bucket_name]

        # months_ago counts down from the oldest point to 0 ("now"), so
        # the last snapshot generated always matches today's actual size.
        for months_ago in range(months - 1, -1, -1):
            size_tb = max(current_size_tb - growth_tb_per_month * months_ago, 0.01)
            # Scale object count down along with size so older snapshots
            # look proportionally smaller too, not just byte-for-byte.
            object_count = max(
                round(current_object_count * (size_tb / current_size_tb)), 1
            )
            history.append(
                {
                    "timestamp": now - timedelta(days=30 * months_ago),
                    "bucket_name": bucket_name,
                    "object_count": object_count,
                    "total_bytes": int(size_tb * BYTES_PER_TB),
                }
            )

    return history


def get_live_buckets(s3):
    """
    Query MinIO for every bucket and how much space/how many objects each
    one holds. Returns a list of dicts shaped exactly like
    get_demo_buckets() -- print_report() consumes either one identically.
    """
    response = s3.list_buckets()
    buckets = []

    for bucket in response["Buckets"]:
        bucket_name = bucket["Name"]

        object_count = 0
        total_bytes = 0
        last_modified = None

        # list_objects_v2 only returns up to 1000 objects per call.
        # A paginator automatically makes repeated calls behind the
        # scenes so we can loop over ALL objects, even in huge buckets.
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket_name):
            # A bucket with no objects yields a page with no "Contents" key.
            for obj in page.get("Contents", []):
                object_count += 1
                total_bytes += obj["Size"]
                # S3/MinIO don't expose true "last accessed" time via this
                # API, only "last modified". We use the most recent
                # modification in the bucket as a proxy for how "warm"
                # the bucket is.
                if last_modified is None or obj["LastModified"] > last_modified:
                    last_modified = obj["LastModified"]

        buckets.append(
            {
                "name": bucket_name,
                "object_count": object_count,
                "total_bytes": total_bytes,
                "last_modified": last_modified,
            }
        )

    return buckets


def summarize_buckets(buckets):
    """
    Aggregate a list of bucket dicts (the shape get_live_buckets() and
    get_demo_buckets() both return) into totals across all buckets.

    Pulled out as its own function so print_report()'s grand-total
    section and anything else that needs a summary (e.g. mcp_server.py)
    share one implementation instead of each summing buckets themselves.
    """
    return {
        "total_bytes": sum(b["total_bytes"] for b in buckets),
        "total_objects": sum(b["object_count"] for b in buckets),
    }


def record_snapshot(buckets, mode, path=SNAPSHOT_LOG_PATH):
    """
    Append one CSV row per bucket to the snapshot log, capturing object
    count and total size right now. Writes the header only if the file
    doesn't exist yet, so repeated calls (e.g. a daily cron job) build up
    one growing history file rather than overwriting it.

    Returns the rows written, so callers (e.g. the MCP tool) can report
    back what was recorded without re-reading the file.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    file_exists = os.path.exists(path)

    rows = [
        {
            "timestamp_utc": timestamp,
            "mode": mode,
            "bucket_name": bucket["name"],
            "object_count": bucket["object_count"],
            "total_bytes": bucket["total_bytes"],
        }
        for bucket in buckets
    ]

    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SNAPSHOT_CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)

    return rows


def read_snapshot_history(path=SNAPSHOT_LOG_PATH, mode=None):
    """
    Read the snapshot log back into memory, parsing timestamps/numbers
    so the result has the exact same shape get_demo_history() returns --
    a list of {timestamp, bucket_name, object_count, total_bytes} dicts.
    That shared shape is what lets forecast_growth() run identically on
    real history or synthetic demo history.

    Args:
        mode: If given, only return rows recorded under this mode
            ("live" or "demo") -- useful since a single log file could
            in principle contain snapshots from both, if someone ran
            `--snapshot` with and without `--demo`.

    Returns an empty list if the log file doesn't exist yet (e.g. no
    snapshots have been recorded).
    """
    if not os.path.exists(path):
        return []

    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if mode is not None and row["mode"] != mode:
                continue
            rows.append(
                {
                    "timestamp": datetime.fromisoformat(row["timestamp_utc"]),
                    "bucket_name": row["bucket_name"],
                    "object_count": int(row["object_count"]),
                    "total_bytes": int(row["total_bytes"]),
                }
            )
    return rows


def fit_linear_trend(points):
    """
    Fit a straight line y = slope*x + intercept through (x, y) points
    using ordinary least squares -- the standard "line of best fit".

    The closed-form solution (no iteration, no library needed) is:

        slope     = (n*sum(xy) - sum(x)*sum(y)) / (n*sum(x^2) - sum(x)^2)
        intercept = (sum(y) - slope*sum(x)) / n

    Intuition: `slope` is the average rate of change of y per unit of x
    (here: bytes grown per day) that best explains the points overall;
    `intercept` is where that line would cross x=0. Once you have both,
    you can read the line's value at ANY x -- including a future one --
    to get a projection.

    Requires at least 2 points (a line needs two points to be defined).
    If every point shares the same x (can't happen with real timestamps,
    but guarded anyway), treat the trend as flat at the average y.
    """
    n = len(points)
    if n < 2:
        raise ValueError("fit_linear_trend() needs at least 2 points.")

    sum_x = sum(x for x, _ in points)
    sum_y = sum(y for _, y in points)
    sum_xy = sum(x * y for x, y in points)
    sum_x2 = sum(x * x for x, _ in points)

    denominator = n * sum_x2 - sum_x**2
    if denominator == 0:
        return 0.0, sum_y / n

    slope = (n * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


def forecast_bucket_growth(bucket_name, rows, months_ahead):
    """
    Fit a linear trend to one bucket's historical (timestamp, total_bytes)
    snapshots and project its size and cost `months_ahead` months into
    the future.

    Uses fit_linear_trend() for the math (x = days since the bucket's
    earliest snapshot, y = total_bytes), then reads the fitted line's
    value at a future x to get the projected size. Cost math reuses
    estimate_monthly_cost()/PRICING_PER_GB_MONTH -- nothing about
    pricing is recomputed here.

    Returns None if there are fewer than 2 snapshots for this bucket --
    you can't fit a line through a single point, so there's no trend to
    project.
    """
    points = sorted(rows, key=lambda r: r["timestamp"])
    if len(points) < 2:
        return None

    epoch = points[0]["timestamp"]
    xy_points = [
        ((p["timestamp"] - epoch).total_seconds() / 86400, p["total_bytes"])
        for p in points
    ]
    slope, intercept = fit_linear_trend(xy_points)

    latest_x, latest_bytes = xy_points[-1]
    monthly_growth_bytes = slope * 30
    future_x = latest_x + months_ahead * 30
    # Clamp at 0 -- a bucket can't have negative bytes, even if a
    # shrinking trend's line technically crosses below zero.
    projected_bytes = max(round(slope * future_x + intercept), 0)

    current_cost = estimate_monthly_cost(latest_bytes, PRICING_PER_GB_MONTH[DEFAULT_TIER])
    projected_cost = estimate_monthly_cost(
        projected_bytes, PRICING_PER_GB_MONTH[DEFAULT_TIER]
    )

    return {
        "name": bucket_name,
        "data_points": len(points),
        "current_total_bytes": latest_bytes,
        "current_size_human": human_readable_size(latest_bytes),
        "current_monthly_cost_usd": round(current_cost, 2),
        "monthly_growth_bytes": round(monthly_growth_bytes),
        "monthly_growth_human": f"{human_readable_signed_size(monthly_growth_bytes)}/month",
        "projected_total_bytes": projected_bytes,
        "projected_size_human": human_readable_size(projected_bytes),
        "projected_monthly_cost_usd": round(projected_cost, 2),
    }


def forecast_growth(history_rows, months_ahead):
    """
    Project every bucket's storage size and cost forward by fitting a
    linear trend to its own historical snapshots (see
    forecast_bucket_growth). Buckets with fewer than 2 historical data
    points are skipped -- there's nothing to fit a trend to yet.

    Always includes an "assumption" string that must be surfaced
    alongside the numbers: this projects PAST linear growth forward
    unchanged, which is a simplifying assumption, not a guarantee.
    """
    buckets_history = {}
    for row in history_rows:
        buckets_history.setdefault(row["bucket_name"], []).append(row)

    bucket_forecasts = []
    for bucket_name, rows in buckets_history.items():
        forecast = forecast_bucket_growth(bucket_name, rows, months_ahead)
        if forecast is not None:
            bucket_forecasts.append(forecast)
    bucket_forecasts.sort(key=lambda f: f["name"])

    total_current_bytes = sum(f["current_total_bytes"] for f in bucket_forecasts)
    total_projected_bytes = sum(f["projected_total_bytes"] for f in bucket_forecasts)
    total_current_cost = sum(f["current_monthly_cost_usd"] for f in bucket_forecasts)
    total_projected_cost = sum(f["projected_monthly_cost_usd"] for f in bucket_forecasts)

    return {
        "months_ahead": months_ahead,
        "bucket_count": len(bucket_forecasts),
        "buckets": bucket_forecasts,
        "total_current_bytes": total_current_bytes,
        "total_current_size_human": human_readable_size(total_current_bytes),
        "total_current_monthly_cost_usd": round(total_current_cost, 2),
        "total_projected_bytes": total_projected_bytes,
        "total_projected_size_human": human_readable_size(total_projected_bytes),
        "total_projected_monthly_cost_usd": round(total_projected_cost, 2),
        "assumption": (
            f"Assumes each bucket keeps growing at its own current linear "
            f"rate for the next {months_ahead} month(s). Real growth can "
            f"speed up, slow down, or reverse due to business changes, "
            f"cleanup, or seasonality -- treat this as a directional "
            f"estimate, not a guarantee."
        ),
    }


def print_report(buckets, demo=False):
    """
    Print the full capacity/cost report for a list of buckets. This is
    the ONLY place cost math and formatting happen, and it's shared by
    both live and demo modes -- neither mode reimplements or duplicates
    any of it.
    """
    if demo:
        print("=== DEMO MODE (synthetic data) ===\n")

    if not buckets:
        print("No buckets found.")
        return

    print(f"Found {len(buckets)} bucket(s):\n")

    now = datetime.now(timezone.utc)

    for bucket in buckets:
        total_bytes = bucket["total_bytes"]

        print(f"Bucket: {bucket['name']}")
        print(f"  Objects: {bucket['object_count']}")
        print(f"  Total size: {human_readable_size(total_bytes)}")

        last_modified = bucket["last_modified"]
        if last_modified is not None:
            days_since_access = (now - last_modified).days
            print(f"  Last accessed: {days_since_access} day(s) ago")

            suggested_tier = suggest_tier(days_since_access)
            if suggested_tier is not None:
                print(f"  Suggested tier: {suggested_tier} (based on age)")

        # Cost at the default tier (S3 Standard) -- this is what the
        # bucket is presumed to actually cost today.
        standard_cost = estimate_monthly_cost(
            total_bytes, PRICING_PER_GB_MONTH[DEFAULT_TIER]
        )
        print(f"  Estimated monthly cost ({DEFAULT_TIER}): {format_money(standard_cost)}")

        # Cost at every other tier -- lets someone see, at a glance, how
        # much they'd save by moving this bucket's data to colder storage.
        print("  Cost if stored at other tiers:")
        for tier_name, price_per_gb in PRICING_PER_GB_MONTH.items():
            if tier_name == DEFAULT_TIER:
                continue
            tier_cost = estimate_monthly_cost(total_bytes, price_per_gb)
            print(f"    {tier_name}: {format_money(tier_cost)}")

        print()

    # --- Grand total across all buckets ---------------------------------
    grand_total_bytes = summarize_buckets(buckets)["total_bytes"]

    print("=" * 40)
    print("Grand total (all buckets)")
    print(f"  Total size: {human_readable_size(grand_total_bytes)}")

    grand_standard_cost = estimate_monthly_cost(
        grand_total_bytes, PRICING_PER_GB_MONTH[DEFAULT_TIER]
    )
    print(f"  Estimated monthly cost ({DEFAULT_TIER}): {format_money(grand_standard_cost)}")

    print("  Cost if stored at other tiers:")
    for tier_name, price_per_gb in PRICING_PER_GB_MONTH.items():
        if tier_name == DEFAULT_TIER:
            continue
        tier_cost = estimate_monthly_cost(grand_total_bytes, price_per_gb)
        print(f"    {tier_name}: {format_money(tier_cost)}")


def main():
    parser = argparse.ArgumentParser(
        description="Report per-bucket capacity and estimated storage cost."
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Use synthetic demo data instead of connecting to MinIO.",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help=(
            "Append a snapshot of current bucket sizes to the snapshot "
            "log (for tracking growth over time) instead of printing the "
            "full report. Intended for a periodic cron job."
        ),
    )
    args = parser.parse_args()

    if args.demo:
        buckets = get_demo_buckets()
    else:
        # boto3.client("s3", ...) creates an object we can use to make S3
        # API calls. Passing endpoint_url points it at our local MinIO
        # server instead of the real AWS S3 service.
        s3 = boto3.client(
            "s3",
            endpoint_url=MINIO_ENDPOINT,
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=SECRET_KEY,
        )
        buckets = get_live_buckets(s3)

    if args.snapshot:
        mode = "demo" if args.demo else "live"
        rows = record_snapshot(buckets, mode=mode)
        print(f"Recorded snapshot for {len(rows)} bucket(s) ({mode} mode) to {SNAPSHOT_LOG_PATH}")
    else:
        print_report(buckets, demo=args.demo)


# This check means main() only runs when the script is executed directly
# (e.g. `python capacity_report.py`), not if it were imported elsewhere.
if __name__ == "__main__":
    main()
