"""
Failure injection: make one internal index operation fail partway through an
update and check that the update is rolled back completely.
"""

import unittest
from decimal import Decimal

from inventory import InventoryManager, Product


class Injected(RuntimeError):
    pass


def fail_once(obj, method_name):
    """Make obj.method_name raise Injected on its first call, then behave normally
    (so the rollback itself can still use it)."""
    original = getattr(obj, method_name)
    state = {"armed": True}

    def wrapper(*args, **kwargs):
        if state["armed"]:
            state["armed"] = False
            raise Injected(f"{type(obj).__name__}.{method_name}")
        return original(*args, **kwargs)

    setattr(obj, method_name, wrapper)


def build():
    inv = InventoryManager(seed=7)
    inv.add_product(Product("A", "Alpha", "Solo", "10.00", 8, 5))      # only product in "Solo"
    inv.add_product(Product("B", "Bravo", "Shared", "20.00", 2, 4))
    inv.add_product(Product("C", "Charlie", "Shared", "30.00", 9, 1))
    return inv


def snapshot(inv):
    return (
        sorted((p.sku, p.name, p.category, p.price, p.quantity, p.reorder_point)
               for p in (inv.get(s) for s in ("A", "B", "C", "D") if s in inv)),
        [(c, [p.sku for p in inv.products_in_category(c)]) for c in inv.categories()],
        [p.sku for p in inv.products_in_price_range(0, 1000)],
        [p.sku for p in inv.low_stock()],
        [p.sku for p in inv.search_by_name("b")],
        inv.total_value,
        len(inv),
    )


OPERATIONS = {
    "add_product": lambda inv: inv.add_product(Product("D", "Delta", "New", "5.00", 1, 3)),
    "remove_product": lambda inv: inv.remove_product("A"),
    "adjust_quantity": lambda inv: inv.adjust_quantity("A", -6),
    "update_price": lambda inv: inv.update_price("A", "99.99"),
    "change_category": lambda inv: inv.change_category("A", "Shared"),
    "set_reorder_point": lambda inv: inv.set_reorder_point("C", 20),
    "rename_product": lambda inv: inv.rename_product("B", "Bravo Deluxe"),
}

TARGETS = [
    ("_products", "put"), ("_products", "remove"),
    ("_by_price", "insert"), ("_by_price", "remove"),
    ("_categories", "put"), ("_categories", "remove"),
    ("_stock", "push"), ("_stock", "remove"), ("_stock", "update"),
    ("_by_name", "insert"), ("_by_name", "remove"),
    (None, "_change_value"),        # fails after every index has been updated
]


class TestRollback(unittest.TestCase):
    def test_every_failure_point_rolls_back(self):
        injected = 0
        for op_name, op in OPERATIONS.items():
            for owner, method in TARGETS:
                with self.subTest(operation=op_name, fails=f"{owner}.{method}"):
                    inv = build()
                    before = snapshot(inv)
                    fail_once(inv if owner is None else getattr(inv, owner), method)
                    try:
                        op(inv)
                    except Injected:
                        injected += 1
                        self.assertEqual(snapshot(inv), before)
                    self.assertEqual(inv.verify_integrity(), [])
        # Index steps per operation: add 6, remove 6, adjust_quantity 3, update_price 4,
        # change_category 2 (adding to an existing set is not a HashTable call),
        # set_reorder_point 2, rename_product 3.
        self.assertEqual(injected, 26)

    def test_nested_transaction_rolls_back_as_a_whole(self):
        inv = build()
        before = snapshot(inv)
        with self.assertRaises(Injected):
            with inv._atomic():
                inv.adjust_quantity("A", -1)
                inv.update_price("B", "1.00")
                inv.remove_product("C")
                raise Injected("fail after three successful operations")
        self.assertEqual(snapshot(inv), before)
        self.assertEqual(inv.verify_integrity(), [])

    def test_successful_operations_commit(self):
        for op_name, op in OPERATIONS.items():
            with self.subTest(operation=op_name):
                inv = build()
                before = snapshot(inv)
                op(inv)
                self.assertNotEqual(snapshot(inv), before)
                self.assertEqual(inv.verify_integrity(), [])
                self.assertIsNone(inv._tx)          # no transaction left open

    def test_category_bookkeeping(self):
        inv = build()
        inv.change_category("A", "Shared")          # "Solo" becomes empty and is dropped
        inv.add_product(Product("D", "Delta", "New", "5.00", 1, 3))
        self.assertEqual(inv.categories(), ["New", "Shared"])
        self.assertEqual(inv.total_value, Decimal("390.00") + Decimal("5.00"))


class TestVerifyIntegrity(unittest.TestCase):
    def test_detects_corruption(self):
        inv = build()
        self.assertEqual(inv.verify_integrity(), [])
        inv._products.put("B", Product("B", "Bravo", "Shared", "21.00", 2, 4))   # bypass the facade
        problems = inv.verify_integrity()
        self.assertTrue(any("price index" in p for p in problems))
        self.assertTrue(any("total value" in p for p in problems))

    def test_detects_heap_corruption(self):
        inv = build()
        inv._stock._prio[0] = 10_000
        self.assertTrue(inv.verify_integrity())


if __name__ == "__main__":
    unittest.main()
