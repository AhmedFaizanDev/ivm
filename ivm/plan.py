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
