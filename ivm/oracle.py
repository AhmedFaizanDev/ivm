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
                st = groups[key] = [0] + [
                    {} if kind in ("min", "max") else 0 for kind, _ci in specs
                ]
            st[0] += w
            for i, (kind, ci) in enumerate(specs):
                if kind in ("sum", "avg"):
                    st[1 + i] += row[ci] * w
                elif kind in ("min", "max"):
                    ms = st[1 + i]
                    val = row[ci]
                    nw = ms.get(val, 0) + w
                    if nw == 0:
                        ms.pop(val, None)
                    else:
                        ms[val] = nw
        out_schema = tuple(plan.group_by) + tuple(a.name for a in plan.aggregates)
        acc = {}
        for key, st in groups.items():
            if st[0] == 0:
                continue
            values = tuple(_oracle_agg_value(st, i, kind) for i, (kind, ci) in enumerate(specs))
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

    if isinstance(plan, P.LeftJoin):
        ls, lz = eval_plan(plan.left, tables)
        rs, rz = eval_plan(plan.right, tables)
        lidx = index_of(ls)
        ridx = index_of(rs)
        lk = [lidx[c] for c in plan.left_keys]
        rk = [ridx[c] for c in plan.right_keys]
        right_keyset = set(plan.right_keys)
        r_nonkey = [i for i, name in enumerate(rs) if name not in right_keyset]
        out_schema = tuple(ls) + tuple(rs[i] for i in r_nonkey)
        pad = (None,) * len(r_nonkey)
        right_by_key = {}
        for rrow, rw in rz.items():
            key = tuple(rrow[i] for i in rk)
            right_by_key.setdefault(key, []).append((rrow, rw))
        acc = {}
        for lrow, lw in lz.items():
            key = tuple(lrow[i] for i in lk)
            matches = right_by_key.get(key)
            if matches:  # inner-join rows for a matched left row
                for rrow, rw in matches:
                    combined = lrow + tuple(rrow[i] for i in r_nonkey)
                    acc[combined] = acc.get(combined, 0) + lw * rw
            else:  # unmatched left row -> one NULL-padded output
                padded = lrow + pad
                acc[padded] = acc.get(padded, 0) + lw
        return out_schema, ZSet(acc)

    if isinstance(plan, (P.RightJoin, P.FullJoin)):
        ls, lz = eval_plan(plan.left, tables)
        rs, rz = eval_plan(plan.right, tables)
        lidx = index_of(ls)
        ridx = index_of(rs)
        lk = [lidx[c] for c in plan.left_keys]
        rk = [ridx[c] for c in plan.right_keys]
        right_keyset = set(plan.right_keys)
        r_nonkey = [i for i, name in enumerate(rs) if name not in right_keyset]
        out_schema = tuple(ls) + tuple(rs[i] for i in r_nonkey)
        left_len = len(ls)
        left_pad = (None,) * len(r_nonkey)

        def rightpad(rrow):
            parts = [None] * left_len
            for lpos, rpos in zip(lk, rk):
                parts[lpos] = rrow[rpos]
            return tuple(parts) + tuple(rrow[i] for i in r_nonkey)

        left_by_key = {}
        for lrow, lw in lz.items():
            left_by_key.setdefault(tuple(lrow[i] for i in lk), []).append((lrow, lw))
        right_by_key = {}
        for rrow, rw in rz.items():
            right_by_key.setdefault(tuple(rrow[i] for i in rk), []).append((rrow, rw))

        acc = {}
        if isinstance(plan, P.FullJoin):
            # matched + left-unmatched, iterating left rows
            for lrow, lw in lz.items():
                key = tuple(lrow[i] for i in lk)
                matches = right_by_key.get(key)
                if matches:
                    for rrow, rw in matches:
                        combined = lrow + tuple(rrow[i] for i in r_nonkey)
                        acc[combined] = acc.get(combined, 0) + lw * rw
                else:
                    padded = lrow + left_pad
                    acc[padded] = acc.get(padded, 0) + lw
            # right-unmatched only (matched already emitted above)
            for rrow, rw in rz.items():
                key = tuple(rrow[i] for i in rk)
                if not left_by_key.get(key):
                    padded = rightpad(rrow)
                    acc[padded] = acc.get(padded, 0) + rw
        else:  # RightJoin: iterate right rows (matched, else right-padded)
            for rrow, rw in rz.items():
                key = tuple(rrow[i] for i in rk)
                matches = left_by_key.get(key)
                if matches:
                    for lrow, lw in matches:
                        combined = lrow + tuple(rrow[i] for i in r_nonkey)
                        acc[combined] = acc.get(combined, 0) + lw * rw
                else:
                    padded = rightpad(rrow)
                    acc[padded] = acc.get(padded, 0) + rw
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
        elif isinstance(a, P.Avg):
            specs.append(("avg", idx[a.column]))
        elif isinstance(a, P.Min):
            specs.append(("min", idx[a.column]))
        elif isinstance(a, P.Max):
            specs.append(("max", idx[a.column]))
        else:
            raise NotImplementedError(f"unknown aggregate {type(a).__name__}")
    return specs


def _oracle_agg_value(st, i, kind):
    """Read aggregate i from a group accumulator [net_weight, slot0, slot1, ...].
    Independent of the operator's reader — the oracle recomputes from scratch."""
    if kind == "count":
        return st[0]
    if kind == "avg":
        return st[1 + i] / st[0]
    if kind == "min":
        return min(st[1 + i])
    if kind == "max":
        return max(st[1 + i])
    return st[1 + i]  # sum
