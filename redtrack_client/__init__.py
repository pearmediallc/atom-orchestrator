"""RedTrack API client — bulk fetcher for domain spend/revenue.

The lifecycle classifier calls `get_domain_spend_revenue_30d()` once per
cron run. That joins `/landings` + `/report?group=landing` in memory and
returns a `{host: {cost, revenue, ...}}` dict the classifier can lookup
each of our domains against.

See client.py for the join logic and the stub fallback used in tests."""

from redtrack_client.client import (  # noqa: F401
    add_tracker_domain,
    get_domain_spend_revenue_30d,
    extract_host,
)
