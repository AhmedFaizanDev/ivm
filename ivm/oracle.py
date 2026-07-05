"""The lie detector: from-scratch recompute of the view over the accumulated
base table. Test-only — deliberately dumb and obviously correct."""


def recompute(table):
    groups = {}
    for (category, amount), weight in table.items():
        count, total = groups.get(category, (0, 0))
        groups[category] = (count + weight, total + amount * weight)
    return {cat: ct for cat, ct in groups.items() if ct[0] != 0}
