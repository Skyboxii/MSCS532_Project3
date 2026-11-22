"""
Phase 2 (proof of concept) versus Phase 3 (optimized), on identical data.

Experiment 1  per-operation time at n = 1k, 10k, 100k, 1M products
Experiment 2  time to load n products (one at a time vs. bulk_load)
Experiment 3  memory per product at n = 100,000 and 1,000,000 (records + indexes)
Experiment 4  mixed "store day" workload throughput at n = 100,000, with a
              checksum to confirm both versions computed the same results

Usage (from the repository root):  python benchmarks/compare.py          (~12 minutes)
                                   python benchmarks/compare.py replot   (charts only)
Writes results/compare_*.csv and results/*.png.
"""

import csv
import gc
import os
import random
import sys
import time
import tracemalloc

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, os.pardir)
sys.path[:0] = [HERE, ROOT]

import matplotlib                                                # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                                  # noqa: E402

import inventory as p3                                           # noqa: E402
import phase2_inventory as p2                                    # noqa: E402
from bench_util import stabilize_timing                          # noqa: E402
from workload import CATEGORIES, WORDS, make_products, price_window, run_mixed   # noqa: E402

SIZES = [1_000, 10_000, 100_000, 1_000_000]
VERSIONS = {"Phase 2": p2, "Phase 3": p3}
OPS = 2_000
OUT = os.path.join(ROOT, "results")
COLORS = {"Phase 2": "#d62728", "Phase 3": "#1f77b4", "Phase 3 bulk_load": "#2ca02c"}


def best_us(fn, args, rounds=3):
    """Best-of-rounds average microseconds per call."""
    best = float("inf")
    for _ in range(rounds):
        start = time.perf_counter()
        for a in args:
            fn(a)
        best = min(best, (time.perf_counter() - start) / len(args) * 1e6)
    return best


def load(pkg, products, bulk):
    inv = pkg.InventoryManager(seed=1)
    start = time.perf_counter()
    if bulk:
        inv.bulk_load(products)
    else:
        for p in products:
            inv.add_product(p)
    return inv, time.perf_counter() - start


def experiment_operations():
    print("\n=== Experiment 1 and 2: per-operation time and load time ===")
    op_rows, load_rows = [], []
    for n in SIZES:
        for version, pkg in VERSIONS.items():
            products = make_products(pkg, n, seed=n)
            inv, seconds = load(pkg, products, bulk=False)
            load_rows.append({"version": version, "n": n, "seconds": seconds})
            if pkg is p3:
                _, bulk_s = load(pkg, products, bulk=True)
                load_rows.append({"version": "Phase 3 bulk_load", "n": n, "seconds": bulk_s})

            rng = random.Random(n)
            skus = [rng.choice(products).sku for _ in range(OPS)]
            width = price_window(n)
            lows = [rng.randint(100, 99_999 - int(width * 100)) / 100 for _ in range(OPS)]
            ranges = [(f"{lo:.2f}", f"{lo + width:.2f}") for lo in lows]
            new = make_products(pkg, OPS, seed=-n, start=n + 10)
            cats = [rng.choice(CATEGORIES) for _ in range(200)]
            words = [rng.choice(WORDS) for _ in range(OPS)]
            prices = [f"{rng.randint(100, 99_999) / 100:.2f}" for _ in range(OPS)]
            price_args = list(zip(skus, prices))
            move_args = list(zip(skus, [rng.choice(CATEGORIES) for _ in range(OPS)]))

            def add_remove(p, inv=inv):
                inv.add_product(p)
                inv.remove_product(p.sku)

            timings = {
                "get": best_us(inv.get, skus),
                "adjust_quantity": best_us(lambda s: inv.adjust_quantity(s, 1), skus),
                "update_price": best_us(lambda a: inv.update_price(*a), price_args),
                "change_category": best_us(lambda a: inv.change_category(*a), move_args),
                "price_range (k≈10)": best_us(lambda r: inv.products_in_price_range(*r), ranges),
                "name_search (10)": best_us(lambda w: inv.search_by_name(w, limit=10), words),
                "low_stock (20)": best_us(lambda _: inv.low_stock(), range(200)),
                "category listing": best_us(inv.products_in_category, cats, rounds=2),
                "add + remove": best_us(add_remove, new),
            }
            for op, us in timings.items():
                op_rows.append({"version": version, "n": n, "operation": op, "us": round(us, 3)})
            print(f"{version} n={n:>9,}  load {seconds:6.2f} s  " +
                  "  ".join(f"{op.split(' ')[0]}={us:.2f}" for op, us in timings.items()))
            del inv, products
            gc.collect()
    return op_rows, load_rows


