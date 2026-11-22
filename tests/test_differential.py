"""
Differential testing: the optimized Phase 3 package must behave exactly like
the frozen Phase 2 proof of concept.

Both packages receive the same long random sequence of operations, including
invalid ones.  After every operation, the result (or the exception type and
message) must match, and periodically every query is compared in full.
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "benchmarks"))

import inventory as p3                      # noqa: E402
import phase2_inventory as p2               # noqa: E402

CATEGORIES = ["Office", "Electronics", "Furniture", "Gifts", "Apparel"]
WORDS = ["desk", "Desk", "lamp", "LAMP", "cable", "Straße", "strasse", "pen", "mug", "Mouse"]
GOOD_PRICES = ["0.00", "0.01", "1.00", "9.99", "19.99", "19.99", "250.00", "999.99", 5, 2.5]
BAD_PRICES = ["-1", "abc", "1.005", "NaN", "Infinity", None]
GOOD_COUNTS = [0, 1, 5, 50, 3, 12]
BAD_COUNTS = [-1, 2.5, True, "3", None]


def pick(rng, good, bad, p_bad=0.15):
    """Mostly valid input (so the inventory grows), sometimes invalid (so errors are compared)."""
    return rng.choice(bad) if rng.random() < p_bad else rng.choice(good)


def outcome(call):
    """Run call() and describe what happened in a form comparable across packages."""
    try:
        return ("ok", describe(call()))
    except Exception as e:                  # noqa: BLE001 - we compare every error
        return ("error", type(e).__name__, str(e))


def describe(value):
    if isinstance(value, list):
        return [describe(v) for v in value]
    if hasattr(value, "sku") and hasattr(value, "reorder_point"):
        return (value.sku, value.name, value.category, str(value.price), value.quantity,
                value.reorder_point)
    return value if value is None or isinstance(value, (int, str, tuple)) else str(value)


def full_state(inv):
    cats = inv.categories()
    return (
        len(inv), str(inv.total_value), cats,
        [describe(inv.products_in_category(c)) for c in cats],
        describe(inv.products_in_price_range(0, 1000)),
        describe(inv.cheapest(5)),
        describe(inv.low_stock()),
        describe(inv.most_urgent()),
        [describe(inv.search_by_name(w)) for w in ("d", "de", "LA", "str", "z")],
    )


def random_operation(rng, skus):
    """Return (description, function(pkg, inv)) for one random, possibly invalid, operation."""
    sku = pick(rng, skus or ["NOPE"], ["NOPE", "", None], 0.1)
    kind = rng.choices(["add", "remove", "adjust", "price", "move", "rename", "reorder", "range",
                        "search", "cheapest"], [6, 1, 5, 3, 2, 2, 2, 2, 2, 1])[0]
    if kind == "add":
        new = f"S{rng.randrange(600):03}"
        fields = (new, f"{rng.choice(WORDS)} {rng.randrange(9)}", rng.choice(CATEGORIES),
                  pick(rng, GOOD_PRICES, BAD_PRICES), pick(rng, GOOD_COUNTS, BAD_COUNTS),
                  pick(rng, GOOD_COUNTS, BAD_COUNTS))
        return kind, lambda pkg, inv: inv.add_product(pkg.Product(*fields))
    if kind == "remove":
        return kind, lambda pkg, inv: inv.remove_product(sku)
    if kind == "adjust":
        delta = pick(rng, [1, 5, -1, -3, 20, 0], [-100, 1.5, True])
        return kind, lambda pkg, inv: inv.adjust_quantity(sku, delta)
    if kind == "price":
        price = pick(rng, GOOD_PRICES, BAD_PRICES)
        return kind, lambda pkg, inv: inv.update_price(sku, price)
    if kind == "move":
        cat = pick(rng, CATEGORIES + ["  Office  ", "New"], ["", None])
        return kind, lambda pkg, inv: inv.change_category(sku, cat)
    if kind == "rename":
        name = pick(rng, WORDS, ["", "  "])
        return kind, lambda pkg, inv: inv.rename_product(sku, name)
    if kind == "reorder":
        point = pick(rng, GOOD_COUNTS, BAD_COUNTS)
        return kind, lambda pkg, inv: inv.set_reorder_point(sku, point)
    if kind == "range":
        lo, hi = pick(rng, GOOD_PRICES, BAD_PRICES), pick(rng, GOOD_PRICES, BAD_PRICES)
        return kind, lambda pkg, inv: inv.products_in_price_range(lo, hi)
    if kind == "search":
        prefix = pick(rng, WORDS + ["s", "STR", "d"], ["", None])
        limit = pick(rng, [None, 0, 2, 10], [-1, "2"])
        return kind, lambda pkg, inv: inv.search_by_name(prefix, limit)
    k = pick(rng, [0, 1, 3, 25], [-1, None])
    return kind, lambda pkg, inv: inv.cheapest(k)


class TestPhase2Equivalence(unittest.TestCase):
    def test_random_sequences_match_phase2(self):
        sizes = []
        for seed in range(12):
            rng = random.Random(seed)
            old, new = p2.InventoryManager(seed=seed), p3.InventoryManager(seed=seed)
            for step in range(1500):
                skus = [p.sku for c in new.categories() for p in new.products_in_category(c)]
                kind, op = random_operation(rng, skus)
                a, b = outcome(lambda: op(p2, old)), outcome(lambda: op(p3, new))
                self.assertEqual(a, b, f"seed {seed}, step {step}, {kind}")
                if step % 150 == 149:
                    self.assertEqual(full_state(old), full_state(new), f"seed {seed}, step {step}")
            self.assertEqual(new.verify_integrity(), [])
            sizes.append(len(new))
        # The sequences must build real inventories, not just compare error messages.
        self.assertGreater(min(sizes), 100)

    def test_transactions_and_batches_match(self):
        data = os.path.join(os.path.dirname(__file__), os.pardir, "data")
        results = []
        for pkg in (p2, p3):
            inv = pkg.InventoryManager(seed=1)
            r1 = pkg.import_products(inv, os.path.join(data, "products.csv"))
            movements, _ = pkg.read_movements(os.path.join(data, "movements.csv"))
            r2 = pkg.apply_movements(inv, movements, all_or_nothing=True)
            r3 = pkg.apply_movements(inv, movements)
            r4 = pkg.import_products(inv, os.path.join(data, "products_with_errors.csv"))
            results.append(([r.summary() for r in (r1, r2, r3, r4)],
                            [r.failed for r in (r1, r2, r3, r4)], full_state(inv)))
        self.assertEqual(results[0], results[1])


if __name__ == "__main__":
    unittest.main()
