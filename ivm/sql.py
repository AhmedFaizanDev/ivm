"""SQL front-end: a hand-written, zero-dependency compiler from a SELECT subset
to the plan API (Source/Filter/Project/Join/.../Aggregate). The plan API is the
stable core; this is a thin compiler on top. Pipeline: tokenize -> recursive-
descent parse -> compile against a catalog {table: schema}.

Supported: SELECT * | cols | aggregates, FROM t [alias],
[INNER|LEFT|RIGHT|FULL [OUTER]] JOIN t2 [alias] ON a.k=b.k [AND ...],
WHERE (comparisons / IS [NOT] NULL, AND/OR, parens), GROUP BY.
See ivm-sql-frontend.md for the design note and deferred features."""

import operator as _operator
import re

from ivm import plan as P


class SqlError(ValueError):
    """Raised for lex/parse/compile errors in the SQL front-end."""


# --------------------------------------------------------------------------- #
# Lexer
# --------------------------------------------------------------------------- #

_SCAN = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<number>\d+\.\d+|\d+)
    | '(?P<string>(?:[^']|'')*)'
    | (?P<op><=|>=|<>|!=|=|<|>)
    | (?P<minus>-)
    | (?P<star>\*)
    | (?P<punct>[(),.])
    | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)


class _Tok:
    __slots__ = ("kind", "value")

    def __init__(self, kind, value):
        self.kind = kind
        self.value = value

    def __repr__(self):
        return f"{self.kind}:{self.value!r}"


def tokenize(sql):
    toks, i, n = [], 0, len(sql)
    while i < n:
        m = _SCAN.match(sql, i)
        if not m:
            raise SqlError(f"unexpected character {sql[i]!r} at position {i}")
        i = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        val = m.group(kind)
        if kind == "number":
            val = float(val) if "." in val else int(val)
        elif kind == "string":
            val = val.replace("''", "'")
        toks.append(_Tok(kind, val))
    toks.append(_Tok("eof", None))
    return toks


# --------------------------------------------------------------------------- #
# AST
# --------------------------------------------------------------------------- #


class _Col:
    def __init__(self, table, name):
        self.table = table
        self.name = name


class _Lit:
    def __init__(self, value):
        self.value = value


class _Agg:
    def __init__(self, func, arg):  # func in COUNT/SUM/AVG/MIN/MAX; arg is _Col or "*"
        self.func = func
        self.arg = arg


class _Item:
    def __init__(self, expr, alias):  # expr is _Col or _Agg
        self.expr = expr
        self.alias = alias


class _And:
    def __init__(self, items):
        self.items = items


class _Or:
    def __init__(self, items):
        self.items = items


class _Cmp:
    def __init__(self, left, op, right):
        self.left = left
        self.op = op
        self.right = right


class _IsNull:
    def __init__(self, operand, negated):
        self.operand = operand
        self.negated = negated


class _JoinClause:
    def __init__(self, kind, table, alias, on):  # kind: inner/left/right/full
        self.kind = kind
        self.table = table
        self.alias = alias
        self.on = on  # list of (_Col left-ish, _Col right-ish)


class _Select:
    def __init__(self, items, table, alias, joins, where, group_by):
        self.items = items  # "*" or list of _Item
        self.table = table
        self.alias = alias
        self.joins = joins
        self.where = where
        self.group_by = group_by  # list of _Col


# --------------------------------------------------------------------------- #
# Parser (recursive descent)
# --------------------------------------------------------------------------- #

