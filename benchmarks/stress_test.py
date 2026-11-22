"""
Stress test of the Phase 3 implementation at 1, 2, and 4 million products.

For each size: bulk-load the catalog, time each core operation, run a
100,000-operation mixed workload, verify every index, and abort a single
transaction of 100,000 updates to check that rollback works at scale.

Usage (from the repository root):  python benchmarks/stress_test.py    (~4 minutes, ~4 GB RAM)
"""

import csv
import ctypes
import gc
import json
import os
import random
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, os.pardir)
sys.path[:0] = [HERE, ROOT]

import inventory as p3                                           # noqa: E402
from bench_util import stabilize_timing                          # noqa: E402
from workload import WORDS, make_products, price_window, run_mixed   # noqa: E402

SIZES = [1_000_000, 2_000_000, 4_000_000]
OUT = os.path.join(ROOT, "results")


def process_mb():
    """Current working set of this process in MB (Windows); None elsewhere."""
    if sys.platform != "win32":
        return None

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + \
                   [(name, ctypes.c_size_t) for name in (
                       "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                       "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                       "PagefileUsage", "PeakPagefileUsage")]

    c = Counters()
    c.cb = ctypes.sizeof(c)
    psapi = ctypes.WinDLL("psapi")
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo(ctypes.c_void_p(kernel32.GetCurrentProcess()), ctypes.byref(c), c.cb)
    return c.WorkingSetSize / 2**20


def per_op_us(fn, args):
    start = time.perf_counter()
    for a in args:
        fn(a)
    return (time.perf_counter() - start) / len(args) * 1e6


class Abort(Exception):
    pass


def stress(n):
    gc.collect()
    base_mb = process_mb()
    t = time.perf_counter()
    products = make_products(p3, n, seed=n)
    gen_s = time.perf_counter() - t
    inv = p3.InventoryManager()
    t = time.perf_counter()
    inv.bulk_load(products)
    bulk_s = time.perf_counter() - t
    loaded_mb = process_mb()
    del products                                        # the inventory now owns the records

    rng = random.Random(n)
    skus = [f"SKU-{rng.randrange(n):07}" for _ in range(5000)]
    width = price_window(n)
    lows = [rng.randint(100, 99_999 - int(width * 100)) / 100 for _ in range(5000)]
    row = {
        "n": n, "generate_s": gen_s, "bulk_load_s": bulk_s,
        "memory_mb": None if base_mb is None else loaded_mb - base_mb,
        "get_us": per_op_us(inv.get, skus),
        "adjust_quantity_us": per_op_us(lambda s: inv.adjust_quantity(s, 1), skus),
        "update_price_us": per_op_us(lambda s: inv.update_price(s, f"{rng.randint(100, 99_999) / 100:.2f}"),
                                     skus),
        "price_range_us": per_op_us(lambda lo: inv.products_in_price_range(f"{lo:.2f}", f"{lo + width:.2f}"),
                                    lows),
        "name_search_us": per_op_us(lambda w: inv.search_by_name(w, limit=10),
                                    [rng.choice(WORDS) for _ in range(5000)]),
        "low_stock_us": per_op_us(lambda _: inv.low_stock(), range(500)),
    }
    row["memory_bytes_per_product"] = None if row["memory_mb"] is None else row["memory_mb"] * 2**20 / n

    _, seconds = run_mixed(p3, inv, 100_000, seed=n)
    row["mixed_ops_per_s"] = 100_000 / seconds

    # One transaction of 100,000 updates, aborted at the end: everything must come back.
    # (The mixed workload removed some products, so only use SKUs that still exist.)
    value_before, sample = inv.total_value, {s: inv.get(s) for s in skus[:1000] if s in inv}
    targets = [s for s in (f"SKU-{i:07}" for i in range(n)) if s in inv][:100_000]
    t = time.perf_counter()
    try:
        with inv.transaction():
            for s in targets:
                inv.adjust_quantity(s, 7)
            raise Abort
    except Abort:
        pass
    row["abort_100k_s"] = time.perf_counter() - t
    row["rollback_ok"] = (inv.total_value == value_before and
                          all(inv.get(s) is p for s, p in sample.items()))

    t = time.perf_counter()
    problems = inv.verify_integrity()
    row["verify_s"] = time.perf_counter() - t
    row["integrity_ok"] = not problems
    del inv
    gc.collect()
    return row


if __name__ == "__main__":
    # Each size runs in its own process, so memory freed by one size cannot be
    # reused by the next and distort its measurement.
    if len(sys.argv) == 2:
        stabilize_timing()
        print(json.dumps(stress(int(sys.argv[1]))), flush=True)
        sys.exit()
    rows = []
    for n in SIZES:
        out = subprocess.run([sys.executable, __file__, str(n)], check=True, capture_output=True,
                             text=True).stdout
        row = json.loads(out.strip().splitlines()[-1])
        rows.append(row)
        print(", ".join(f"{k}={v:,.2f}" if isinstance(v, float) else f"{k}={v}" for k, v in row.items()),
              flush=True)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "stress.csv"), "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        for r in rows:
            writer.writerow({k: round(v, 3) if isinstance(v, float) else v for k, v in r.items()})
