"""
Does Python's cyclic garbage collector slow bulk loading at scale?

Runs one configuration per process (so collector state cannot leak between
trials): load n products with bulk_load, with the collector either running
normally or paused during the load, then time the first 5,000 updates.

Usage (from the repository root):  python benchmarks/gc_check.py
"""

import gc
import os
import random
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, os.pardir)


def trial(n, pause):
    sys.path[:0] = [HERE, ROOT]
    import inventory as p3
    from bench_util import stabilize_timing
    from workload import make_products
    stabilize_timing()
    products = make_products(p3, n, seed=5)
    inv = p3.InventoryManager()
    gc.collect()
    start = time.perf_counter()
    if pause:
        gc.disable()
    inv.bulk_load(products)
    if pause:
        gc.enable()
    load = time.perf_counter() - start
    rng = random.Random(1)
    skus = [f"SKU-{rng.randrange(n):07}" for _ in range(5000)]
    start = time.perf_counter()
    for s in skus:
        inv.adjust_quantity(s, 1)
    first = (time.perf_counter() - start) / 5000 * 1e6
    start = time.perf_counter()
    for s in skus:
        inv.adjust_quantity(s, 1)
    second = (time.perf_counter() - start) / 5000 * 1e6
    print(f"{n},{'paused' if pause else 'normal'},{load:.2f},{first:.2f},{second:.2f}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 3:
        trial(int(sys.argv[1]), sys.argv[2] == "paused")
        sys.exit()
    print("n,collector,bulk_load_s,first_5000_updates_us,next_5000_updates_us", flush=True)
    for n in (1_000_000, 4_000_000):
        for mode in ("normal", "paused"):
            subprocess.run([sys.executable, __file__, str(n), mode], check=True)
