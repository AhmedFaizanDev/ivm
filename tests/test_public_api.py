"""The public API surface (top-level imports) and the exact README example —
kept as a test so the copy-paste snippet can never silently rot."""


def test_public_api_imports():
    from ivm import Engine, compile_sql, SqlError, ZSet, __version__

    assert isinstance(__version__, str)
    assert all(x is not None for x in (Engine, compile_sql, SqlError, ZSet))


def test_readme_example_stays_correct():
    from ivm import Engine

    eng = Engine()
    catalog = {"sales": ("id", "region", "amount")}
    view = eng.add_sql_view(
        "by_region",
        "SELECT region, COUNT(*) AS n, SUM(amount) AS total "
        "FROM sales WHERE amount > 0 GROUP BY region",
        catalog,
    )

    eng.insert("sales", (1, "west", 100))
    eng.insert("sales", (2, "west", 50))
    eng.insert("sales", (3, "east", 200))
    assert view.result() == {("west", 2, 150): 1, ("east", 1, 200): 1}

    eng.delete("sales", (2, "west", 50))  # retract — view updates incrementally
    assert view.result() == {("west", 1, 100): 1, ("east", 1, 200): 1}
