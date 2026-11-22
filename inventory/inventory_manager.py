"""
InventoryManager: a facade that keeps five indexes consistent.

    _products    dict            sku -> Product              O(1) avg lookup/update
    _by_price    SortedKeyList   (cents, sku)                O(log n) price change,
                                                             O(log n + k) range query
    _by_name     SortedKeyList   (casefolded name, sku)      O(log n + k) prefix search
    _categories  dict            category -> set of skus     O(1) avg recategorize
    _stock       IndexedMinHeap  sku -> quantity - reorder   O(log n) quantity change,
                                                             O(k) low-stock report

Every mutating method runs inside a transaction: each index change is
recorded with the call that reverses it, and if anything raises partway
through, the recorded changes are undone newest first.  Callers see either
the whole update or none of it.

Phase 3 optimizations (each traced to the Phase 2 profile):
  * dict replaces the pure-Python HashTable for the product and category
    tables (same open-addressing design, implemented in C).
  * SortedKeyList replaces the skip lists for the price and name indexes.
  * Records change through Product.evolve(), which validates only the
    changed field, and the indexes use precomputed integer cents.
  * The undo log stores (function, args) tuples and is managed by a small
    class instead of a generator-based context manager.
  * Category listings are cached and invalidated only when membership changes.
  * bulk_load() builds all indexes off to the side in O(n log n) and swaps
    them in, instead of n separate transactional inserts.
"""

import gc
from decimal import Decimal

from .exceptions import (DuplicateSKUError, InsufficientStockError, InventoryError,
                         ProductNotFoundError, ValidationError)
from .indexed_heap import IndexedMinHeap
from .product import Product, fold, to_count, to_price
from .sorted_index import SortedKeyList


class _Atomic:
    """
    Context manager for one all-or-nothing operation.

    The undo log is a list of (function, *args) tuples stored on the manager
    as _tx.  A nested _Atomic joins the outer log, so the outermost block
    owns the rollback; that is what makes transaction() and all-or-nothing
    batches work.  Each logged step must be exactly ONE mutation, recorded
    only after it succeeds (the rule found by the Phase 2 failure-injection
    tests).
    """
    __slots__ = ("inv", "owner")

    def __init__(self, inv):
        self.inv = inv

    def __enter__(self):
        inv = self.inv
        self.owner = inv._tx is None
        if self.owner:
            inv._tx = []
        return inv._tx

    def __exit__(self, exc_type, exc, tb):
        if self.owner:
            log, self.inv._tx = self.inv._tx, None
            if exc_type is not None:
                for step in reversed(log):
                    step[0](*step[1:])
        return False


def _prefix_end(key):
    """Smallest string greater than every string that starts with key (None = no bound)."""
    while key:
        last = ord(key[-1])
        if last < 0x10FFFF:
            return key[:-1] + chr(last + 1)
        key = key[:-1]
    return None


