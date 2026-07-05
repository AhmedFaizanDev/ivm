"""The engine ties base tables to views. It owns one shared SourceOp per table,
so many views over the same tables all receive each delta. A View materializes
its root operator's output by accumulating emitted deltas into a Z-set."""

import copy

from ivm.zset import ZSet
from ivm.operators import compile_plan, SourceOp


class View:
    def __init__(self, name, schema):
        self.name = name
        self.schema = schema
        self._state = ZSet()
        self._ops = []  # stateful operators in this view's graph, in snapshot order

    def _absorb(self, delta):
        self._state = self._state + delta

    def result(self):
        """The maintained view as {result_row: weight}."""
        return dict(self._state.items())

    def state(self):
        return dict(self._state.items())

    def load(self, snap):
        self._state = ZSet(snap)


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
        ops = []
        root, schema = compile_plan(plan, self, ops)
        view = View(name, schema)
        view._ops = ops
        root.subscribe(view._absorb)
        self._views[name] = view
        return view

    def snapshot(self):
        """A point-in-time, picklable snapshot of every view: each stateful
        operator's state (join indexes, aggregate accumulators + MIN/MAX
        multisets) and the view's materialized result. Persist it with stdlib
        `pickle`. Restore into an engine whose views were re-added with the SAME
        plans (the plans are code/config; only this state is data).

        Security note: a snapshot is arbitrary Python state — only `pickle.load`
        snapshots you trust, exactly as with any pickle-based persistence."""
        snap = {
            name: {"view": view.state(), "ops": [op.state() for op in view._ops]}
            for name, view in self._views.items()
        }
        return copy.deepcopy(snap)  # freeze at capture time, independent of later writes

    def restore(self, snap):
        """Inject a snapshot into views already re-added with the same plans."""
        for name, saved in snap.items():
            view = self._views.get(name)
            if view is None:
                raise KeyError(
                    f"snapshot references unknown view {name!r}; re-add it before restoring"
                )
            if len(saved["ops"]) != len(view._ops):
                raise ValueError(
                    f"view {name!r}: snapshot has {len(saved['ops'])} operator states but "
                    f"the rebuilt graph has {len(view._ops)} (different plan?)"
                )
            for op, op_state in zip(view._ops, saved["ops"]):
                op.load(copy.deepcopy(op_state))
            view.load(copy.deepcopy(saved["view"]))

    def add_sql_view(self, name, sql, catalog):
        """Compile a SELECT against a catalog {table: schema} and register the
        maintained view. Sugar for add_view(name, compile_sql(sql, catalog))."""
        from ivm.sql import compile_sql

        return self.add_view(name, compile_sql(sql, catalog))

    def apply(self, table, delta):
        """Feed a base-table delta; it fans out to every view reading `table`."""
        src = self._sources.get(table)
        if src is not None:
            src.push(delta)

    def insert(self, table, row):
        self.apply(table, ZSet({row: +1}))

    def delete(self, table, row):
        self.apply(table, ZSet({row: -1}))
