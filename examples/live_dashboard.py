"""Live dashboard demo for ivm.

    python examples/live_dashboard.py

A stream of random sales events (inserts and retractions) flows through a SQL
view that is maintained INCREMENTALLY — only the deltas are applied, the base
table is never re-scanned. The dashboard is redrawn on every event, and at the
end the maintained numbers are proven equal to a from-scratch recompute.

Env knobs: IVM_DEMO_EVENTS (default 60), IVM_DEMO_DELAY seconds (default 0.08).
"""

import os
import random
import sys
import time

from ivm import Engine

REGIONS = ["west", "east", "north", "south"]
PRODUCTS = ["widget", "gadget", "gizmo"]

DASHBOARD_SQL = (
    "SELECT region, COUNT(*) AS orders, SUM(amount) AS revenue, MAX(amount) AS biggest "
    "FROM sales GROUP BY region"
)
CATALOG = {"sales": ("order_id", "region", "product", "amount")}


def expected(live):
    """Independent from-scratch recompute of the dashboard, for the correctness
    proof. Deliberately dumb: plain Python aggregation over the live rows."""
    groups = {}
    for _oid, region, _product, amount in live.values():
        orders, revenue, biggest = groups.get(region, (0, 0, None))
        groups[region] = (orders + 1, revenue + amount,
                          amount if biggest is None else max(biggest, amount))
    return {(region, o, rev, big): 1 for region, (o, rev, big) in groups.items()}


def simulate(events=60, seed=7, on_tick=None):
    """Run the event stream through the maintained view. Returns (view, live).
    `on_tick(view, event_no)` is called after each applied event (for rendering);
    tests pass None to run headless."""
    rng = random.Random(seed)
    eng = Engine()
    view = eng.add_sql_view("dashboard", DASHBOARD_SQL, CATALOG)
    live = {}  # order_id -> row
    next_id = 0

    for event_no in range(1, events + 1):
        if live and rng.random() < 0.25:  # retract a past sale
            row = live.pop(rng.choice(list(live)))
            eng.delete("sales", row)
        else:  # a new sale
            row = (next_id, rng.choice(REGIONS), rng.choice(PRODUCTS), rng.randint(5, 500))
            next_id += 1
            live[row[0]] = row
            eng.insert("sales", row)
        if on_tick is not None:
            on_tick(view, event_no)

    return view, live


def render(view, event_no):
    rows = sorted(view.result(), key=lambda r: -r[2])  # by revenue, descending
    out = [f"  live sales dashboard        event #{event_no}", ""]
    out.append(f"  {'region':<8}{'orders':>8}{'revenue':>10}{'biggest':>9}")
    out.append("  " + "-" * 34)
    for region, orders, revenue, biggest in rows:
        out.append(f"  {region:<8}{orders:>8}{revenue:>10}{biggest:>9}")
    if not rows:
        out.append("  (no live sales)")
    sys.stdout.write("\033[2J\033[H" + "\n".join(out) + "\n")
    sys.stdout.flush()


def main():
    events = int(os.environ.get("IVM_DEMO_EVENTS", "60"))
    delay = float(os.environ.get("IVM_DEMO_DELAY", "0.08"))

    def tick(view, event_no):
        render(view, event_no)
        time.sleep(delay)

    view, live = simulate(events=events, seed=7, on_tick=tick)

    assert view.result() == expected(live), "incremental view diverged from recompute!"
    print(f"\n  OK: the incrementally maintained view exactly equals a from-scratch "
          f"recompute\n      ({len(live)} live sales after {events} events, applied as deltas).")


if __name__ == "__main__":
    main()