def experiment_memory():
    rows = []
    for n in (100_000, 1_000_000):
        print(f"\n=== Experiment 3: memory at n = {n:,} ===")
        rows.extend(memory_at(n))
    return rows


def memory_at(n):
    rows = []
    for version, pkg in VERSIONS.items():
        if pkg is p3:
            p3.product._PRICES.clear()          # count the flyweight cache as part of the cost
        gc.collect()
        tracemalloc.start()
        products = make_products(pkg, n, seed=7)
        records = tracemalloc.get_traced_memory()[0]
        inv = pkg.InventoryManager(seed=1)
        if pkg is p3:
            inv.bulk_load(products)
        else:
            for p in products:
                inv.add_product(p)
        total = tracemalloc.get_traced_memory()[0]
        tracemalloc.stop()
        row = {"version": version, "n": n, "records_bytes_per_product": records / n,
               "index_bytes_per_product": (total - records) / n, "total_bytes_per_product": total / n}
        rows.append(row)
        print(f"{version}: records {row['records_bytes_per_product']:.0f} B, indexes "
              f"{row['index_bytes_per_product']:.0f} B, total {row['total_bytes_per_product']:.0f} B per product")
        del inv, products
    return rows


def experiment_mixed(n=100_000, ops=50_000):
    print(f"\n=== Experiment 4: mixed workload, {ops:,} operations on {n:,} products ===")
    rows = []
    for version, pkg in VERSIONS.items():
        inv, _ = load(pkg, make_products(pkg, n, seed=3), bulk=False)
        checksum, seconds = run_mixed(pkg, inv, ops, seed=11)
        rows.append({"version": version, "n": n, "ops": ops, "seconds": seconds,
                     "ops_per_second": ops / seconds, "checksum": checksum,
                     "integrity_ok": not inv.verify_integrity()})
        print(f"{version}: {seconds:.2f} s, {ops / seconds:,.0f} ops/s, checksum {checksum}")
    return rows


def write_csv(name, rows):
    with open(os.path.join(OUT, name), "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def plot_operations(op_rows):
    ops = list(dict.fromkeys(r["operation"] for r in op_rows))
    fig, axes = plt.subplots(3, 3, figsize=(12, 9.5), sharex=True)
    for ax, op in zip(axes.ravel(), ops):
        for version in VERSIONS:
            pts = [r for r in op_rows if r["operation"] == op and r["version"] == version]
            ax.plot([p["n"] for p in pts], [p["us"] for p in pts], marker="o",
                    color=COLORS[version], label=version)
        ax.set_title(op, fontsize=10)
        ax.set_xscale("log")
        ax.set_ylim(bottom=0)
        ax.grid(True, which="both", alpha=0.3)
    for ax in axes[-1]:
        ax.set_xlabel("Products (log scale)")
    for ax in axes[:, 0]:
        ax.set_ylabel("µs per operation")
    axes[0, 0].legend()
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "operations_vs_n.png"), dpi=200)
    plt.close(fig)

    big = SIZES[-1]
    fig, ax = plt.subplots(figsize=(10, 4.2))
    width = 0.38
    for k, version in enumerate(VERSIONS):
        vals = [next(r["us"] for r in op_rows if r["operation"] == op and r["version"] == version
                     and r["n"] == big) for op in ops]
        bars = ax.bar([i + (k - 0.5) * width for i in range(len(ops))], vals, width,
                      color=COLORS[version], label=version)
        if version == "Phase 3":
            base = [next(r["us"] for r in op_rows if r["operation"] == op and r["version"] == "Phase 2"
                         and r["n"] == big) for op in ops]
            for b, v, b2 in zip(bars, vals, base):
                ax.text(b.get_x() + b.get_width() / 2, max(v, b2) * 1.15, f"{b2 / v:.1f}×",
                        ha="center", fontsize=8)
    ax.set_xticks(range(len(ops)))
    ax.set_xticklabels(ops, rotation=20, ha="right", fontsize=8)
    ax.set_yscale("log")
    ax.set_ylabel("µs per operation (log scale)")
    ax.set_title(f"Per-operation time at {big:,} products (label = Phase 3 speedup)")
    ax.legend()
    ax.grid(True, axis="y", which="both", alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "speedup_1m.png"), dpi=200)
    plt.close(fig)


