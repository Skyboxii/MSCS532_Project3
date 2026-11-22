"""
InventoryManager: a facade that keeps four indexes consistent.

    _products   HashTable      sku -> Product            O(1) avg lookup/update
    _by_price   SkipList       (price, sku) -> None      O(log n) price changes,
                                                         O(log n + k) range queries
    _categories HashTable      category -> set of skus   O(1) avg recategorize
    _stock      IndexedMinHeap sku -> quantity - reorder O(log n) quantity changes,
                                                         O(k) low-stock report
    _by_name    SkipList       (name.casefold(), sku)    O(log n + k) prefix search

Every mutating method runs inside a transaction: each index change is
recorded with the action that reverses it, and if anything raises partway
through, the recorded changes are undone in reverse order.  Callers
therefore see either the whole update or none of it.
"""

from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal

from .exceptions import DuplicateSKUError, InsufficientStockError, ProductNotFoundError, ValidationError
from .hash_table import HashTable
from .indexed_heap import IndexedMinHeap
from .product import Product, to_count, to_price
from .skip_list import SkipList


def _name_key(product):
    """Name-index key: case-insensitive name, with the SKU to keep equal names distinct."""
    return (product.name.casefold(), product.sku)


class _UndoLog:
    """The undo actions for the index changes made in one transaction."""

    def __init__(self):
        self._undo = []

    def do(self, apply, undo):
        """Run apply(); remember undo() only if apply() succeeded."""
        apply()
        self._undo.append(undo)

    def rollback(self):
        """Reverse every recorded change, newest first."""
        while self._undo:
            self._undo.pop()()


