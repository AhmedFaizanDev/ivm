"""Reproducible benchmark / evaluation harness for ivm.

    python bench/benchmark.py            # full run, writes bench/results.json
    python bench/benchmark.py --quick    # smaller sizes, for a fast check

Measures, on a representative maintained view (GROUP BY region with
COUNT/SUM/AVG/MIN/MAX):

  * throughput      — single-row deltas applied per second
  * update latency  — per-delta time distribution (p50 / p95 / p99)
  * state size      — bytes to persist operator state (pickled snapshot) per row
  * speedup curve   — THE money graph: incremental refresh vs full from-scratch
                      recompute, as a function of batch size. Incremental wins for
                      small change sets; as a batch approaches the base size the
                      advantage collapses and a cost model should flip to recompute
                      (we report where the experimental cost model actually flips).

Stdlib only. Results are saved as JSON so the numbers are reproducible and
citable; a human summary is printed.
"""

import json
import pathlib
import pickle
import statistics
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ivm.engine import Engine
from ivm.zset import ZSet
from ivm.oracle import eval_plan
from ivm.cost_model import HybridView
from ivm.sql import compile_sql

SCHEMA = ("id", "region", "amount")
CATALOG = {"sales": SCHEMA}
SQL = ("SELECT region, COUNT(*) AS n, SUM(amount) AS total, AVG(amount) AS mean, "
       "MIN(amount) AS lo, MAX(amount) AS hi FROM sales GROUP BY region")
REGIONS = ("west", "east", "north", "south", "central")


def _row(i):
    # deterministic, ~5 groups, spread of amounts (ints -> AVG/SUM stay exact)
    return (i, REGIONS[i % len(REGIONS)], (i * 2654435761) % 1000)


def _warm_engine(n):
    eng = Engine()
    view = eng.add_view("v", compile_sql(SQL, CATALOG))
    for i in range(n):
        eng.apply("sales", ZSet({_row(i): +1}))
    return eng, view


def measure_throughput(n):
    eng = Engine()
    eng.add_view("v", compile_sql(SQL, CATALOG))
    rows = [_row(i) for i in range(n)]
    t0 = time.perf_counter()
    for r in rows:
        eng.apply("sales", ZSet({r: +1}))
    dt = time.perf_counter() - t0
    return {"rows": n, "seconds": dt, "deltas_per_sec": n / dt}


