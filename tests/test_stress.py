"""
Stress and edge-case tests for the Phase 3 implementation: unexpected input,
extreme data shapes, bulk loading, and cache invalidation.  (The long-running
1,000,000-product stress test is benchmarks/stress_test.py.)
"""

import os
import random
import unittest
from decimal import Decimal
from unittest import mock

from inventory import (DuplicateSKUError, InventoryError, InventoryManager, Product, ValidationError,
                       import_products)

DATA = os.path.join(os.path.dirname(__file__), os.pardir, "data")

GARBAGE = [None, "", "  ", "x", [], {}, (), object(), float("nan"), float("inf"), -0.0, 10**40, -10**40,
           b"bytes", "1e999999", "-0.00", "sNaN", "0x10", "١٢", True, 3.000000001, "9" * 60]


def catalog(n=20):
    inv = InventoryManager()
    inv.bulk_load([Product(f"S{i:03}", f"Item {i}", "Cat", "1.00", 5, 2) for i in range(n)])
    return inv


def state(inv):
    cats = inv.categories()
    return (len(inv), inv.total_value, cats,
            [[p.sku for p in inv.products_in_category(c)] for c in cats],
            [p.sku for p in inv.products_in_price_range(0, 10_000_000)],
            [p.sku for p in inv.low_stock()], [p.sku for p in inv.search_by_name("i")])


class TestUnexpectedInput(unittest.TestCase):
    def test_garbage_only_raises_inventory_errors(self):
        """Every public entry point, every odd value: the result is either a normal
        return or an InventoryError, never a raw TypeError/InvalidOperation/etc."""
        inv = catalog()
        calls = {
            "get": lambda a: inv.get(a), "remove": lambda a: inv.remove_product(a),
            "adjust": lambda a: inv.adjust_quantity("S001", a),
            "price": lambda a: inv.update_price("S002", a),
            "move": lambda a: inv.change_category("S003", a),
            "rename": lambda a: inv.rename_product("S004", a),
            "reorder": lambda a: inv.set_reorder_point("S005", a),
            "category": lambda a: inv.products_in_category(a),
            "range low": lambda a: inv.products_in_price_range(a, 5),
            "range high": lambda a: inv.products_in_price_range(0, a),
            "search": lambda a: inv.search_by_name(a), "limit": lambda a: inv.search_by_name("i", a),
            "cheapest": lambda a: inv.cheapest(a), "bulk": lambda a: inv.bulk_load(a),
            "add": lambda a: inv.add_product(a),
            "Product price": lambda a: Product("Z", "n", "c", a, 1),
            "Product quantity": lambda a: Product("Z", "n", "c", "1", a),
            "Product name": lambda a: Product("Z", a, "c", "1", 1),
        }
        for name, call in calls.items():
            for value in GARBAGE:
                with self.subTest(call=name, value=repr(value)[:20]):
                    try:
                        call(value)
                    except InventoryError:
                        pass
        self.assertEqual(inv.verify_integrity(), [])

    def test_canonical_prices(self):
        self.assertEqual(str(Product("Z", "n", "c", "-0.00", 1).price), "0.00")    # no negative zero
        self.assertEqual(str(Product("Z", "n", "c", -0.0, 1).price), "0.00")
        self.assertEqual(str(Product("Z", "n", "c", "1e2", 1).price), "100.00")
        self.assertEqual(str(Product("Z", "n", "c", "10000000.00", 1).price), "10000000.00")
        with self.assertRaises(ValidationError):
            Product("Z", "n", "c", "10000000.01", 1)
        a, b = Product("A", "n", "c", "19.99", 1), Product("B", "n", "c", 19.99, 1)
        self.assertIs(a.price, b.price)                     # shared flyweight Decimal


class TestExtremeShapes(unittest.TestCase):
    def test_every_product_same_price(self):
        inv = InventoryManager()
        inv.bulk_load([Product(f"S{i:05}", f"n{i}", "C", "5.00", 1) for i in range(5000)])
        same = inv.products_in_price_range("5.00", "5.00")
        self.assertEqual([p.sku for p in same], [f"S{i:05}" for i in range(5000)])    # SKU order
        inv.update_price("S02500", "4.99")
        self.assertEqual(inv.cheapest(1)[0].sku, "S02500")
        self.assertEqual(len(inv.products_in_price_range("5.00", "5.00")), 4999)
        self.assertEqual(inv.verify_integrity(), [])

    def test_one_huge_category_then_empty(self):
        inv = InventoryManager()
        inv.bulk_load([Product(f"S{i:05}", "n", "Only", "1.00", 1) for i in range(3000)])
        self.assertEqual(len(inv.products_in_category("Only")), 3000)
        for i in range(3000):
            inv.change_category(f"S{i:05}", f"New {i % 3}")
        self.assertEqual(inv.categories(), ["New 0", "New 1", "New 2"])
        self.assertEqual(inv.products_in_category("Only"), [])
        self.assertEqual(inv.verify_integrity(), [])

    def test_everything_low_stock(self):
        inv = InventoryManager()
        rng = random.Random(4)
        inv.bulk_load([Product(f"S{i:05}", "n", "C", "1.00", rng.randint(0, 9), 10) for i in range(4000)])
        low = inv.low_stock()
        self.assertEqual(len(low), 4000)
        self.assertEqual(low, sorted(low, key=lambda p: (p.stock_gap, p.sku)))

    def test_huge_quantities_keep_exact_totals(self):
        inv = InventoryManager()
        inv.add_product(Product("BIG", "n", "C", "10000000.00", 10**15))
        inv.add_product(Product("PENNY", "n", "C", "0.01", 10**15 + 1))
        self.assertEqual(inv.total_value, Decimal("10000000.00") * 10**15 + Decimal("0.01") * (10**15 + 1))
        inv.adjust_quantity("PENNY", -(10**15))
        self.assertEqual(inv.get("PENNY").quantity, 1)

    def test_long_and_unicode_names(self):
        inv = InventoryManager()
        long_name = "Desk " + "x" * 10_000
        inv.add_product(Product("L", long_name, "C", "1.00", 1))
        inv.add_product(Product("E", "Émigré café ☕", "C", "1.00", 1))
        inv.add_product(Product("K", "\U0010FFFF\U0010FFFF tail", "C", "1.00", 1))
        self.assertEqual([p.sku for p in inv.search_by_name("desk x")], ["L"])
        self.assertEqual([p.sku for p in inv.search_by_name("ÉMIGRÉ")], ["E"])
        self.assertEqual([p.sku for p in inv.search_by_name("\U0010FFFF")], ["K"])   # max code point
        self.assertEqual(inv.verify_integrity(), [])

    def test_delete_heavy_churn(self):
        inv = catalog(500)
        rng = random.Random(6)
        live = [f"S{i:03}" for i in range(500)]
        for step in range(5000):
            inv.remove_product(live.pop(rng.randrange(len(live))))
            sku = f"N{step:05}"
            inv.add_product(Product(sku, f"Item {step}", f"Cat {step % 7}", f"{rng.randint(1, 999)}.00", 3, 4))
            live.append(sku)
        self.assertEqual(len(inv), 500)
        self.assertEqual(inv.verify_integrity(), [])


