# MSCS 532 Project – Dynamic Inventory Management (Phase 3: Optimization and Scaling)

An in-memory inventory system that handles constant changes to product
quantities, prices, categories, and names. Phase 1 designed the data
structures, Phase 2 built a tested proof of concept, and Phase 3 (this
deliverable) profiles that proof of concept, optimizes it, scales it to
4,000,000 products, and proves the optimized version gives the same answers.
The full write-up is in [REPORT.md](REPORT.md).

Earlier phases: [Phase 1](https://github.com/Skyboxii/MSCS532_Project) ·
[Phase 2](https://github.com/Skyboxii/MSCS532_Project2). This repository
continues their commit history.

## Results at a glance (1,000,000 products)

| Operation | Phase 2 (µs) | Phase 3 (µs) | Speedup |
|-----------|-------------:|-------------:|--------:|
| Lookup by SKU | 0.27 | 0.10 | 2.7× |
| Stock update | 7.00 | 2.69 | 2.6× |
| Price change | 18.24 | 8.11 | 2.2× |
| Category move | 3.16 | 1.31 | 2.4× |
| Price range (≈10 results) | 29.82 | 12.33 | 2.4× |
| Name search (10 results) | 5.67 | 3.37 | 1.7× |
| Low-stock report (20 items) | 10.13 | 5.15 | 2.0× |
| Category listing (20,000 items) | 14,371 | 3,895 | 3.7× |
| Add + remove a product | 35.27 | 10.77 | 3.3× |

| Metric | Phase 2 | Phase 3 |
|--------|--------:|--------:|
| Load 1,000,000 products | 29.8 s | 3.6 s (`bulk_load`) |
| Memory per product (1,000,000 products) | 1,105 B | 700 B |
| Mixed store-day workload (100,000 products) | 90,641 ops/s | 186,428 ops/s |
| Failed lookup after delete-heavy churn (own hash table) | 804 slots avg | 6 slots avg |

Charts: [per-operation speedup](results/speedup_1m.png),
[operations vs. catalog size](results/operations_vs_n.png),
[load time and memory](results/load_and_memory.png),
[delete-heavy churn](results/churn.png).

## What Phase 3 changed, and why

Every change traces to the Phase 2 profile (`results/profile_phase2_inventory.txt`).

| Optimization | Phase 2 bottleneck |
|--------------|--------------------|
| `Product.evolve()` validates only the changed field | `dataclasses.replace()` re-validated every field on every update (23% of time) |
| Built-in `dict` for the product table, categories, and heap positions | pure-Python hash probing (18% of time) |
| `SortedKeyList` (blocked sorted arrays, C-level `bisect`) replaces the skip lists | pointer chasing on every price change, range query, and name search |
| Undo log of `(function, args)` tuples with a class-based context manager | two closures and a generator per step (21% of time) |
| Cached category order and category names, invalidated only on membership change | every listing re-sorted its category |
| `__slots__`, cached cents and search keys, shared (flyweight) prices, interned categories, two-array heap | per-object dictionaries and lists |
| `bulk_load()`: shadow build, one sort, O(n) heapify, collector paused, then swap | one full transaction per product |
| Backward-shift deletion in the from-scratch `HashTable` | tombstones lengthened every failed lookup |

## Layout

```
inventory/
    inventory_manager.py  InventoryManager facade: dict tables, sorted indexes, undo log, caches, bulk_load
    sorted_index.py       SortedKeyList (new in Phase 3)
    indexed_heap.py       IndexedMinHeap on parallel arrays with O(n) heapify
    hash_table.py         from-scratch HashTable with backward-shift deletion
    product.py            Product record, validators, flyweight prices
    batch.py              CSV import (bulk or incremental) and stock movements
    skip_list.py          Phase 1/2 skip list (kept for reference; no longer used)
    exceptions.py         InventoryError hierarchy
cli.py, demo.py           interactive shell and scripted demonstration
benchmarks/
    phase2_inventory/     frozen copy of the Phase 2 package (the "before" side)
    workload.py           shared synthetic catalog and store-day workload
    profile_phase2.py     cProfile of either package
    compare.py            Phase 2 vs. Phase 3: operations, load time, memory, mixed workload
    churn.py              delete-heavy hash-table test
    stress_test.py        1M / 2M / 4M products: timings, memory, 100k-update rollback, integrity
    gc_check.py           garbage-collector pause after bulk loads
results/                  CSVs, console output, and charts from every benchmark
tests/                    87 unit tests
```

## Usage

Requires Python 3.10+ for `__slots__` on the dataclass (it runs on 3.8+ without
the memory saving). The package uses only the standard library; the
benchmarks' charts need `matplotlib`.

```bash
python demo.py                          # scripted walkthrough (same transcript as Phase 2)
python cli.py data/products.csv         # interactive shell
python -m unittest -v                   # 87 tests, ~2 s
python benchmarks/compare.py            # ~12 min; Phase 2 vs. Phase 3
python benchmarks/compare.py replot     # redraw charts from saved CSVs
python benchmarks/churn.py              # ~1 min
python benchmarks/stress_test.py        # ~4 min, ~3 GB RAM
python benchmarks/gc_check.py           # ~2 min
python benchmarks/profile_phase2.py [phase2_inventory|inventory]
```

```python
from inventory import InventoryManager, Product

inv = InventoryManager()
inv.bulk_load(Product(f"SKU-{i:07}", f"Item {i}", "Office", "4.99", 50, 10) for i in range(1_000_000))
inv.adjust_quantity("SKU-0000042", -3)
inv.products_in_price_range("4.00", "5.00")[:5]
with inv.transaction():                  # all-or-nothing
    inv.update_price("SKU-0000001", "3.99")
    inv.change_category("SKU-0000001", "Clearance")
```

## Testing

| Test file | Tests | Covers |
|-----------|------:|--------|
| `test_differential.py` | 2 | 18,000 random operations (a third invalid) must match Phase 2 exactly, results and error messages |
| `test_stress.py` | 15 | unexpected input on every public method, extreme data shapes, bulk load, cache invalidation |
| `test_phase3_structures.py` | 8 | backward-shift deletion, `SortedKeyList`, array heap vs. reference models |
| `test_transactions.py` | 6 | 26 injected mid-update failures, each fully rolled back |
| Phase 1–2 suites | 56 | structures, manager, validation, search, batch, CLI |

Stress testing found five ways a raw Python error could escape (most present
since Phase 2): prices near 1e26 crashed `Decimal.quantize`, `"-0.00"` was
stored as negative zero, and three methods raised `TypeError` on wrong argument
types. All now raise `InventoryError` subclasses. At 1, 2, and 4 million
products every index passes `verify_integrity()`, and an aborted transaction of
100,000 updates restores the inventory exactly.

## Limitations and next steps

- Price-range queries still slow as the catalog grows (12 µs at 1M, 32 µs at 4M):
  each result is fetched from a different place in memory.
- Single-threaded and in-memory only (about 2.5 GB at 4M products).
- Next: a transaction log for durability, locking for concurrent registers, and a
  compiled extension for the hottest paths.
