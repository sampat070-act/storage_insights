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


def main():
    # boto3.client("s3", ...) creates an object we can use to make S3 API
    # calls. Passing endpoint_url points it at our local MinIO server
    # instead of the real AWS S3 service.
    s3 = boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
    )

    # Ask MinIO for the list of all buckets.
    response = s3.list_buckets()
    buckets = response["Buckets"]

    if not buckets:
        print("No buckets found.")
        return

    print(f"Found {len(buckets)} bucket(s):\n")

    # Running total across all buckets, so we can print a grand-total
    # summary (capacity + cost) after the per-bucket loop finishes.
    grand_total_bytes = 0

    for bucket in buckets:
        bucket_name = bucket["Name"]

        object_count = 0
        total_bytes = 0

        # list_objects_v2 only returns up to 1000 objects per call.
        # A paginator automatically makes repeated calls behind the
        # scenes so we can loop over ALL objects, even in huge buckets.
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket_name):
            # A bucket with no objects yields a page with no "Contents" key.
            for obj in page.get("Contents", []):
                object_count += 1
                total_bytes += obj["Size"]

        grand_total_bytes += total_bytes

        print(f"Bucket: {bucket_name}")
        print(f"  Objects: {object_count}")
        print(f"  Total size: {human_readable_size(total_bytes)}")

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


# This check means main() only runs when the script is executed directly
# (e.g. `python capacity_report.py`), not if it were imported elsewhere.
if __name__ == "__main__":
    main()
