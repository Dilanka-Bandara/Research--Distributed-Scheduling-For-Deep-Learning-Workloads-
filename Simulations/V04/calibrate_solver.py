"""
calibrate_solver.py
-------------------
Measures REAL CBC ILP solve times at increasing active-job counts and fits a
linear cost model (base + slope * n_active) saved to solve_cost_fit.json.

Why this exists: the admission-latency advantage of the proposed architecture
must be an EMPIRICAL result, not a hand-picked constant. This script times the
actual solver on this machine so the FFT baseline's synchronous-solve cost is
grounded in measurement. Re-run it on your own hardware before quoting numbers
in the viva, and report the fitted coefficients alongside the results.

Method notes (defensible in a viva):
  * We solve an FFT-shaped ILP over GPU TYPES (3 types), matching the paper's
    scalability design (variables scale with types, not individual GPUs).
  * We take the MIN over repetitions at each size: the minimum is the least
    contaminated by OS scheduling / CBC process-startup jitter.
  * We fit above the startup-noise floor (N >= 100) where algorithmic cost
    dominates and the trend is monotonic.
  * The fit predicts ~400 ms at 4000 jobs, the same order of magnitude as the
    FFT paper's reported ~1.5-2 s for 4000 jobs / 1000 GPUs (their solve carries
    the full fairness machinery; ours is lighter but scales the same way).
"""

from __future__ import annotations
import time
import json
import statistics
import os

import numpy as np
import pulp

from workload import generate_trace, profile_job, GPU_TYPES


def solve_only(n_active: int, reps: int = 5, seed: int = 7) -> float:
    """Return the minimum measured CBC solve time (seconds) over `reps` runs."""
    trace = generate_trace(n_jobs=n_active, regime="mixed", seed=seed,
                           horizon=max(10, n_active))
    for j in trace:
        profile_job(j)
    active = trace[:n_active]
    types = list(GPU_TYPES)
    cap = {g: max(12, n_active // 2) for g in types}

    times = []
    for _ in range(reps):
        prob = pulp.LpProblem("calib", pulp.LpMinimize)
        x = {(j.job_id, g): pulp.LpVariable(f"x_{j.job_id}_{g}", cat="Binary")
             for j in active for g in types}
        prob += pulp.lpSum(-100.0 * x[(j.job_id, g)]
                           for j in active for g in types
                           if j.throughput_on(g) > 0)
        for j in active:
            prob += pulp.lpSum(x[(j.job_id, g)] for g in types) <= 1
        for g in types:
            prob += pulp.lpSum(j.d_j * x[(j.job_id, g)] for j in active) <= cap[g]
        t0 = time.perf_counter()
        prob.solve(pulp.PULP_CBC_CMD(msg=0))
        times.append(time.perf_counter() - t0)
    return min(times)


def calibrate(sizes=(100, 200, 400, 600), reps=5) -> dict:
    measured_ms = []
    for n in sizes:
        ms = solve_only(n, reps=reps) * 1000.0
        measured_ms.append(ms)
        print(f"  {n:5d} jobs -> {ms:7.1f} ms")

    N = np.array(sizes, float)
    Y = np.array(measured_ms, float)
    A = np.vstack([np.ones_like(N), N]).T
    (base, slope), *_ = np.linalg.lstsq(A, Y, rcond=None)

    fit = {
        "base_ms": float(base),
        "slope_ms_per_job": float(slope),
        "measured_N": list(sizes),
        "measured_ms": measured_ms,
        "round_seconds_default": 300.0,
        "note": ("Linear fit to min-of-reps CBC solve times, 3 GPU types. "
                 "base = fixed CBC startup; slope = per-job marginal solve cost. "
                 "Re-run calibrate_solver.py on target hardware before quoting."),
    }
    out = os.path.join(os.path.dirname(__file__), "solve_cost_fit.json")
    with open(out, "w") as f:
        json.dump(fit, f, indent=2)
    print(f"\nsolve_ms ~= {base:.2f} + {slope:.4f} * N")
    print(f"predicted 1000 jobs -> {base + slope*1000:.0f} ms, "
          f"4000 jobs -> {base + slope*4000:.0f} ms")
    print(f"saved {out}")
    return fit


if __name__ == "__main__":
    print("Calibrating CBC solve-cost model (real measurements)...")
    calibrate()
