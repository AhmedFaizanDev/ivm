"""Smoke test for the benchmark harness: it must run and produce sane numbers,
including the core claim that incremental beats full recompute for a tiny batch.
Tiny sizes keep it fast; the real numbers come from `python bench/benchmark.py`.
"""

import importlib.util
import pathlib

_BENCH = pathlib.Path(__file__).resolve().parent.parent / "bench" / "benchmark.py"


def _load():
    spec = importlib.util.spec_from_file_location("benchmark", _BENCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_benchmark_metrics_are_sane():
    b = _load()
    assert b.measure_throughput(300)["deltas_per_sec"] > 0
    lat = b.measure_latency(200)
    assert lat["p99_us"] >= lat["p50_us"] > 0
    assert b.measure_state_size(300)["snapshot_bytes"] > 0
    assert b.measure_join_state_size(200)["snapshot_bytes"] > 0


def test_incremental_beats_recompute_for_tiny_batch():
    b = _load()
    curve = b.measure_speedup_curve(1000, [1, 500])
    assert curve[0]["batch"] == 1
    assert curve[0]["incremental_speedup"] > 1.0  # the whole premise of IVM


def test_join_curve_runs_and_cost_model_flips_on_bulk():
    b = _load()
    curve = b.measure_join_speedup_curve(2000, 100, [1, 1000])
    assert curve[0]["incremental_speedup"] > curve[-1]["incremental_speedup"]  # advantage shrinks with batch
