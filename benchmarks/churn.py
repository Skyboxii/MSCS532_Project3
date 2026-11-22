"""
Delete-heavy workload on the from-scratch hash table: Phase 2 (tombstones)
versus Phase 3 (backward-shift deletion).

The live size is held constant at LIVE keys while STEPS remove/insert pairs
churn through the table.  Every WINDOW steps it records:
  * slots scanned by an unsuccessful lookup (averaged over random absent keys)
  * the slowest single remove+insert in the window (rehash pauses show up here)

Usage (from the repository root):  python benchmarks/churn.py      (~1 minute)
"""

import csv
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, os.pardir)
sys.path[:0] = [HERE, ROOT]

import matplotlib                                                # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                                  # noqa: E402

from bench_util import stabilize_timing                          # noqa: E402
from inventory.hash_table import HashTable as Phase3Table        # noqa: E402
from phase2_inventory.hash_table import HashTable as Phase2Table  # noqa: E402

CAPACITY, LIVE, STEPS, WINDOW, PROBES = 32_768, 9_000, 120_000, 1_000, 2_000
OUT = os.path.join(ROOT, "results")


def miss_cost(table, absent):
    """Average slots scanned before an unsuccessful lookup reaches an empty slot.
    Phase 2 tombstones are non-None, so they count as scanned slots."""
    keys = table._keys
    mask = len(keys) - 1
    total = 0
    for k in absent:
        i = hash(k) & mask
        total += 1
        while keys[i] is not None:
            i = (i + 1) & mask
            total += 1
    return total / len(absent)


def churn(table_class):
    table = table_class(capacity=CAPACITY)
    rng = random.Random(42)
    live = list(range(LIVE))
    for k in live:
        table.put(k, k)
    next_key = 10**9
    absent = [-(i + 1) * 7919 for i in range(PROBES)]       # keys never inserted
    rows, slowest = [], 0.0
    for step in range(1, STEPS + 1):
        victim = live.pop(rng.randrange(len(live)))
        start = time.perf_counter()
        table.remove(victim)
        table.put(next_key, next_key)
        slowest = max(slowest, time.perf_counter() - start)
        live.append(next_key)
        next_key += 1
        if step % WINDOW == 0:
            rows.append({"step": step, "miss_slots": miss_cost(table, absent),
                         "slowest_ms": slowest * 1000, "capacity": len(table._keys),
                         "occupied_fraction": sum(k is not None for k in table._keys) / len(table._keys)})
            slowest = 0.0
    return rows


if __name__ == "__main__":
    print("Power throttling disabled:", stabilize_timing())
    results = {"Phase 2 (tombstones)": churn(Phase2Table), "Phase 3 (backward shift)": churn(Phase3Table)}
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "churn.csv"), "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["version", "step", "miss_slots", "slowest_ms", "capacity",
                                               "occupied_fraction"])
        writer.writeheader()
        for version, rows in results.items():
            for r in rows:
                writer.writerow({"version": version, **{k: round(v, 4) if isinstance(v, float) else v
                                                        for k, v in r.items()}})
    for version, rows in results.items():
        miss = [r["miss_slots"] for r in rows]
        spikes = [r["slowest_ms"] for r in rows]
        print(f"{version}: miss slots mean {sum(miss) / len(miss):.2f}, max {max(miss):.2f}; "
              f"occupied up to {max(r['occupied_fraction'] for r in rows):.2f}; "
              f"slowest single op {max(spikes):.2f} ms (median window {sorted(spikes)[len(spikes) // 2]:.3f} ms)")

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 3.8))
    colors = {"Phase 2 (tombstones)": "#d62728", "Phase 3 (backward shift)": "#1f77b4"}
    for version, rows in results.items():
        steps = [r["step"] for r in rows]
        ax1.plot(steps, [r["miss_slots"] for r in rows], color=colors[version], label=version)
        ax2.plot(steps, [r["slowest_ms"] for r in rows], color=colors[version], label=version)
    ax1.set_title(f"Unsuccessful lookup cost ({LIVE:,} live keys)")
    ax1.set_ylabel("Slots scanned per miss")
    ax2.set_title("Slowest remove + insert per 1,000 steps")
    ax2.set_ylabel("Milliseconds (log scale)")
    ax2.set_yscale("log")
    for ax in (ax1, ax2):
        ax.set_xlabel("Remove/insert steps")
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "churn.png"), dpi=200)
    plt.close(fig)
