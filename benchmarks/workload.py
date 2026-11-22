"""
Shared synthetic data and workload for the Phase 3 benchmarks and profiler.

Both the Phase 2 baseline and the Phase 3 implementation expose the same
InventoryManager API, so every function here takes the package module
(`inventory` or `phase2_inventory`) as an argument and runs identical work
against either one.
"""

import random
import time

WORDS = ["Desk", "Laptop", "Cable", "Mouse", "Chair", "Lamp", "Paper", "Pen", "Mug", "Bottle",
         "Hoodie", "Monitor", "Stapler", "Tape", "Shelf", "Cap", "Keyboard", "Hub", "Folder", "Map"]
CATEGORIES = [f"Category {i:02}" for i in range(50)]
LOW_STOCK_ITEMS = 20

# A store day: mostly sales and receipts, some price changes and lookups,
# occasional searches, reports, and catalog changes.
MIX = [("sell", 40), ("receive", 15), ("get", 15), ("price", 10), ("move", 5), ("range", 5),
       ("search", 5), ("low_stock", 3), ("add", 1), ("remove", 1)]


def make_products(pkg, n, seed=0, start=0):
    """n valid products; exactly LOW_STOCK_ITEMS of them start at or below their reorder point."""
    rng = random.Random(seed)
    products = []
    for i in range(start, start + n):
        low = i < LOW_STOCK_ITEMS
        products.append(pkg.Product(
            sku=f"SKU-{i:07}",
            name=f"{rng.choice(WORDS)} {rng.choice(WORDS)} {i}",
            category=rng.choice(CATEGORIES),
            price=f"{rng.randint(100, 99_999) / 100:.2f}",
            quantity=rng.randint(0, 5) if low else rng.randint(50, 500),
            reorder_point=10 if low else 0))
    return products


def build(pkg, n, seed=0):
    inv = pkg.InventoryManager(seed=seed)
    for p in make_products(pkg, n, seed):
        inv.add_product(p)
    return inv


def price_window(n):
    """Width in dollars of a price range that holds about 10 products at catalog size n."""
    return max(1, round(10 * 99_900 / n)) / 100


def run_mixed(pkg, inv, ops, seed=1):
    """
    Apply `ops` operations drawn from MIX.  Returns (checksum, seconds): the
    checksum lets runs on different packages be compared, and seconds times
    only the operations (listing the catalog's SKUs beforehand is setup).
    """
    rng = random.Random(seed)
    names, weights = zip(*MIX)
    skus = [s for s in _all_skus(inv)]
    next_id = 10_000_000
    width = price_window(max(len(inv), 1))
    checksum = 0
    plan = rng.choices(names, weights, k=ops)
    start = time.perf_counter()
    for op in plan:
        sku = skus[rng.randrange(len(skus))]
        if op == "sell":
            q = inv.get(sku).quantity
            if q:
                checksum += inv.adjust_quantity(sku, -min(q, rng.randint(1, 3))).quantity
        elif op == "receive":
            checksum += inv.adjust_quantity(sku, rng.randint(1, 50)).quantity
        elif op == "get":
            checksum += inv.get(sku).quantity
        elif op == "price":
            inv.update_price(sku, f"{rng.randint(100, 99_999) / 100:.2f}")
        elif op == "move":
            inv.change_category(sku, rng.choice(CATEGORIES))
        elif op == "range":
            lo = rng.randint(100, 99_999 - int(width * 100)) / 100
            checksum += len(inv.products_in_price_range(f"{lo:.2f}", f"{lo + width:.2f}"))
        elif op == "search":
            checksum += len(inv.search_by_name(rng.choice(WORDS), limit=10))
        elif op == "low_stock":
            checksum += len(inv.low_stock())
        elif op == "add":
            p = make_products(pkg, 1, seed=next_id, start=next_id)[0]
            inv.add_product(p)
            skus.append(p.sku)
            next_id += 1
        elif op == "remove" and len(skus) > 1:
            i = rng.randrange(len(skus))
            skus[i], skus[-1] = skus[-1], skus[i]       # O(1) removal from the driver's list;
            inv.remove_product(skus.pop())              # pop(i) would shift millions of entries
    return checksum, time.perf_counter() - start


def _all_skus(inv):
    return sorted(p.sku for c in inv.categories() for p in inv.products_in_category(c))
