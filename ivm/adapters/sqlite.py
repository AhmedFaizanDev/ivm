"""SQLite change capture — the single biggest correctness risk in the plan.

Do NOT build on `sqlite3_update_hook`: it carries no column values and silently
misses WITHOUT ROWID tables, ON CONFLICT REPLACE deletes, and truncate-optimized
`DELETE`. This adapter uses row-level mechanisms that see every change with its
values:

  * capture="trigger" (portable, stdlib): AFTER INSERT/UPDATE/DELETE triggers
    record the full OLD/NEW rows into a per-table changelog. A row trigger
    disables the truncate optimization, and `PRAGMA recursive_triggers=ON` makes
    REPLACE fire its delete trigger — so all three blind spots are covered. The
    changelog stores column values NATIVELY (BLOB-affinity columns, no JSON): a
    REAL keeps its exact double (no float drift) and a BLOB round-trips, so
    captured deltas cancel bit-for-bit against the rows `contents()` reads back.
  * capture="session" (primary, apsw): the SQLite session extension's row-level
    changesets, with native values.

Caveat (choose knowingly): triggers capture writes from ANY connection to the
database; the session backend only sees writes on its OWN connection.

The database is the source of truth: `contents()` reads it back with `SELECT *`,
so the recompute oracle runs over the real table, not a shadow copy."""

from ivm.zset import ZSet


