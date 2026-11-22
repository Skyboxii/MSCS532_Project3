"""
Profile the Phase 2 baseline on the mixed workload to find where time goes.

Usage (from the repository root):  python benchmarks/profile_phase2.py [package]
    package = phase2_inventory (default) or inventory (the optimized Phase 3 code)
Writes results/profile_<package>.txt.
"""

import cProfile
import io
import os
import pstats
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, os.pardir)
sys.path[:0] = [HERE, ROOT]

import importlib                                     # noqa: E402

from workload import build, run_mixed                # noqa: E402

N, OPS = 100_000, 50_000

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "phase2_inventory"
    pkg = importlib.import_module(name)
    start = time.perf_counter()
    inv = build(pkg, N)
    build_s = time.perf_counter() - start

    profiler = cProfile.Profile()
    profiler.enable()
    run_mixed(pkg, inv, OPS)
    profiler.disable()

    out = io.StringIO()
    out.write(f"{name}: build {N:,} products in {build_s:.2f} s, then {OPS:,} mixed operations\n\n")
    stats = pstats.Stats(profiler, stream=out).strip_dirs().sort_stats("tottime")
    stats.print_stats(22)
    text = out.getvalue()
    print(text)
    os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
    with open(os.path.join(ROOT, "results", f"profile_{name}.txt"), "w") as f:
        f.write(text)