def plot_load_and_memory(load_rows, mem_rows):
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4))
    for version in ("Phase 2", "Phase 3", "Phase 3 bulk_load"):
        pts = [r for r in load_rows if r["version"] == version]
        ax1.plot([p["n"] for p in pts], [p["seconds"] for p in pts], marker="o",
                 color=COLORS[version], label=version)
    ax1.set_xscale("log")
    ax1.set_yscale("log")
    ax1.set_xlabel("Products (log scale)")
    ax1.set_ylabel("Seconds (log scale)")
    ax1.set_title("Time to load the catalog")
    ax1.grid(True, which="both", alpha=0.3)
    ax1.legend()
    names = [f"{r['version']}\nn = {r['n']:,}" for r in mem_rows]
    rec = [r["records_bytes_per_product"] for r in mem_rows]
    idx = [r["index_bytes_per_product"] for r in mem_rows]
    ax2.bar(names, rec, color="#9ecae1", label="Product records")
    ax2.bar(names, idx, bottom=rec, color="#3182bd", label="Indexes")
    for i, r in enumerate(mem_rows):
        ax2.text(i, r["total_bytes_per_product"] * 1.02, f"{r['total_bytes_per_product']:,.0f} B",
                 ha="center", fontsize=9)
    ax2.tick_params(axis="x", labelsize=8)
    ax2.set_ylabel("Bytes per product")
    ax2.set_title("Memory per product")
    ax2.set_ylim(top=max(r["total_bytes_per_product"] for r in mem_rows) * 1.35)
    ax2.legend(loc="upper center", ncol=2, fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "load_and_memory.png"), dpi=200)
    plt.close(fig)


def read_csv(name):
    """Load a saved results CSV, converting numeric columns back to numbers."""
    with open(os.path.join(OUT, name), newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        for k, v in row.items():
            try:
                row[k] = int(v)
            except ValueError:
                try:
                    row[k] = float(v)
                except ValueError:
                    pass
    return rows


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    if sys.argv[1:] == ["mixed"]:               # rerun only Experiment 4
        print("Power throttling disabled:", stabilize_timing())
        write_csv("compare_mixed.csv", experiment_mixed())
        sys.exit()
    if sys.argv[1:] == ["replot"]:              # redraw charts from saved CSVs, no benchmarking
        plot_operations(read_csv("compare_operations.csv"))
        plot_load_and_memory(read_csv("compare_load.csv"), read_csv("compare_memory.csv"))
        sys.exit()
    print("Power throttling disabled:", stabilize_timing())
    # Save each experiment as soon as it finishes, so a late failure loses nothing.
    op_rows, load_rows = experiment_operations()
    write_csv("compare_operations.csv", op_rows)
    write_csv("compare_load.csv", load_rows)
    plot_operations(op_rows)
    mem_rows = experiment_memory()
    write_csv("compare_memory.csv", mem_rows)
    plot_load_and_memory(load_rows, mem_rows)
    mixed_rows = experiment_mixed()
    write_csv("compare_mixed.csv", mixed_rows)
    print("\nSaved results to results/")