class InventoryManager:

    def __init__(self, seed=None):
        self._products = HashTable()
        self._by_price = SkipList(seed=seed)
        self._categories = HashTable()
        self._stock = IndexedMinHeap()
        self._by_name = SkipList(seed=None if seed is None else seed + 1)
        self._total_value = Decimal("0")    # running sum of price * quantity
        self._tx = None                     # the active _UndoLog, if any

    def transaction(self):
        """
        Public all-or-nothing block:

            with inventory.transaction():
                inventory.adjust_quantity("A", -2)
                inventory.adjust_quantity("B", +2)

        If anything inside raises, every change made inside is undone.
        """
        return self._atomic()

    @contextmanager
    def _atomic(self):
        """
        All-or-nothing block.  A nested _atomic() joins the outer transaction,
        so a batch wrapped in one _atomic() is rolled back as a whole.
        """
        if self._tx is not None:
            yield self._tx
            return
        self._tx = _UndoLog()
        try:
            yield self._tx
        except BaseException:
            self._tx.rollback()
            raise
        finally:
            self._tx = None

    # -- create / delete ------------------------------------------------------

    def add_product(self, product):
        """Add a new product to every index. O(log n) expected."""
        if not isinstance(product, Product):
            raise ValidationError(f"expected a Product, got {type(product).__name__}")
        if product.sku in self._products:
            raise DuplicateSKUError(product.sku)
        p = product
        with self._atomic() as tx:
            tx.do(lambda: self._products.put(p.sku, p), lambda: self._products.remove(p.sku))
            tx.do(lambda: self._by_price.insert((p.price, p.sku)),
                  lambda: self._by_price.remove((p.price, p.sku)))
            tx.do(lambda: self._category_add(p.category, p.sku),
                  lambda: self._category_discard(p.category, p.sku))
            tx.do(lambda: self._stock.push(p.sku, p.stock_gap), lambda: self._stock.remove(p.sku))
            tx.do(lambda: self._by_name.insert(_name_key(p)), lambda: self._by_name.remove(_name_key(p)))
            self._change_value(tx, p.price * p.quantity)

    def remove_product(self, sku):
        """Remove a product from every index and return it. O(log n) expected."""
        p = self._require(sku)
        with self._atomic() as tx:
            tx.do(lambda: self._products.remove(p.sku), lambda: self._products.put(p.sku, p))
            tx.do(lambda: self._by_price.remove((p.price, p.sku)),
                  lambda: self._by_price.insert((p.price, p.sku)))
            tx.do(lambda: self._category_discard(p.category, p.sku),
                  lambda: self._category_add(p.category, p.sku))
            tx.do(lambda: self._stock.remove(p.sku), lambda: self._stock.push(p.sku, p.stock_gap))
            tx.do(lambda: self._by_name.remove(_name_key(p)), lambda: self._by_name.insert(_name_key(p)))
            self._change_value(tx, -p.price * p.quantity)
        return p

    # -- dynamic updates ------------------------------------------------------

    def adjust_quantity(self, sku, delta):
        """
        Apply a stock movement: positive for receiving, negative for sales.
        Rejects changes that would make quantity negative. O(log n).
        """
        if isinstance(delta, bool) or not isinstance(delta, int):
            raise ValidationError(f"quantity change must be a whole number, got {delta!r}")
        old = self._require(sku)
        if old.quantity + delta < 0:
            raise InsufficientStockError(sku, old.quantity, -delta)
        new = replace(old, quantity=old.quantity + delta)
        with self._atomic() as tx:
            self._replace_record(tx, old, new)
            tx.do(lambda: self._stock.update(sku, new.stock_gap),
                  lambda: self._stock.update(sku, old.stock_gap))
            self._change_value(tx, old.price * delta)
        return new

    def update_price(self, sku, new_price):
        """Change a price and move the product in the price index. O(log n) expected."""
        old = self._require(sku)
        new = replace(old, price=new_price)     # validates the price before anything changes
        with self._atomic() as tx:
            tx.do(lambda: self._by_price.remove((old.price, sku)),
                  lambda: self._by_price.insert((old.price, sku)))
            tx.do(lambda: self._by_price.insert((new.price, sku)),
                  lambda: self._by_price.remove((new.price, sku)))
            self._replace_record(tx, old, new)
            self._change_value(tx, (new.price - old.price) * old.quantity)
        return new

    def change_category(self, sku, new_category):
        """Move a product to another category. O(1) average."""
        old = self._require(sku)
        new = replace(old, category=new_category)
        if new.category == old.category:        # compare after normalization
            return old
        with self._atomic() as tx:
            tx.do(lambda: self._category_discard(old.category, sku),
                  lambda: self._category_add(old.category, sku))
            tx.do(lambda: self._category_add(new.category, sku),
                  lambda: self._category_discard(new.category, sku))
            self._replace_record(tx, old, new)
        return new

    def rename_product(self, sku, new_name):
        """Change a product's display name and move it in the name index. O(log n) expected."""
        old = self._require(sku)
        new = replace(old, name=new_name)
        if new.name == old.name:
            return old
        with self._atomic() as tx:
            tx.do(lambda: self._by_name.remove(_name_key(old)), lambda: self._by_name.insert(_name_key(old)))
            tx.do(lambda: self._by_name.insert(_name_key(new)), lambda: self._by_name.remove(_name_key(new)))
            self._replace_record(tx, old, new)
        return new

    def set_reorder_point(self, sku, reorder_point):
        """Change when a product should be reordered. O(log n)."""
        old = self._require(sku)
        new = replace(old, reorder_point=reorder_point)
        with self._atomic() as tx:
            self._replace_record(tx, old, new)
            tx.do(lambda: self._stock.update(sku, new.stock_gap),
                  lambda: self._stock.update(sku, old.stock_gap))
        return new

    # -- queries --------------------------------------------------------------

    def get(self, sku):
        """Look up a product by SKU. O(1) average."""
        return self._require(sku)

    def products_in_category(self, category):
        """All products in a category, sorted by SKU. O(k log k)."""
        skus = self._categories.get(category, set())
        return [self._products[s] for s in sorted(skus)]

    def categories(self):
        return sorted(self._categories)

    def products_in_price_range(self, low, high):
        """Products with low <= price <= high, cheapest first. O(log n + k)."""
        low, high = to_price(low, "low"), to_price(high, "high")
        if low > high:
            raise ValidationError(f"low ({low}) is greater than high ({high})")
        result = []
        for (price, sku), _ in self._by_price.iter_from((low, "")):
            if price > high:
                break
            result.append(self._products[sku])
        return result

    def search_by_name(self, prefix, limit=None):
        """
        Products whose name starts with prefix (case-insensitive), in name
        order. O(log n + k): one skip-list search to the first match, then a
        walk along the bottom level until the prefix stops matching.
        """
        if not isinstance(prefix, str) or not prefix.strip():
            raise ValidationError("search prefix is required")
        if limit is not None:
            to_count(limit, "limit")
        key = prefix.strip().casefold()
        result = []
        for (name, sku), _ in self._by_name.iter_from((key, "")):
            if not name.startswith(key) or len(result) == limit:
                break
            result.append(self._products[sku])
        return result

    def cheapest(self, k=1):
        """The k lowest-priced products. O(k)."""
        to_count(k, "k")
        result = []
        for (_, sku), _ in self._by_price:
            if len(result) == k:
                break
            result.append(self._products[sku])
        return result

    def low_stock(self):
        """Products at or below their reorder point, most urgent first.
        O(k log k) for k such products, independent of catalog size."""
        hits = sorted(self._stock.items_at_or_below(0), key=lambda e: (e[1], e[0]))
        return [self._products[sku] for sku, _ in hits]

    def most_urgent(self):
        """The product closest to (or furthest below) its reorder point, or
        None if the inventory is empty. O(1)."""
        if not len(self._stock):
            return None
        sku, _ = self._stock.peek()
        return self._products[sku]

    @property
    def total_value(self):
        """Inventory value (sum of price * quantity), maintained in O(1)."""
        return self._total_value

    def __contains__(self, sku):
        return sku in self._products

    def __len__(self):
        return len(self._products)

    # -- integrity ------------------------------------------------------------

    def verify_integrity(self):
        """
        Cross-check every index against the product table. O(n log n).

        Returns a list of problems; an empty list means all indexes agree.
        Used by the tests after every failure-injection scenario, and by the
        CLI's `verify` command.
        """
        problems = []
        products = dict(self._products.items())
        n = len(products)
        if not len(self._by_price) == len(self._stock) == len(self._by_name) == n:
            problems.append(f"index sizes differ: products={n}, price={len(self._by_price)}, "
                            f"stock={len(self._stock)}, name={len(self._by_name)}")
        if {key for key, _ in self._by_name} != {_name_key(p) for p in products.values()}:
            problems.append("name index does not match product names")
        keys = [key for key, _ in self._by_price]
        if any(a >= b for a, b in zip(keys, keys[1:])):
            problems.append("price index is out of order")
        if set(keys) != {(p.price, s) for s, p in products.items()}:
            problems.append("price index does not match product prices")
        member_count = 0
        for category, skus in self._categories.items():
            if not skus:
                problems.append(f"empty category {category!r} was not removed")
            for s in skus:
                member_count += 1
                if s not in products or products[s].category != category:
                    problems.append(f"{s!r} is listed under the wrong category {category!r}")
        if member_count != n:
            problems.append(f"category index lists {member_count} products, expected {n}")
        for s, p in products.items():
            if s not in self._stock or self._stock.priority_of(s) != p.stock_gap:
                problems.append(f"stock index is wrong for {s!r}")
        problems.extend(self._stock.check_invariants())
        expected = sum((p.price * p.quantity for p in products.values()), Decimal("0"))
        if expected != self._total_value:
            problems.append(f"total value is {self._total_value}, expected {expected}")
        return problems

    # -- helpers --------------------------------------------------------------

    def _require(self, sku):
        """Return the product for sku or raise ProductNotFoundError. O(1) average."""
        product = self._products.get(sku) if isinstance(sku, str) else None
        if product is None:
            raise ProductNotFoundError(sku)
        return product

    def _replace_record(self, tx, old, new):
        tx.do(lambda: self._products.put(new.sku, new), lambda: self._products.put(old.sku, old))

    def _change_value(self, tx, delta):
        def add(amount):
            self._total_value += amount
        tx.do(lambda: add(delta), lambda: add(-delta))

    # Each helper below makes exactly ONE mutation.  An undo step is recorded
    # only after its action succeeds, so an action that changed two things
    # and then failed halfway would leave the first change un-undoable.

    def _category_add(self, category, sku):
        skus = self._categories.get(category)
        if skus is None:
            self._categories.put(category, {sku})       # new category, created with its member
        else:
            skus.add(sku)

    def _category_discard(self, category, sku):
        skus = self._categories[category]
        if skus == {sku}:
            self._categories.remove(category)           # last member: drop the whole category
        else:
            skus.discard(sku)