class InventoryManager:

    def __init__(self, seed=None):
        # seed is accepted for compatibility with Phase 2 (it seeded the skip lists);
        # the Phase 3 indexes are deterministic.
        self._products = {}
        self._by_price = SortedKeyList()
        self._by_name = SortedKeyList()
        self._categories = {}
        self._stock = IndexedMinHeap()
        self._total_cents = 0               # running sum of cents * quantity
        self._tx = None                     # the active undo log, if any
        self._cat_order = {}                # cache: category -> tuple of SKUs in order
        self._cat_names = None              # cache: sorted category names

    def transaction(self):
        """
        Public all-or-nothing block:

            with inventory.transaction():
                inventory.adjust_quantity("A", -2)
                inventory.adjust_quantity("B", +2)

        If anything inside raises, every change made inside is undone.
        """
        return _Atomic(self)

    _atomic = transaction                   # Phase 2 name, kept for internal callers and tests

    # -- create / delete ------------------------------------------------------

    def add_product(self, product):
        """Add a new product to every index. O(log n)."""
        if not isinstance(product, Product):
            raise ValidationError(f"expected a Product, got {type(product).__name__}")
        p, sku = product, product.sku
        if sku in self._products:
            raise DuplicateSKUError(sku)
        price_key, name_key, value = (p.cents, sku), (p.name_key, sku), p.cents * p.quantity
        with _Atomic(self) as log:
            self._products[sku] = p
            log.append((self._products.pop, sku))
            self._by_price.insert(price_key)
            log.append((self._by_price.remove, price_key))
            self._category_add(p.category, sku)
            log.append((self._category_discard, p.category, sku))
            self._stock.push(sku, p.stock_gap)
            log.append((self._stock.remove, sku))
            self._by_name.insert(name_key)
            log.append((self._by_name.remove, name_key))
            self._add_cents(value)
            log.append((self._add_cents, -value))

    def remove_product(self, sku):
        """Remove a product from every index and return it. O(log n)."""
        p = self._require(sku)
        price_key, name_key, value = (p.cents, sku), (p.name_key, sku), p.cents * p.quantity
        with _Atomic(self) as log:
            self._products.pop(sku)
            log.append((self._products.__setitem__, sku, p))
            self._by_price.remove(price_key)
            log.append((self._by_price.insert, price_key))
            self._category_discard(p.category, sku)
            log.append((self._category_add, p.category, sku))
            self._stock.remove(sku)
            log.append((self._stock.push, sku, p.stock_gap))
            self._by_name.remove(name_key)
            log.append((self._by_name.insert, name_key))
            self._add_cents(-value)
            log.append((self._add_cents, value))
        return p

    def bulk_load(self, products):
        """
        Add many products at once. O((n + m) log(n + m)) for m new products.

        Instead of m separate transactional inserts, this validates the whole
        batch, builds complete new indexes off to the side (sorted indexes from
        one sort, the heap with Floyd's O(n) heapify), and only then swaps them
        in.  If anything fails before the swap, the inventory is untouched, so
        no undo log is needed (the same idea as shadow paging; Lorie, 1977).
        """
        if self._tx is not None:
            raise InventoryError("bulk_load cannot run inside a transaction")
        try:
            new = list(products)
        except TypeError:
            raise ValidationError("bulk_load needs an iterable of Products") from None
        seen = set(self._products)
        for p in new:
            if not isinstance(p, Product):
                raise ValidationError(f"expected a Product, got {type(p).__name__}")
            if p.sku in seen:
                raise DuplicateSKUError(p.sku)
            seen.add(p.sku)
        everything = list(self._products.values()) + new

        # Pause Python's cyclic garbage collector while building millions of
        # objects.  Stress tests showed that otherwise the collector defers a
        # full pass over them to whatever runs next: at 4,000,000 products the
        # first 5,000 updates after a load averaged 107 us instead of 4 us.
        # The indexes contain no reference cycles, so nothing is lost by
        # waiting; reference counting still frees ordinary garbage at once.
        collector_was_on = gc.isenabled()
        gc.disable()
        try:
            products = {p.sku: p for p in everything}
            by_price = SortedKeyList((p.cents, p.sku) for p in everything)
            by_name = SortedKeyList((p.name_key, p.sku) for p in everything)
            categories = {}
            for p in everything:
                categories.setdefault(p.category, set()).add(p.sku)
            stock = IndexedMinHeap.from_items((p.sku, p.stock_gap) for p in everything)
            total = sum(p.cents * p.quantity for p in everything)
        finally:
            if collector_was_on:
                gc.enable()

        (self._products, self._by_price, self._by_name, self._categories, self._stock,
         self._total_cents) = products, by_price, by_name, categories, stock, total
        self._cat_order, self._cat_names = {}, None
        return len(new)

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
        new = old.evolve(quantity=old.quantity + delta)
        value = old.cents * delta
        with _Atomic(self) as log:
            self._products[sku] = new
            log.append((self._products.__setitem__, sku, old))
            self._stock.update(sku, new.stock_gap)
            log.append((self._stock.update, sku, old.stock_gap))
            self._add_cents(value)
            log.append((self._add_cents, -value))
        return new

    def update_price(self, sku, new_price):
        """Change a price and move the product in the price index. O(log n)."""
        old = self._require(sku)
        new = old.evolve(price=new_price)       # validates the price before anything changes
        old_key, new_key = (old.cents, sku), (new.cents, sku)
        value = (new.cents - old.cents) * old.quantity
        with _Atomic(self) as log:
            self._by_price.remove(old_key)
            log.append((self._by_price.insert, old_key))
            self._by_price.insert(new_key)
            log.append((self._by_price.remove, new_key))
            self._products[sku] = new
            log.append((self._products.__setitem__, sku, old))
            self._add_cents(value)
            log.append((self._add_cents, -value))
        return new

    def change_category(self, sku, new_category):
        """Move a product to another category. O(1) average."""
        old = self._require(sku)
        new = old.evolve(category=new_category)
        if new.category == old.category:        # compare after normalization
            return old
        with _Atomic(self) as log:
            self._category_discard(old.category, sku)
            log.append((self._category_add, old.category, sku))
            self._category_add(new.category, sku)
            log.append((self._category_discard, new.category, sku))
            self._products[sku] = new
            log.append((self._products.__setitem__, sku, old))
        return new

    def rename_product(self, sku, new_name):
        """Change a product's display name and move it in the name index. O(log n)."""
        old = self._require(sku)
        new = old.evolve(name=new_name)
        if new.name == old.name:
            return old
        old_key, new_key = (old.name_key, sku), (new.name_key, sku)
        with _Atomic(self) as log:
            self._by_name.remove(old_key)
            log.append((self._by_name.insert, old_key))
            self._by_name.insert(new_key)
            log.append((self._by_name.remove, new_key))
            self._products[sku] = new
            log.append((self._products.__setitem__, sku, old))
        return new

    def set_reorder_point(self, sku, reorder_point):
        """Change when a product should be reordered. O(log n)."""
        old = self._require(sku)
        new = old.evolve(reorder_point=reorder_point)
        with _Atomic(self) as log:
            self._products[sku] = new
            log.append((self._products.__setitem__, sku, old))
            self._stock.update(sku, new.stock_gap)
            log.append((self._stock.update, sku, old.stock_gap))
        return new

    # -- queries --------------------------------------------------------------

    def get(self, sku):
        """Look up a product by SKU. O(1) average."""
        return self._require(sku)

    def products_in_category(self, category):
        """
        All products in a category, sorted by SKU.  The sorted SKU order is
        cached per category and dropped only when the category's membership
        changes, so repeated listings cost O(k) instead of O(k log k).
        """
        if not isinstance(category, str):
            raise ValidationError(f"category must be text, got {type(category).__name__}")
        order = self._cat_order.get(category)
        if order is None:
            skus = self._categories.get(category)
            if not skus:
                return []
            order = self._cat_order[category] = tuple(sorted(skus))
        products = self._products
        return [products[s] for s in order]

    def categories(self):
        if self._cat_names is None:
            self._cat_names = sorted(self._categories)
        return list(self._cat_names)            # a copy, so callers cannot edit the cache

    def products_in_price_range(self, low, high):
        """Products with low <= price <= high, cheapest first. O(log n + k)."""
        low, high = to_price(low, "low"), to_price(high, "high")
        if low > high:
            raise ValidationError(f"low ({low}) is greater than high ({high})")
        keys = self._by_price.range((int(low * 100), ""), (int(high * 100) + 1, ""))
        products = self._products
        return [products[sku] for _, sku in keys]

    def search_by_name(self, prefix, limit=None):
        """
        Products whose name starts with prefix (case-insensitive), in name
        order. O(log n + k): two binary searches bound the matching run.
        """
        if not isinstance(prefix, str) or not prefix.strip():
            raise ValidationError("search prefix is required")
        if limit is not None:
            to_count(limit, "limit")
        key = fold(prefix.strip())
        end = _prefix_end(key)
        keys = self._by_name.range((key, ""), None if end is None else (end, ""), limit)
        products = self._products
        return [products[sku] for _, sku in keys]

    def cheapest(self, k=1):
        """The k lowest-priced products. O(k)."""
        k = min(to_count(k, "k"), len(self))       # huge k would overflow islice
        products = self._products
        return [products[sku] for _, sku in self._by_price.first(k)]

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
        """Inventory value (sum of price * quantity), maintained in O(1) as integer cents."""
        return Decimal(self._total_cents).scaleb(-2)

    def __contains__(self, sku):
        return sku in self._products

    def __len__(self):
        return len(self._products)

    # -- integrity ------------------------------------------------------------

    def verify_integrity(self):
        """
        Cross-check every index, cache, and derived value against the
        product table. O(n log n).  Returns a list of problems; empty = OK.
        """
        problems = []
        products = self._products
        n = len(products)
        if not len(self._by_price) == len(self._stock) == len(self._by_name) == n:
            problems.append(f"index sizes differ: products={n}, price={len(self._by_price)}, "
                            f"stock={len(self._stock)}, name={len(self._by_name)}")
        for s, p in products.items():
            if p.sku != s or p.cents != int(p.price * 100) or p.name_key != fold(p.name):
                problems.append(f"derived fields are wrong for {s!r}")
        if set(self._by_price) != {(p.cents, s) for s, p in products.items()}:
            problems.append("price index does not match product prices")
        if set(self._by_name) != {(p.name_key, s) for s, p in products.items()}:
            problems.append("name index does not match product names")
        problems.extend(self._by_price.check_invariants())
        problems.extend(self._by_name.check_invariants())
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
        for category, order in self._cat_order.items():
            if order != tuple(sorted(self._categories.get(category, ()))):
                problems.append(f"cached order for {category!r} is stale")
        if self._cat_names is not None and self._cat_names != sorted(self._categories):
            problems.append("cached category list is stale")
        for s, p in products.items():
            if s not in self._stock or self._stock.priority_of(s) != p.stock_gap:
                problems.append(f"stock index is wrong for {s!r}")
        problems.extend(self._stock.check_invariants())
        expected = sum(p.cents * p.quantity for p in products.values())
        if expected != self._total_cents:
            problems.append(f"total value is {self.total_value}, expected {Decimal(expected).scaleb(-2)}")
        return problems

    # -- helpers --------------------------------------------------------------

    def _require(self, sku):
        """Return the product for sku or raise ProductNotFoundError. O(1) average."""
        product = self._products.get(sku) if isinstance(sku, str) else None
        if product is None:
            raise ProductNotFoundError(sku)
        return product

    def _add_cents(self, delta):
        self._total_cents += delta

    # Each helper below makes exactly ONE change to index state (cache
    # invalidation is not state: a dropped cache entry is simply rebuilt).

    def _category_add(self, category, sku):
        self._cat_order.pop(category, None)
        skus = self._categories.get(category)
        if skus is None:
            self._cat_names = None
            self._categories[category] = {sku}          # new category, created with its member
        else:
            skus.add(sku)

    def _category_discard(self, category, sku):
        self._cat_order.pop(category, None)
        skus = self._categories[category]
        if skus == {sku}:
            self._cat_names = None
            self._categories.pop(category)              # last member: drop the whole category
        else:
            skus.discard(sku)