def measure_latency(n):
    eng, _ = _warm_engine(n)
    samples = []
    for i in range(n, n + 2000):  # 2000 timed single-row updates on a warm engine
        r = _row(i)
        t0 = time.perf_counter()
        eng.apply("sales", ZSet({r: +1}))
        samples.append((time.perf_counter() - t0) * 1e6)  # microseconds
    samples.sort()
    return {
        "warm_rows": n,
        "mean_us": statistics.mean(samples),
        "p50_us": samples[len(samples) // 2],
        "p95_us": samples[int(len(samples) * 0.95)],
        "p99_us": samples[int(len(samples) * 0.99)],
    }


def measure_state_size(n):
    """Aggregate view: state is O(distinct groups), so bytes/row shrinks with n."""
    eng, _ = _warm_engine(n)
    blob = pickle.dumps(eng.snapshot())
    return {"rows": n, "snapshot_bytes": len(blob), "bytes_per_row": len(blob) / n}


JOIN_SQL = ("SELECT orders.oid, orders.uid, orders.amount, users.uname "
            "FROM orders JOIN users ON orders.uid = users.uid")
JOIN_CATALOG = {"orders": ("oid", "uid", "amount"), "users": ("uid", "uname")}


def measure_join_state_size(n):
    """Join view: the operator retains BOTH inputs as indexes, so state is
    O(rows) — the honest per-record overhead that matters for the Rust port."""
    eng = Engine()
    eng.add_view("v", compile_sql(JOIN_SQL, JOIN_CATALOG))
    for u in range(n):
        eng.apply("users", ZSet({(u, f"u{u}"): +1}))
    for o in range(n):
        eng.apply("orders", ZSet({(o, o % n, o): +1}))
    blob = pickle.dumps(eng.snapshot())
    return {"input_rows": 2 * n, "snapshot_bytes": len(blob),
            "bytes_per_input_row": len(blob) / (2 * n)}


def measure_speedup_curve(base_n, batch_sizes):
    base_rows = [_row(i) for i in range(base_n)]
    base_table = ZSet({r: +1 for r in base_rows})
    plan = compile_sql(SQL, CATALOG)
    curve = []
    for b in batch_sizes:
        batch_rows = [_row(base_n + j) for j in range(b)]
        batch_delta = ZSet({r: +1 for r in batch_rows})

        # incremental: warm engine at base_n, then apply the batch (timed)
        eng, _ = _warm_engine(base_n)
        t0 = time.perf_counter()
        eng.apply("sales", batch_delta)
        incr = time.perf_counter() - t0

        # full recompute: evaluate the view from scratch over base + batch
        full_table = {"sales": base_table + batch_delta}
        t0 = time.perf_counter()
        eval_plan(plan, full_table)
        recompute = time.perf_counter() - t0

        # what the experimental cost model chooses for this refresh
        hv = HybridView(plan, recompute_threshold=0.5)
        hv.refresh({"sales": base_table})
        hv.refresh({"sales": batch_delta})
        strategy = hv.last_strategy

        curve.append({
            "batch": b,
            "batch_fraction_of_base": b / base_n,
            "incremental_ms": incr * 1e3,
            "recompute_ms": recompute * 1e3,
            "incremental_speedup": recompute / incr if incr > 0 else float("inf"),
            "cost_model_strategy": strategy,
        })
    return curve


def measure_join_speedup_curve(base_orders, n_uids, batch_sizes):
    """Join money graph. Base: n_uids users (one each) + base_orders orders spread
    over those uids, so each uid has ~base_orders/n_uids orders (the fan-out).
    Each batch inserts B new users at existing uids, so an incremental refresh does
    B * fan-out work (join output), while a full recompute is ~O(base). For large
    B, incremental's superlinear work loses — exactly the case a cost model exists
    to catch."""
    plan = compile_sql(JOIN_SQL, JOIN_CATALOG)
    base_users = ZSet({(u, f"u{u}"): +1 for u in range(n_uids)})
    base_orders_z = ZSet({(o, o % n_uids, o): +1 for o in range(base_orders)})
    curve = []
    for b in batch_sizes:
        batch = ZSet({(j % n_uids, f"new{j}"): +1 for j in range(b)})

        eng = Engine()
        eng.add_view("v", compile_sql(JOIN_SQL, JOIN_CATALOG))
        eng.apply("users", base_users)
        eng.apply("orders", base_orders_z)
        t0 = time.perf_counter()
        eng.apply("users", batch)
        incr = time.perf_counter() - t0

        tables = {"users": base_users + batch, "orders": base_orders_z}
        t0 = time.perf_counter()
        eval_plan(plan, tables)
        recompute = time.perf_counter() - t0

        hv = HybridView(plan, recompute_threshold=0.5)
        hv.refresh({"users": base_users, "orders": base_orders_z})
        hv.refresh({"users": batch})

        curve.append({
            "batch_users": b,
            "fan_out": base_orders // n_uids,
            "incremental_ms": incr * 1e3,
            "recompute_ms": recompute * 1e3,
            "incremental_speedup": recompute / incr if incr > 0 else float("inf"),
            "cost_model_strategy": hv.last_strategy,
        })
    return curve


def run_all(quick=False):
    n = 5000 if quick else 50000
    base_n = 5000 if quick else 50000
    batch_sizes = [1, 10, 100, 1000] if quick else [1, 10, 100, 1000, 10000, 50000]
    join_orders = 4000 if quick else 20000
    n_uids = 100 if quick else 200
    join_batches = [1, 10, 100, 1000] if quick else [1, 10, 100, 500, 2000]
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "aggregate_view_sql": SQL,
        "join_view_sql": JOIN_SQL,
        "config": {"n": n, "base_n": base_n, "batch_sizes": batch_sizes,
                   "join_orders": join_orders, "n_uids": n_uids, "join_batches": join_batches},
        "throughput": measure_throughput(n),
        "latency": measure_latency(n),
        "aggregate_state_size": measure_state_size(n),
        "join_state_size": measure_join_state_size(1000 if quick else 10000),
        "aggregate_speedup_curve": measure_speedup_curve(base_n, batch_sizes),
        "join_speedup_curve": measure_join_speedup_curve(join_orders, n_uids, join_batches),
    }


def _print_summary(res):
    tp = res["throughput"]
    lat = res["latency"]
    ss = res["aggregate_state_size"]
    js = res["join_state_size"]
    print("\n=== ivm benchmark ===")
    print(f"aggregate view: {res['aggregate_view_sql']}")
    print(f"throughput : {tp['deltas_per_sec']:>12,.0f} single-row deltas/sec "
          f"({tp['rows']:,} rows in {tp['seconds']:.2f}s)")
    print(f"latency    : p50 {lat['p50_us']:.1f}us  p95 {lat['p95_us']:.1f}us  "
          f"p99 {lat['p99_us']:.1f}us  (warm {lat['warm_rows']:,} rows)")
    print(f"state size : aggregate {ss['bytes_per_row']:.2f} bytes/row (O(groups)); "
          f"join {js['bytes_per_input_row']:.1f} bytes/input-row (O(rows))")

    print("\nAGGREGATE - incremental refresh vs full recompute (the money graph):")
    print(f"  {'batch':>8} {'frac':>7} {'incr(ms)':>10} {'recompute(ms)':>14} {'speedup':>9}  cost-model")
    for p in res["aggregate_speedup_curve"]:
        print(f"  {p['batch']:>8,} {p['batch_fraction_of_base']:>7.3f} "
              f"{p['incremental_ms']:>10.3f} {p['recompute_ms']:>14.3f} "
              f"{p['incremental_speedup']:>8.1f}x  {p['cost_model_strategy']}")

    print("\nJOIN (fan-out) - where recompute WINS on bulk updates and the cost model flips:")
    print(f"  {'batch':>8} {'incr(ms)':>10} {'recompute(ms)':>14} {'speedup':>9}  cost-model")
    for p in res["join_speedup_curve"]:
        print(f"  {p['batch_users']:>8,} {p['incremental_ms']:>10.3f} {p['recompute_ms']:>14.3f} "
              f"{p['incremental_speedup']:>8.2f}x  {p['cost_model_strategy']}")
    print()


def main():
    quick = "--quick" in sys.argv
    res = run_all(quick=quick)
    out = pathlib.Path(__file__).resolve().parent / "results.json"
    out.write_text(json.dumps(res, indent=2))
    _print_summary(res)
    print(f"results written to {out}")


if __name__ == "__main__":
    main()
