"""Named-column access over a plain value-tuple row.

Rows stay bare hashable tuples (the Z-set kernel never changes). A stream's
schema (ordered column names) lives on the operator, not the row; `Row` binds a
value-tuple to that schema so plan predicates/expressions read columns by name
(`r["amount"]`) while the resolution to a tuple index is O(1)."""


def index_of(schema):
    """Map column name -> position, computed once per operator (schemas are static)."""
    return {name: i for i, name in enumerate(schema)}


class Row:
    __slots__ = ("_values", "_index")

    def __init__(self, values, index):
        self._values = values
        self._index = index

    def __getitem__(self, name):
        return self._values[self._index[name]]
