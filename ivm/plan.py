"""The plan: an explicit, declarative description of a view as a tree of nodes.

A hand-built plan API (no SQL parser yet — that's later sugar). The engine
compiles a plan into an incremental operator graph; the oracle interprets the
same plan from scratch. Nodes are frozen dataclasses; some carry Python
callables (predicates, projection expressions) — join/group keys are column
NAMES, never opaque lambdas, so the join can build a real index on them."""

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Source:
    """A base table, identified by name and its column schema."""

    table: str
    schema: tuple


@dataclass(frozen=True)
class Filter:
    """Keep input rows for which predicate(Row) is true. Linear, stateless."""

    input: object
    predicate: Callable


@dataclass(frozen=True)
class Project:
    """Rewrite each row into named output columns. Linear, stateless.

    `outputs` is a sequence of (name, expr) where expr is a Callable(Row).
    A projection may drop columns, so distinct input rows can collapse to one
    output row — their weights merge."""

    input: object
    outputs: tuple


@dataclass(frozen=True)
class Distinct:
    """SELECT DISTINCT: collapse Z-set multiplicities to set semantics — each row
    with positive net weight appears exactly once. Non-linear: the operator keeps
    per-row net weight and flips a row in/out only as its weight crosses zero.
    Output schema = input schema."""

    input: object


@dataclass(frozen=True)
class Count:
    """COUNT(*) for a group — the net Z-set weight of its rows. Output column `name`."""

    name: str


@dataclass(frozen=True)
class Sum:
    """SUM(column) for a group. Output column `name`."""

    name: str
    column: str


@dataclass(frozen=True)
class Avg:
    """AVG(column) for a group = SUM(column) / COUNT(*). Linear: keeps a running
    sum alongside the group's net weight (count) and divides at read time.
    Output column `name`."""

    name: str
    column: str


@dataclass(frozen=True)
class Min:
    """MIN(column) for a group. NON-linear: deleting the current minimum must
    recover the next one, so the operator keeps the group's full value multiset
    (value -> weight) rather than a single accumulator. Output column `name`."""

    name: str
    column: str


@dataclass(frozen=True)
class Max:
    """MAX(column) for a group. Non-linear, same multiset state as Min."""

    name: str
    column: str


@dataclass(frozen=True)
class Aggregate:
    """GROUP BY `group_by`, computing `aggregates` (Count/Sum) per group.

    A group exists while its net weight (COUNT) is non-zero; it vanishes at zero
    regardless of any SUM value. Output schema = group_by columns + agg names."""

    input: object
    group_by: tuple
    aggregates: tuple


@dataclass(frozen=True)
class Join:
    """Inner equi-join of `left` and `right` on paired key columns. Bilinear —
    the operator retains both inputs. Output columns = all of left, plus right's
    non-key columns (the key values are shared, so right keys are dropped);
    non-key names must not collide across the two sides."""

    left: object
    right: object
    left_keys: tuple
    right_keys: tuple


@dataclass(frozen=True)
class LeftJoin:
    """LEFT OUTER equi-join. Every left row appears: combined with each matching
    right row, or NULL-padded once (right non-key columns = None) when its key has
    no match. Same output schema as the inner Join. Non-linear: a right-side
    change that flips a key between "no matches" and "some match" must flip every
    left row at that key between its padded and matched forms.

    """

    left: object
    right: object
    left_keys: tuple
    right_keys: tuple


@dataclass(frozen=True)
class RightJoin:
    """RIGHT OUTER equi-join — the mirror of LeftJoin. Every right row appears;
    unmatched right rows are NULL-padded on the left (left non-key = None), with
    the shared key coalesced from the right. Same output schema as inner/LEFT.
    Non-linear: a left-side change flips every right row at a key between padded
    and matched."""

    left: object
    right: object
    left_keys: tuple
    right_keys: tuple


@dataclass(frozen=True)
class FullJoin:
    """FULL OUTER equi-join. Every left AND right row appears: matched rows
    combined, unmatched left rows left-padded (right non-key = None), unmatched
    right rows right-padded (left non-key = None, key coalesced from the right).
    Both sides flip. Same output schema as inner/LEFT."""

    left: object
    right: object
    left_keys: tuple
    right_keys: tuple
