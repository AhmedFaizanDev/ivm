"""SQLite change capture — the single biggest correctness risk in the plan.

Do NOT build on `sqlite3_update_hook`: it carries no column values and silently
misses WITHOUT ROWID tables, ON CONFLICT REPLACE deletes, and truncate-optimized
`DELETE`. This adapter uses row-level mechanisms that see every change with its
values:

  * capture="trigger" (portable, stdlib): AFTER INSERT/UPDATE/DELETE triggers
    record the full OLD/NEW rows into a changelog table. A row trigger disables
    the truncate optimization, and `PRAGMA recursive_triggers=ON` makes REPLACE
    fire its delete trigger — so all three blind spots are covered.
  * capture="session" (primary, apsw): the SQLite session extension's row-level
    changesets. Built in the next task.

The database is the source of truth: `contents()` reads it back with `SELECT *`,
so the recompute oracle runs over the real table, not a shadow copy."""

import json

from ivm.zset import ZSet

CHANGELOG = "_ivm_changelog"


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
        # REPLACE-induced deletes only fire delete triggers with this on.
        self._exec("PRAGMA recursive_triggers = ON")
        self._exec(
            f"CREATE TABLE IF NOT EXISTS {CHANGELOG}("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
            "tbl TEXT NOT NULL, op TEXT NOT NULL, oldvals TEXT, newvals TEXT)"
        )

    def _install_triggers(self, table, schema):
        new = ", ".join(f'NEW."{c}"' for c in schema)
        old = ", ".join(f'OLD."{c}"' for c in schema)
        self._exec(
            f'CREATE TRIGGER IF NOT EXISTS "_ivm_{table}_ins" AFTER INSERT ON "{table}" BEGIN '
            f"INSERT INTO {CHANGELOG}(tbl, op, newvals) "
            f"VALUES('{table}', 'I', json_array({new})); END"
        )
        self._exec(
            f'CREATE TRIGGER IF NOT EXISTS "_ivm_{table}_del" AFTER DELETE ON "{table}" BEGIN '
            f"INSERT INTO {CHANGELOG}(tbl, op, oldvals) "
            f"VALUES('{table}', 'D', json_array({old})); END"
        )
        self._exec(
            f'CREATE TRIGGER IF NOT EXISTS "_ivm_{table}_upd" AFTER UPDATE ON "{table}" BEGIN '
            f"INSERT INTO {CHANGELOG}(tbl, op, oldvals, newvals) "
            f"VALUES('{table}', 'U', json_array({old}), json_array({new})); END"
        )

    def _flush_trigger(self):
        rows = self._query(
            f"SELECT seq, tbl, op, oldvals, newvals FROM {CHANGELOG} ORDER BY seq"
        )
        if not rows:
            return
        deltas = {}  # table -> {row: weight}
        for _seq, tbl, op, oldvals, newvals in rows:
            d = deltas.setdefault(tbl, {})
            if op == "I":
                row = tuple(json.loads(newvals))
                d[row] = d.get(row, 0) + 1
            elif op == "D":
                row = tuple(json.loads(oldvals))
                d[row] = d.get(row, 0) - 1
            else:  # "U" -> delete-old + insert-new
                old = tuple(json.loads(oldvals))
                nw = tuple(json.loads(newvals))
                d[old] = d.get(old, 0) - 1
                d[nw] = d.get(nw, 0) + 1
        self._exec(f"DELETE FROM {CHANGELOG} WHERE seq <= ?", (rows[-1][0],))
        for tbl, d in deltas.items():
            self._engine.apply(tbl, ZSet(d))

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
