"""Z-set: a multiset with integer weights. Weight +1 = insert, -1 = delete.
A row whose weight reaches 0 is absent. This is the DBSP primitive."""


class ZSet:
    def __init__(self, entries=None):
        self._w = {}
        for row, weight in (entries or {}).items():
            if weight != 0:
                self._w[row] = weight

    def __add__(self, other):
        merged = dict(self._w)
        for row, weight in other.items():
            new = merged.get(row, 0) + weight
            if new == 0:
                merged.pop(row, None)
            else:
                merged[row] = new
        return ZSet(merged)

    def items(self):
        return self._w.items()
