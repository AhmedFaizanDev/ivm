"""The engine ties base tables to views. It owns one shared SourceOp per table,
so many views over the same tables all receive each delta. A View materializes
its root operator's output by accumulating emitted deltas into a Z-set."""

from ivm.zset import ZSet
from ivm.operators import compile_plan, SourceOp


class View:
    def __init__(self, name, schema):
        self.name = name
        self.schema = schema
        self._state = ZSet()

    def _absorb(self, delta):
        self._state = self._state + delta

    def result(self):
        """The maintained view as {result_row: weight}."""
        return dict(self._state.items())


class Engine:
    def __init__(self):
        self._sources = {}  # table name -> SourceOp (shared across views)
        self._views = {}

    def _get_source(self, table, schema):
        src = self._sources.get(table)
        if src is None:
            src = self._sources[table] = SourceOp(schema)
        elif src.schema != tuple(schema):
            raise ValueError(
                f"table {table!r} used with conflicting schemas: "
                f"{src.schema} vs {tuple(schema)}"
            )
        return src

    def add_view(self, name, plan):
        root, schema = compile_plan(plan, self)
        view = View(name, schema)
        root.subscribe(view._absorb)
        self._views[name] = view
        return view

    def apply(self, table, delta):
        """Feed a base-table delta; it fans out to every view reading `table`."""
        src = self._sources.get(table)
        if src is not None:
            src.push(delta)

    def insert(self, table, row):
        self.apply(table, ZSet({row: +1}))

    def delete(self, table, row):
        self.apply(table, ZSet({row: -1}))
