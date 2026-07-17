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

    print_report(buckets, demo=args.demo)


# This check means main() only runs when the script is executed directly
# (e.g. `python capacity_report.py`), not if it were imported elsewhere.
if __name__ == "__main__":
    main()
