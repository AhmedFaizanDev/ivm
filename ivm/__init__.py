"""ivm — embeddable Incremental View Maintenance.

Keep declared SQL views fresh by applying only the deltas to their base tables
(built on the DBSP model — Z-sets and differential operators), with no external
service. Write SQL, mutate base tables, read a view that stays correct
incrementally.

    from ivm import Engine

    eng = Engine()
    view = eng.add_sql_view(
        "totals",
        "SELECT cat, SUM(amount) AS total FROM t GROUP BY cat",
        {"t": ("id", "cat", "amount")},
    )
    eng.insert("t", (1, "food", 10))
    view.result()          # {("food", 10): 1}
"""

from ivm.engine import Engine, View
from ivm.sql import compile_sql, SqlError
from ivm.zset import ZSet

__version__ = "0.1.0"
__all__ = ["Engine", "View", "compile_sql", "SqlError", "ZSet", "__version__"]