_JOIN_KINDS = {"INNER", "LEFT", "RIGHT", "FULL"}
# words that can follow a table reference and must NOT be swallowed as an alias
_AFTER_TABLE = {"WHERE", "GROUP", "ON", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER"}
_AGGS = {"COUNT", "SUM", "AVG", "MIN", "MAX"}


class _Parser:
    def __init__(self, toks):
        self.toks = toks
        self.pos = 0

    def peek(self):
        return self.toks[self.pos]

    def next(self):
        t = self.toks[self.pos]
        self.pos += 1
        return t

    def at_kw(self, kw):
        t = self.peek()
        return t.kind == "word" and t.value.upper() == kw

    def accept_kw(self, kw):
        if self.at_kw(kw):
            self.pos += 1
            return True
        return False

    def eat_kw(self, kw):
        if not self.accept_kw(kw):
            raise SqlError(f"expected {kw}, got {self.peek()}")

    def accept_punct(self, ch):
        t = self.peek()
        if t.kind == "punct" and t.value == ch:
            self.pos += 1
            return True
        return False

    def eat_punct(self, ch):
        if not self.accept_punct(ch):
            raise SqlError(f"expected {ch!r}, got {self.peek()}")

    def expect(self, kind):
        t = self.peek()
        if t.kind != kind:
            raise SqlError(f"expected {kind}, got {t}")
        self.pos += 1
        return t

    # -- top level --

    def parse(self):
        self.eat_kw("SELECT")
        items = self.parse_items()
        self.eat_kw("FROM")
        table, alias = self.parse_table_ref()
        joins = self.parse_joins()
        where = self.parse_or() if self.accept_kw("WHERE") else None
        group_by = []
        if self.accept_kw("GROUP"):
            self.eat_kw("BY")
            group_by = [self.parse_column()]
            while self.accept_punct(","):
                group_by.append(self.parse_column())
        self.expect("eof")
        return _Select(items, table, alias, joins, where, group_by)

    # -- select items --

    def parse_items(self):
        if self.peek().kind == "star":
            self.next()
            return "*"
        items = [self.parse_item()]
        while self.accept_punct(","):
            items.append(self.parse_item())
        return items

    def parse_item(self):
        t = self.peek()
        if t.kind == "word" and t.value.upper() in _AGGS and self.toks[self.pos + 1].kind == "punct" \
                and self.toks[self.pos + 1].value == "(":
            func = self.next().value.upper()
            self.eat_punct("(")
            if self.peek().kind == "star":
                self.next()
                arg = "*"
            else:
                arg = self.parse_column()
            self.eat_punct(")")
            expr = _Agg(func, arg)
        else:
            expr = self.parse_column()
        alias = self.expect("word").value if self.accept_kw("AS") else None
        return _Item(expr, alias)

    def parse_column(self):
        name = self.expect("word").value
        table = None
        if self.accept_punct("."):
            table = name
            name = self.expect("word").value
        return _Col(table, name)

    # -- from / joins --

    def parse_table_ref(self):
        name = self.expect("word").value
        alias = None
        if self.accept_kw("AS"):
            alias = self.expect("word").value
        elif self.peek().kind == "word" and self.peek().value.upper() not in _AFTER_TABLE:
            alias = self.next().value
        return name, alias

    def parse_joins(self):
        joins = []
        while True:
            kind = "inner"
            t = self.peek()
            if t.kind == "word" and t.value.upper() in _JOIN_KINDS:
                kw = self.next().value.upper()
                kind = kw.lower() if kw != "INNER" else "inner"
                self.accept_kw("OUTER")
            elif self.at_kw("JOIN"):
                kind = "inner"
            else:
                break
            self.eat_kw("JOIN")
            table, alias = self.parse_table_ref()
            self.eat_kw("ON")
            on = [self.parse_eq()]
            while self.accept_kw("AND"):
                on.append(self.parse_eq())
            joins.append(_JoinClause(kind, table, alias, on))
        return joins

    def parse_eq(self):
        left = self.parse_column()
        t = self.peek()
        if t.kind != "op" or t.value != "=":
            raise SqlError(f"JOIN ON supports only '=', got {t}")
        self.next()
        right = self.parse_column()
        return (left, right)

    # -- where expression --

    def parse_or(self):
        parts = [self.parse_and()]
        while self.accept_kw("OR"):
            parts.append(self.parse_and())
        return parts[0] if len(parts) == 1 else _Or(parts)

    def parse_and(self):
        parts = [self.parse_cmp()]
        while self.accept_kw("AND"):
            parts.append(self.parse_cmp())
        return parts[0] if len(parts) == 1 else _And(parts)

    def parse_cmp(self):
        if self.accept_punct("("):
            node = self.parse_or()
            self.eat_punct(")")
            return node
        left = self.parse_operand()
        if self.accept_kw("IS"):
            negated = self.accept_kw("NOT")
            self.eat_kw("NULL")
            return _IsNull(left, negated)
        op = self.expect("op").value
        right = self.parse_operand()
        return _Cmp(left, op, right)

    def parse_operand(self):
        negate = self.peek().kind == "minus"
        if negate:
            self.next()
        t = self.peek()
        if t.kind == "number":
            self.next()
            return _Lit(-t.value if negate else t.value)
        if negate:
            raise SqlError("unary '-' must precede a number")
        if t.kind == "string":
            self.next()
            return _Lit(t.value)
        if t.kind == "word":
            up = t.value.upper()
            if up == "NULL":
                self.next()
                return _Lit(None)
            if up == "TRUE":
                self.next()
                return _Lit(True)
            if up == "FALSE":
                self.next()
                return _Lit(False)
            return self.parse_column()
        raise SqlError(f"expected an operand, got {t}")


# --------------------------------------------------------------------------- #
# Compiler: AST + catalog -> plan
# --------------------------------------------------------------------------- #

_CMP = {
    "=": _operator.eq,
    "!=": _operator.ne,
    "<>": _operator.ne,
    "<": _operator.lt,
    "<=": _operator.le,
    ">": _operator.gt,
    ">=": _operator.ge,
}

_JOIN_NODE = {
    "inner": P.Join,
    "left": P.LeftJoin,
    "right": P.RightJoin,
    "full": P.FullJoin,
}

_AGG_NODE = {"SUM": P.Sum, "AVG": P.Avg, "MIN": P.Min, "MAX": P.Max}


def compile_sql(sql, catalog):
    """Compile a SELECT statement to a plan, resolving columns against
    catalog {table: schema}. Returns a plan node for engine.add_view."""
    ast = _Parser(tokenize(sql)).parse()
    return _Compiler(catalog).compile(ast)


class _Compiler:
    def __init__(self, catalog):
        self._catalog = {t: tuple(s) for t, s in catalog.items()}

    def _schema_of(self, table):
        if table not in self._catalog:
            raise SqlError(f"unknown table {table!r}")
        return self._catalog[table]

    def compile(self, ast):
        # 1. FROM + JOINs -> node, working schema, alias -> set(columns)
        node, working, aliases = self._compile_from(ast)

        # 2. WHERE
        if ast.where is not None:
            node = P.Filter(node, self._predicate(ast.where, working, aliases))

        # 3. GROUP BY / aggregates
        has_agg = ast.items != "*" and any(isinstance(it.expr, _Agg) for it in ast.items)
        if ast.group_by or has_agg:
            return self._compile_grouped(node, ast, working, aliases)

        # 4. SELECT projection
        if ast.items == "*":
            return node
        return self._project(node, ast, working, aliases)

    # -- FROM + JOINs --

    def _compile_from(self, ast):
        base_schema = self._schema_of(ast.table)
        node = P.Source(ast.table, base_schema)
        working = list(base_schema)
        # alias/table name -> its columns (for qualified resolution)
        aliases = {ast.table: set(base_schema)}
        if ast.alias:
            aliases[ast.alias] = set(base_schema)

        for jc in ast.joins:
            right_schema = self._schema_of(jc.table)
            right_cols = set(right_schema)
            # resolve each ON equality into (left_key, right_key)
            left_keys, right_keys = [], []
            for a, b in jc.on:
                la = self._maybe_in(a, working, aliases)
                lb = self._maybe_in(b, working, aliases)
                ra = a.name if a.name in right_cols and self._table_ok(a, jc) else None
                rb = b.name if b.name in right_cols and self._table_ok(b, jc) else None
                if la and rb:
                    left_keys.append(a.name)
                    right_keys.append(b.name)
                elif lb and ra:
                    left_keys.append(b.name)
                    right_keys.append(a.name)
                else:
                    raise SqlError(
                        f"JOIN ON {a.name}={b.name}: need one column from the left "
                        f"input and one from {jc.table!r}"
                    )
            r_nonkey = [c for c in right_schema if c not in set(right_keys)]
            collide = (set(working) | {*right_keys}) & set(r_nonkey)
            if collide:
                raise SqlError(f"joined columns collide (rename needed): {sorted(collide)}")
            node = _JOIN_NODE[jc.kind](node, P.Source(jc.table, right_schema),
                                       tuple(left_keys), tuple(right_keys))
            working = working + r_nonkey
            aliases[jc.table] = right_cols
            if jc.alias:
                aliases[jc.alias] = right_cols
        return node, working, aliases

    @staticmethod
    def _table_ok(col, jc):
        return col.table is None or col.table in (jc.table, jc.alias)

    @staticmethod
    def _maybe_in(col, working, aliases):
        if col.name not in working:
            return False
        if col.table is None:
            return True
        return col.table in aliases and col.name in aliases[col.table]

    # -- aggregate / GROUP BY --

    def _compile_grouped(self, node, ast, working, aliases):
        if ast.items == "*":
            raise SqlError("SELECT * is not allowed with GROUP BY / aggregates")
        group_cols = [self._resolve(c, working, aliases) for c in ast.group_by]
        aggs, output = [], []  # output: (out_name, source column in the post-agg schema)
        for it in ast.items:
            if isinstance(it.expr, _Agg):
                a = self._agg_node(it, working, aliases)
                aggs.append(a)
                output.append((a.name, a.name))
            else:
                col = self._resolve(it.expr, working, aliases)
                if col not in group_cols:
                    raise SqlError(f"SELECT column {col!r} must appear in GROUP BY")
                output.append((it.alias or col, col))
        agg_node = P.Aggregate(node, tuple(group_cols), tuple(aggs))
        post = list(group_cols) + [a.name for a in aggs]
        if output == [(c, c) for c in post]:  # SELECT already matches the agg output
            return agg_node
        return P.Project(agg_node, tuple((name, _getter(src)) for name, src in output))

    def _agg_node(self, item, working, aliases):
        agg = item.expr
        if agg.func == "COUNT":
            return P.Count(item.alias or "count")
        col = self._resolve(agg.arg, working, aliases)
        return _AGG_NODE[agg.func](item.alias or f"{agg.func.lower()}_{col}", col)

    # -- projection: each output reads its SOURCE column, named by alias-or-column --

    def _project(self, node, ast, working, aliases):
        output = [
            (it.alias or self._resolve(it.expr, working, aliases),
             self._resolve(it.expr, working, aliases))
            for it in ast.items
        ]
        if output == [(c, c) for c in working]:
            return node
        return P.Project(node, tuple((name, _getter(src)) for name, src in output))

    # -- column resolution --

    def _resolve(self, col, working, aliases):
        if col.table is not None:
            if col.table not in aliases:
                raise SqlError(f"unknown table/alias {col.table!r}")
            if col.name not in aliases[col.table]:
                raise SqlError(f"column {col.table}.{col.name} not found")
            return col.name
        if col.name not in working:
            raise SqlError(f"unknown column {col.name!r}")
        return col.name

    def _predicate(self, node, working, aliases):
        if isinstance(node, _And):
            parts = [self._predicate(c, working, aliases) for c in node.items]
            return lambda r: all(p(r) for p in parts)
        if isinstance(node, _Or):
            parts = [self._predicate(c, working, aliases) for c in node.items]
            return lambda r: any(p(r) for p in parts)
        if isinstance(node, _IsNull):
            get = self._operand(node.operand, working, aliases)
            if node.negated:
                return lambda r: get(r) is not None
            return lambda r: get(r) is None
        if isinstance(node, _Cmp):
            lget = self._operand(node.left, working, aliases)
            rget = self._operand(node.right, working, aliases)
            cmp = _CMP[node.op]

            def pred(r):
                a, b = lget(r), rget(r)
                if a is None or b is None:  # SQL: comparison with NULL is unknown
                    return False
                return cmp(a, b)

            return pred
        raise SqlError("malformed WHERE expression")

    def _operand(self, operand, working, aliases):
        if isinstance(operand, _Lit):
            v = operand.value
            return lambda r, _v=v: _v
        name = self._resolve(operand, working, aliases)
        return _getter(name)


def _getter(name):
    return lambda r, _n=name: r[_n]
