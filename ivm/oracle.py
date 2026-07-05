"""The lie detector: from-scratch recompute of a view over the accumulated base
tables. Test-only — deliberately dumb and obviously correct, sharing no code
with the incremental operators. If the incremental engine ever disagrees with
this, the engine is wrong.

`recompute` is the Milestone 0 hand-wired GROUP BY oracle. `eval_plan` is the
Milestone 1 general interpreter: it walks a plan and evaluates each node over
full Z-sets the obvious way."""

from ivm.zset import ZSet
from ivm.row import Row, index_of
from ivm import plan as P


def recompute(table):
    groups = {}
    for (category, amount), weight in table.items():
        count, total = groups.get(category, (0, 0))
        groups[category] = (count + weight, total + amount * weight)
    return {cat: ct for cat, ct in groups.items() if ct[0] != 0}


def eval_plan(plan, tables):
    """Evaluate a plan from scratch. Returns (schema, ZSet-of-result-rows)."""
    if isinstance(plan, P.Source):
        return plan.schema, tables.get(plan.table, ZSet())

    if isinstance(plan, P.Filter):
        schema, z = eval_plan(plan.input, tables)
        idx = index_of(schema)
        kept = {row: w for row, w in z.items() if plan.predicate(Row(row, idx))}
        return schema, ZSet(kept)

    if isinstance(plan, P.Project):
        schema, z = eval_plan(plan.input, tables)
        idx = index_of(schema)
        out_schema = tuple(name for name, _ in plan.outputs)
        acc = {}
        for row, w in z.items():
            r = Row(row, idx)
            out_row = tuple(expr(r) for _name, expr in plan.outputs)
            acc[out_row] = acc.get(out_row, 0) + w
        return out_schema, ZSet(acc)

    if isinstance(plan, P.Aggregate):
        schema, z = eval_plan(plan.input, tables)
        idx = index_of(schema)
        key_idx = [idx[c] for c in plan.group_by]
        specs = _agg_specs(plan.aggregates, idx)
        groups = {}  # key -> [net_weight, running values aligned to aggregates]
        for row, w in z.items():
            key = tuple(row[i] for i in key_idx)
            st = groups.get(key)
            if st is None:
                st = groups[key] = [0] + [0] * len(specs)
            st[0] += w
            for i, (kind, ci) in enumerate(specs):
                if kind == "sum":
                    st[1 + i] += row[ci] * w
        out_schema = tuple(plan.group_by) + tuple(a.name for a in plan.aggregates)
        acc = {}
        for key, st in groups.items():
            if st[0] == 0:
                continue
            values = tuple(
                st[0] if kind == "count" else st[1 + i]
                for i, (kind, ci) in enumerate(specs)
            )
            acc[key + values] = 1
        return out_schema, ZSet(acc)

    if isinstance(plan, P.Join):
        ls, lz = eval_plan(plan.left, tables)
        rs, rz = eval_plan(plan.right, tables)
        lidx = index_of(ls)
        ridx = index_of(rs)
        lk = [lidx[c] for c in plan.left_keys]
        rk = [ridx[c] for c in plan.right_keys]
        right_keyset = set(plan.right_keys)
        r_nonkey = [i for i, name in enumerate(rs) if name not in right_keyset]
        out_schema = tuple(ls) + tuple(rs[i] for i in r_nonkey)
        right_by_key = {}
        for rrow, rw in rz.items():
            key = tuple(rrow[i] for i in rk)
            right_by_key.setdefault(key, []).append((rrow, rw))
        acc = {}
        for lrow, lw in lz.items():
            key = tuple(lrow[i] for i in lk)
            for rrow, rw in right_by_key.get(key, ()):
                combined = lrow + tuple(rrow[i] for i in r_nonkey)
                acc[combined] = acc.get(combined, 0) + lw * rw
        return out_schema, ZSet(acc)

    raise NotImplementedError(f"oracle has no rule for {type(plan).__name__}")


def _agg_specs(aggregates, idx):
    """Lower each aggregate spec to ('count', None) or ('sum', column_index)."""
    specs = []
    for a in aggregates:
        if isinstance(a, P.Count):
            specs.append(("count", None))
        elif isinstance(a, P.Sum):
            specs.append(("sum", idx[a.column]))
        else:
            raise NotImplementedError(f"unknown aggregate {type(a).__name__}")
    return specs
