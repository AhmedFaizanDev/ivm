"""In-process mutation log: the easiest change feed. The application owns its
writes and routes them through this thin PK-keyed API instead of touching the
engine directly. Each write becomes a Z-set delta; UPDATE is delete-old +
insert-new. Pure stdlib, no database.

The log holds the authoritative table contents (a PK -> row map per table), so
it is both the write path AND the source of truth the oracle recomputes over."""

from ivm.zset import ZSet


class MutationLog:
    def __init__(self, engine):
        self._engine = engine
        self._tables = {}  # table -> {"schema", "pk_idx", "rows": {pk: row}}

    def register(self, table, schema, primary_key):
        schema = tuple(schema)
        idx = {name: i for i, name in enumerate(schema)}
        pk_idx = tuple(idx[c] for c in primary_key)
        self._tables[table] = {"schema": schema, "pk_idx": pk_idx, "rows": {}}

    def _pk(self, table, row):
        return tuple(row[i] for i in self._tables[table]["pk_idx"])

    def insert(self, table, row):
        row = tuple(row)
        rows = self._tables[table]["rows"]
        pk = self._pk(table, row)
        if pk in rows:
            raise KeyError(f"duplicate primary key {pk} in {table!r}")
        rows[pk] = row
        self._engine.apply(table, ZSet({row: +1}))

    def delete(self, table, key):
        """Delete by primary key (a tuple of the PK column values)."""
        rows = self._tables[table]["rows"]
        row = rows.pop(tuple(key))  # KeyError if the key is absent
        self._engine.apply(table, ZSet({row: -1}))

    def update(self, table, new_row):
        """Replace the row with this new_row's primary key. PK is assumed stable;
        changing a PK is a delete of the old key plus an insert of the new."""
        new_row = tuple(new_row)
        rows = self._tables[table]["rows"]
        pk = self._pk(table, new_row)
        old = rows[pk]  # KeyError if the row does not exist
        if old == new_row:
            return
        rows[pk] = new_row
        self._engine.apply(table, ZSet({old: -1, new_row: +1}))

    def contents(self, table):
        """The table's current rows as a Z-set (each present once) — the oracle
        recomputes over this."""
        return ZSet({row: +1 for row in self._tables[table]["rows"].values()})

    def all_contents(self):
        return {table: self.contents(table) for table in self._tables}
