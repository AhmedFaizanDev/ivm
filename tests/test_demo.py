"""Keep the live-dashboard demo honest: its incrementally maintained view must
equal a from-scratch recompute, headless (no rendering / no sleep), across seeds.
"""

import importlib.util
import pathlib

import pytest

_DEMO = pathlib.Path(__file__).resolve().parent.parent / "examples" / "live_dashboard.py"


def _load():
    spec = importlib.util.spec_from_file_location("live_dashboard", _DEMO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("seed", range(5))
def test_demo_view_equals_recompute(seed):
    demo = _load()
    view, live = demo.simulate(events=80, seed=seed, on_tick=None)
    assert view.result() == demo.expected(live)
