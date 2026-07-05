"""Hand-wired incremental view for Milestone 0:
    SELECT category, COUNT(*), SUM(amount) FROM t GROUP BY category
Consumes deltas only — it never sees or rescans the base table.

COUNT and SUM are linear (DBSP Theorem 3.3): the per-group accumulators
are updated by the delta alone. A group vanishes when its COUNT reaches
zero — not when its SUM does (sums can legitimately be zero)."""


class GroupCountSumView:
    def __init__(self):
        self._groups = {}  # category -> [count, total]

    def apply(self, delta):
        for (category, amount), weight in delta.items():
            acc = self._groups.get(category)
            if acc is None:
                acc = self._groups[category] = [0, 0]
            acc[0] += weight
            acc[1] += amount * weight
            if acc[0] == 0:
                del self._groups[category]

    def result(self):
        return {cat: (count, total) for cat, (count, total) in self._groups.items()}
