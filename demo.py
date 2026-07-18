"""
demo.py

A terminal-friendly walkthrough of storage_insights' headline
capabilities, meant to be recorded into the README's demo GIF via
`vhs demo.tape` (see that file). Calls the exact same functions
mcp_server.py exposes as MCP tools -- nothing here is reimplemented,
this script only formats their output for a terminal audience.
"""

import textwrap

from mcp_server import find_savings, forecast_growth, get_storage_summary


def print_header(text):
    print(f"\n{text}")
    print("-" * len(text))


def main():
    print("=== storage_insights demo (synthetic data) ===")

    print_header("How much is storage costing me right now?")
    summary = get_storage_summary(demo=True)
    print(f"  {summary['bucket_count']} buckets, {summary['total_size_human']} total")
    print(f"  Estimated monthly cost: {summary['estimated_monthly_cost_display']}")

    print_header("Where am I wasting money?")
    savings = find_savings(demo=True)
    for opp in savings["opportunities"]:
        print(
            f"  {opp['name']:<24} idle {opp['days_since_last_access']:>3}d -> "
            f"{opp['suggested_tier']:<30} save ${opp['monthly_savings_usd']:>7.2f}/mo"
        )
    print(f"  Total potential savings: {savings['total_monthly_savings_display']}/mo")

    print_header("What will my storage look like in 6 months?")
    forecast = forecast_growth(months=6, demo=True)
    print(f"  Total size:    {forecast['total_current_size_human']} -> {forecast['total_projected_size_human']}")
    print(
        f"  Monthly cost:  ${forecast['total_current_monthly_cost_usd']:.2f} -> "
        f"${forecast['total_projected_monthly_cost_usd']:.2f}"
    )
    note = textwrap.fill(
        forecast["assumption"],
        width=76,
        initial_indent="  Note: ",
        subsequent_indent="        ",
    )
    print(note)


if __name__ == "__main__":
    main()