class TestBulkLoad(unittest.TestCase):
    def products(self, n=300, start=0):
        rng = random.Random(start)
        return [Product(f"S{i:04}", f"{rng.choice(['desk', 'lamp', 'pen'])} {i}", f"C{i % 9}",
                        f"{rng.randint(1, 50_000) / 100:.2f}", rng.randint(0, 30), rng.randint(0, 10))
                for i in range(start, start + n)]

    def test_bulk_equals_one_at_a_time(self):
        items = self.products()
        one, bulk = InventoryManager(), InventoryManager()
        for p in items:
            one.add_product(p)
        bulk.bulk_load(items)
        self.assertEqual(state(one), state(bulk))
        self.assertEqual(bulk.verify_integrity(), [])

    def test_bulk_merges_into_existing(self):
        inv = InventoryManager()
        inv.bulk_load(self.products(200))
        inv.adjust_quantity("S0005", 7)
        inv.bulk_load(self.products(200, start=200))
        ref = InventoryManager()
        for p in self.products(200) + self.products(200, start=200):
            ref.add_product(p)
        ref.adjust_quantity("S0005", 7)
        self.assertEqual(state(inv), state(ref))

    def test_bad_batches_change_nothing(self):
        inv = catalog()
        before = state(inv)
        for bad, exc in (([Product("S001", "dup", "C", "1", 1)], DuplicateSKUError),
                         ([Product("NEW", "a", "C", "1", 1), Product("NEW", "b", "C", "1", 1)],
                          DuplicateSKUError),
                         ([Product("OK", "a", "C", "1", 1), "not a product"], ValidationError),
                         (None, ValidationError)):
            with self.subTest(exc=exc.__name__), self.assertRaises(exc):
                inv.bulk_load(bad)
            self.assertEqual(state(inv), before)
        with self.assertRaises(InventoryError), inv.transaction():
            inv.bulk_load([])
        self.assertEqual(inv.bulk_load([]), 0)

    def test_import_chooses_bulk_or_incremental(self):
        path = os.path.join(DATA, "products.csv")
        empty = InventoryManager()
        with mock.patch.object(empty, "bulk_load", wraps=empty.bulk_load) as spy:
            import_products(empty, path)                    # 25 rows into an empty catalog
        spy.assert_called_once()
        big = catalog(1000)
        with mock.patch.object(big, "bulk_load", wraps=big.bulk_load) as spy:
            result = import_products(big, path)             # 25 rows into 1,000 products
        spy.assert_not_called()
        self.assertEqual(len(result.succeeded), 25)


class TestCaches(unittest.TestCase):
    def test_category_cache_tracks_membership(self):
        inv = catalog(10)
        first = inv.products_in_category("Cat")
        self.assertIn("Cat", inv._cat_order)                # cached after first listing
        inv.adjust_quantity("S001", 3)                      # quantity change keeps the cache...
        self.assertIn("Cat", inv._cat_order)
        self.assertEqual(inv.products_in_category("Cat")[1].quantity, 8)   # ...but shows fresh records
        inv.change_category("S002", "Other")                # membership change drops it
        self.assertNotIn("Cat", inv._cat_order)
        self.assertEqual(len(inv.products_in_category("Cat")), len(first) - 1)
        inv.remove_product("S003")
        inv.add_product(Product("S999", "n", "Cat", "1", 1))
        self.assertEqual([p.sku for p in inv.products_in_category("Cat")][-1], "S999")
        self.assertEqual(inv.verify_integrity(), [])

    def test_category_list_is_a_copy(self):
        inv = catalog(3)
        inv.categories().append("Tampered")
        self.assertEqual(inv.categories(), ["Cat"])

    def test_stale_cache_is_detected(self):
        inv = catalog(5)
        inv.products_in_category("Cat")
        inv._categories["Cat"].discard("S001")              # bypass the facade
        self.assertTrue(any("stale" in p for p in inv.verify_integrity()))


if __name__ == "__main__":
    unittest.main()