class SqliteAdapter:
    def __init__(self, engine, connection, capture="trigger"):
        if capture not in ("trigger", "session"):
            raise ValueError(f"unknown capture mode {capture!r}")
        self._engine = engine
        self._conn = connection
        self._capture = capture
        self._tables = {}  # table -> {"schema": (...), "pk": (...)}
        if capture == "trigger":
            self._setup_trigger_store()
        else:
            self._setup_session()

    # --- connection helpers (work on both stdlib sqlite3 and apsw) ------------

    def _exec(self, sql, params=None):
        cur = self._conn.cursor()
        cur.execute(sql) if params is None else cur.execute(sql, params)

    def _query(self, sql, params=None):
        cur = self._conn.cursor()
        cur.execute(sql) if params is None else cur.execute(sql, params)
        return [tuple(r) for r in cur]

    # --- registration ---------------------------------------------------------

    def register(self, table, schema, primary_key):
        schema = tuple(schema)
        self._tables[table] = {"schema": schema, "pk": tuple(primary_key)}
        if self._capture == "trigger":
            self._install_triggers(table, schema)
        else:
            self._attach_session(table)

    # --- contents (the oracle reads the real table through here) --------------

    def contents(self, table):
        schema = self._tables[table]["schema"]
        cols = ", ".join(f'"{c}"' for c in schema)
        rows = self._query(f'SELECT {cols} FROM "{table}"')
        return ZSet({row: +1 for row in rows})

    def all_contents(self):
        return {table: self.contents(table) for table in self._tables}

    # --- trigger backend ------------------------------------------------------

    def _setup_trigger_store(self):
        # REPLACE-induced deletes only fire delete triggers with this on. The
        # per-table changelogs themselves are created in register().
        self._exec("PRAGMA recursive_triggers = ON")

    @staticmethod
    def _changelog_name(table):
        return f"_ivm_log_{table}"

    def _install_triggers(self, table, schema):
        log = self._changelog_name(table)
        n = len(schema)
        old_cols = [f"o{i}" for i in range(n)]
        new_cols = [f"n{i}" for i in range(n)]
        # Value columns are declared with NO type -> BLOB affinity, so SQLite
        # stores each value in its original storage class without coercion:
        # a REAL stays an exact double, a BLOB stays bytes. No JSON anywhere.
        val_defs = ", ".join(f'"{c}"' for c in old_cols + new_cols)
        self._exec(
            f'CREATE TABLE IF NOT EXISTS "{log}"('
            f"seq INTEGER PRIMARY KEY AUTOINCREMENT, op TEXT NOT NULL, {val_defs})"
        )
        new_src = ", ".join(f'NEW."{c}"' for c in schema)
        old_src = ", ".join(f'OLD."{c}"' for c in schema)
        new_tgt = ", ".join(f'"{c}"' for c in new_cols)
        old_tgt = ", ".join(f'"{c}"' for c in old_cols)
        self._exec(
            f'CREATE TRIGGER IF NOT EXISTS "_ivm_{table}_ins" AFTER INSERT ON "{table}" BEGIN '
            f'INSERT INTO "{log}"(op, {new_tgt}) VALUES(\'I\', {new_src}); END'
        )
        self._exec(
            f'CREATE TRIGGER IF NOT EXISTS "_ivm_{table}_del" AFTER DELETE ON "{table}" BEGIN '
            f'INSERT INTO "{log}"(op, {old_tgt}) VALUES(\'D\', {old_src}); END'
        )
        self._exec(
            f'CREATE TRIGGER IF NOT EXISTS "_ivm_{table}_upd" AFTER UPDATE ON "{table}" BEGIN '
            f'INSERT INTO "{log}"(op, {old_tgt}, {new_tgt}) VALUES(\'U\', {old_src}, {new_src}); END'
        )

    def _flush_trigger(self):
        for table, meta in self._tables.items():
            self._drain_changelog(table, len(meta["schema"]))

    def _drain_changelog(self, table, n):
        log = self._changelog_name(table)
        old_cols = ", ".join(f'"o{i}"' for i in range(n))
        new_cols = ", ".join(f'"n{i}"' for i in range(n))
        rows = self._query(
            f'SELECT seq, op, {old_cols}, {new_cols} FROM "{log}" ORDER BY seq'
        )
        if not rows:
            return
        delta = {}  # row -> weight
        for r in rows:
            op = r[1]
            old_row = tuple(r[2 : 2 + n])
            new_row = tuple(r[2 + n : 2 + 2 * n])
            if op == "I":
                delta[new_row] = delta.get(new_row, 0) + 1
            elif op == "D":
                delta[old_row] = delta.get(old_row, 0) - 1
            else:  # "U" -> delete-old + insert-new
                delta[old_row] = delta.get(old_row, 0) - 1
                delta[new_row] = delta.get(new_row, 0) + 1
        self._exec(f'DELETE FROM "{log}" WHERE seq <= ?', (rows[-1][0],))
        self._engine.apply(table, ZSet(delta))

    # --- session backend (apsw) -----------------------------------------------

    def _setup_session(self):
        import apsw

        self._apsw = apsw
        self._session = apsw.Session(self._conn, "main")

    def _attach_session(self, table):
        self._session.attach(table)

    def _reset_session(self):
        # A changeset is cumulative from session start, so after draining one we
        # must start a fresh session or the next flush would replay everything.
        old = self._session
        self._session = self._apsw.Session(self._conn, "main")
        for tbl in self._tables:
            self._session.attach(tbl)
        try:
            old.close()
        except Exception:
            pass

    def _flush_session(self):
        changeset = self._session.changeset()
        self._reset_session()
        if not changeset:
            return
        deltas = {}  # table -> {row: weight}
        for ch in self._apsw.Changeset.iter(changeset):
            tbl = ch.name
            if tbl not in self._tables:
                continue
            d = deltas.setdefault(tbl, {})
            if ch.op == "INSERT":
                row = tuple(ch.new)
                d[row] = d.get(row, 0) + 1
            elif ch.op == "DELETE":
                row = tuple(ch.old)
                d[row] = d.get(row, 0) - 1
            else:  # UPDATE: unchanged columns are apsw.no_change -> reconstruct
                old, new = self._reconstruct_update(tbl, ch)
                d[old] = d.get(old, 0) - 1
                d[new] = d.get(new, 0) + 1
        for tbl, d in deltas.items():
            self._engine.apply(tbl, ZSet(d))

    def _reconstruct_update(self, tbl, ch):
        """Rebuild full old/new rows for an UPDATE change. The current DB row (by
        PK) gives every column's new value (unchanged columns included); the old
        row is that, with the changeset's supplied old values laid over the
        columns that actually changed. PK changes never reach here — the session
        emits those as DELETE + INSERT."""
        schema = self._tables[tbl]["schema"]
        pk_cols = self._tables[tbl]["pk"]
        no_change = self._apsw.no_change
        old_change = list(ch.old)
        pk_idx = [schema.index(c) for c in pk_cols]
        pk_vals = tuple(old_change[i] for i in pk_idx)
        current = self._select_by_pk(tbl, pk_cols, pk_vals)  # full new row
        full_old = list(current)
        for i, val in enumerate(old_change):
            if val is not no_change:  # PK cols and changed cols carry real values
                full_old[i] = val
        return tuple(full_old), tuple(current)

    def _select_by_pk(self, tbl, pk_cols, pk_vals):
        schema = self._tables[tbl]["schema"]
        cols = ", ".join(f'"{c}"' for c in schema)
        where = " AND ".join(f'"{c}" = ?' for c in pk_cols)
        rows = self._query(f'SELECT {cols} FROM "{tbl}" WHERE {where}', pk_vals)
        return rows[0]

    # --- flush: capture pending changes and feed the engine -------------------

    def flush(self):
        """Drain captured changes since the last flush, as typed deltas, into the
        engine. The app just mutates SQL and calls this — no deltas by hand."""
        if self._capture == "trigger":
            self._flush_trigger()
        else:
            self._flush_session()
