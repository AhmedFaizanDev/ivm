"""Hybrid incremental-vs-recompute cost model — EXPERIMENTAL (Milestone 3).

The Enzyme idea carried into the embeddable setting: incremental maintenance is
not always cheaper. When a refresh touches a large fraction of the data, a full
recompute wins, so per refresh we estimate both costs and take the cheaper path.

Research caveat (from the build plan): no public source documents how production
systems actually make this decision — it survived zero verification. So this is
a deliberately simple HEURISTIC experiment, not a validated recipe. The estimate
here is a crude size ratio: if a refresh's total change is at least a threshold
fraction of the current base size, recompute; otherwise apply deltas.

What is NOT negotiable is correctness: whichever path runs, the maintained view
must equal a from-scratch recompute. Recompute rebuilds the operator graph over
the current contents — that both yields the from-scratch answer AND leaves
operator state consistent, so a later incremental refresh is still correct. This
wrapper is kept OUTSIDE the validated incremental engine on purpose: an
unverified experiment should not destabilize the core."""

from ivm.zset import ZSet
from ivm.engine import Engine


def _cardinality(zset):
    """Total row-instances (multiplicity), the cost proxy for a Z-set."""
    return sum(abs(w) for _row, w in zset.items())


class HybridView:
    def __init__(self, plan, recompute_threshold=0.5):
        self._plan = plan
        self._threshold = recompute_threshold
        self._tables = {}  # table -> ZSet: the authoritative current contents
        self._build()
        self.last_strategy = None

    def _build(self):
        self._engine = Engine()
        self._view = self._engine.add_view("v", self._plan)

    def refresh(self, batch):
        """Apply a batch {table: delta ZSet}, choosing incremental vs recompute
        by estimated cost, and return the maintained result."""
        change = sum(_cardinality(d) for d in batch.values())
        base = sum(_cardinality(z) for z in self._tables.values())
        for table, d in batch.items():
            self._tables[table] = self._tables.get(table, ZSet()) + d

        if change >= self._threshold * base:
            self._recompute()
            self.last_strategy = "recompute"
        else:
            for table, d in batch.items():
                self._engine.apply(table, d)
            self.last_strategy = "incremental"
        return self.result()

    def _recompute(self):
        # Discard stale operator state and rebuild from current contents. Feeding
        # each table's full contents through the operators from empty IS the
        # from-scratch computation, and leaves the graph consistent for the next
        # incremental refresh.
        self._build()
        for table, z in self._tables.items():
            if _cardinality(z):
                self._engine.apply(table, z)

    def result(self):
        return self._view.result()
