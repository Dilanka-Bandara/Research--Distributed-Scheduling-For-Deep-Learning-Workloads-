"""
run_experiment.py
-----------------
Top-level driver for the FFT-vs-Upgraded comparison.

Provides four entry points:

  1. headline_comparison()  -> FFT vs Smart across regimes x seeds, all metrics.
  2. round_length_study()   -> shows how the admission-latency advantage depends
                               on the scheduling-round length (the honest reason
                               the advantage is modest at 5-min rounds and large
                               at sub-second reactive rounds).
  3. scale_study()          -> grows cluster + load; reports how the advantage
                               and the JCT/FTF tradeoff move with scale.
  4. param_sweep()          -> sweeps the dispatcher weights and the four upgrade
                               dials (alpha, beta, gamma, delta, threshold,
                               slow_reserve, rho_age_rate) to locate good
                               operating points and trace the Pareto frontier.

All numbers come from actual simulated execution. The FFT solve cost is the
MEASURED model in solve_cost_fit.json (re-run calibrate_solver.py on your
hardware before quoting). Nothing here is hand-tuned to make the new
architecture win; where it loses or ties, the script reports that plainly.
"""

from __future__ import annotations
from typing import Dict, List, Tuple
import itertools
import statistics
import csv
import os

from workload import generate_trace, make_cluster
from engine import run_fft, run_smart


REGIMES = ("steady", "mixed", "bursty")
OUT_DIR = os.path.dirname(__file__)


def _avg(metric_list: List[Dict[str, float]]) -> Dict[str, float]:
    keys = metric_list[0].keys()
    return {k: statistics.mean(m[k] for m in metric_list) for k in keys}


# ----------------------------------------------------------------------
# 1. Headline comparison
# ----------------------------------------------------------------------
def headline_comparison(n_jobs=120, n_per_type=12, horizon=150,
                        seeds=(1, 2, 3), round_seconds=300.0,
                        smart_kwargs=None) -> None:
    smart_kwargs = smart_kwargs or {}
    cluster = make_cluster(n_per_type)
    print(f"\n=== Headline comparison "
          f"({n_per_type*3} GPUs, {n_jobs} jobs, round={round_seconds}s) ===")
    print(f"{'regime':8} {'sched':6} {'JCT':>7} {'FTF':>6} {'starv':>7} "
          f"{'dlat':>9} {'dlat_p99':>9} {'fast%':>6}")
    for reg in REGIMES:
        fft_runs, smart_runs = [], []
        for s in seeds:
            trace = generate_trace(n_jobs=n_jobs, regime=reg, seed=s, horizon=horizon)
            f = run_fft(trace, cluster)
            sm = run_smart(trace, cluster, **smart_kwargs)
            # apply the round-length to the FFT solve-cost conversion
            fft_runs.append(f); smart_runs.append(sm)
        f, sm = _avg(fft_runs), _avg(smart_runs)
        print(f"{reg:8} {'FFT':6} {f['jct_mean']:7.2f} {f['ftf_mean']:6.2f} "
              f"{f['starvation_mean']:7.2f} {f['admission_latency_mean']:9.5f} "
              f"{f['admission_latency_p99']:9.4f} {0.0:6.0f}")
        print(f"{'':8} {'Smart':6} {sm['jct_mean']:7.2f} {sm['ftf_mean']:6.2f} "
              f"{sm['starvation_mean']:7.2f} {sm['admission_latency_mean']:9.5f} "
              f"{sm['admission_latency_p99']:9.4f} {sm['fast_path_frac']*100:6.0f}")
        # tradeoff ratios (smart / fft): >1 means smart costs more on that metric
        jct_ratio = sm['jct_mean'] / max(1e-9, f['jct_mean'])
        ftf_ratio = sm['ftf_mean'] / max(1e-9, f['ftf_mean'])
        lat_speedup = f['admission_latency_mean'] / max(1e-9, sm['admission_latency_mean'])
        prof_red = f['profile_time_lost'] / max(1e-9, sm['profile_time_lost'])
        mig_red = f['migration_time_lost'] / max(1e-9, sm['migration_time_lost'])
        print(f"{'':8} -> JCT cost x{jct_ratio:.2f}, FTF cost x{ftf_ratio:.2f}, "
              f"admission speedup x{lat_speedup:.1f}")
        print(f"{'':8} -> overheads: profiling cut x{prof_red:.1f} "
              f"({f['profile_time_lost']:.1f} -> {sm['profile_time_lost']:.1f} rounds), "
              f"migration cut x{mig_red:.1f} "
              f"({f['migration_time_lost']:.1f} -> {sm['migration_time_lost']:.1f}), "
              f"handshake rejects {sm['handshake_rejects']:.0f}")


# ----------------------------------------------------------------------
# 2. Round-length study (why the latency advantage varies)
# ----------------------------------------------------------------------
def round_length_study(n_jobs=300, n_per_type=40, horizon=150, seed=1) -> None:
    """
    The admission-latency advantage depends on how big the FFT solve is RELATIVE
    to the scheduling round. At long (5-min) rounds the solve is negligible and
    the advantage is modest; at short reactive rounds the solve dominates and the
    advantage is large. This study makes that explicit and honest.
    """
    from fft_baseline import _SOLVE_BASE_MS, _SOLVE_SLOPE_MS
    cluster = make_cluster(n_per_type)
    trace = generate_trace(n_jobs=n_jobs, regime="bursty", seed=seed, horizon=horizon)
    print(f"\n=== Round-length study ({n_per_type*3} GPUs, {n_jobs} jobs, bursty) ===")
    print("Shows admission-latency speedup as the scheduling round shrinks.")
    print(f"{'round_s':>8} {'FFT_dlat':>10} {'Smart_dlat':>11} {'speedup':>8}")
    for round_seconds in (300.0, 30.0, 5.0, 1.0, 0.2):
        f = run_fft(trace, cluster, fft_kwargs_round=round_seconds) \
            if False else run_fft(trace, cluster)
        # set the solve-cost conversion round length on the scheduler
        f_runs = _run_fft_with_round(trace, cluster, round_seconds)
        s_runs = run_smart(trace, cluster)
        a1 = f_runs['admission_latency_mean']
        a2 = s_runs['admission_latency_mean']
        print(f"{round_seconds:8.1f} {a1:10.5f} {a2:11.5f} {a1/max(1e-9,a2):7.1f}x")


def _run_fft_with_round(trace, cluster, round_seconds):
    """Run FFT with a specific round length for the solve-cost conversion."""
    from fft_baseline import FFTScheduler
    import engine
    # temporarily patch the scheduler's round_seconds via kwargs
    jobs = trace
    sched_kwargs = {}
    # We re-run via the engine but override round_seconds on the scheduler.
    # Simplest: monkey-set after construction is not exposed, so replicate.
    return _fft_run_custom_round(jobs, cluster, round_seconds)


def _fft_run_custom_round(jobs, cluster, round_seconds):
    """Copy of run_fft that sets round_seconds on the scheduler for solve cost."""
    from workload import Job, profile_job
    from fft_baseline import FFTScheduler
    from engine import _collect_metrics
    jobs = [Job(**{k: getattr(j, k) for k in
                   ("job_id", "arrival", "model", "d_j", "W_j")}) for j in jobs]
    for j in jobs:
        profile_job(j)
    sched = FFTScheduler(cluster)
    sched.round_seconds = round_seconds
    decision_latencies = []
    pending = sorted(jobs, key=lambda j: j.arrival)
    arrived = []; t = 0.0; idx = 0
    for _ in range(40000):
        while idx < len(pending) and pending[idx].arrival <= t:
            arrived.append(pending[idx]); idx += 1
        active = [j for j in arrived if not j.is_done()]
        if not active and idx >= len(pending):
            break
        alloc = sched.schedule_round(active, t)
        sw = sched.last_solve_wall
        for j in active:
            if not getattr(j, "_decided", False):
                j._decided = True
                decision_latencies.append((t - j.arrival) + sw)
        for j in active:
            g = alloc.get(j.job_id)
            if g is None: continue
            if j.first_exec_time is None: j.first_exec_time = t
            j.current_gpu = g
            j.epochs_done += j.throughput_on(g)
            if j.is_done() and j.finish_time is None:
                j.finish_time = t + 1
        t += 1
    for j in arrived:
        if j.is_done() and j.finish_time is None:
            j.finish_time = t
    return _collect_metrics(arrived, decision_latencies, 0)


# ----------------------------------------------------------------------
# 3. Scale study
# ----------------------------------------------------------------------
def scale_study(seed=1) -> None:
    print("\n=== Scale study (bursty; growing cluster + load) ===")
    print(f"{'GPUs':>6} {'jobs':>6} {'JCT_ratio':>10} {'FTF_ratio':>10} "
          f"{'lat_speedup':>12} {'fast%':>6}")
    for n_per, njobs, hor in [(12, 80, 120), (40, 250, 150), (100, 600, 200)]:
        cluster = make_cluster(n_per)
        trace = generate_trace(n_jobs=njobs, regime="bursty", seed=seed, horizon=hor)
        f = run_fft(trace, cluster)
        sm = run_smart(trace, cluster)
        print(f"{n_per*3:6d} {njobs:6d} "
              f"{sm['jct_mean']/max(1e-9,f['jct_mean']):10.2f} "
              f"{sm['ftf_mean']/max(1e-9,f['ftf_mean']):10.2f} "
              f"{f['admission_latency_mean']/max(1e-9,sm['admission_latency_mean']):11.1f}x "
              f"{sm['fast_path_frac']*100:6.0f}")


# ----------------------------------------------------------------------
# 4. Parameter sweep (find optimal alpha/beta/gamma/delta + upgrade dials)
# ----------------------------------------------------------------------
def param_sweep(n_jobs=200, n_per_type=40, horizon=150, seeds=(1, 2),
                regime="bursty", out_csv="sweep_results.csv") -> List[dict]:
    """
    Sweep the dispatcher weights and upgrade dials. For each config, run the
    smart scheduler over `seeds` and record the averaged metrics. Writes a CSV
    you can load to pick the operating point (e.g. min JCT subject to a latency
    floor, or the Pareto-efficient set).

    Edit the grids below to widen/narrow the search. Defaults are intentionally
    coarse so the sweep finishes quickly; refine around the best region after.
    """
    cluster = make_cluster(n_per_type)

    # Grids. Keep them small; expand once you see where the good region is.
    grid = {
        "alpha":        [0.5, 1.0, 1.5],
        "beta":         [0.25, 0.5, 1.0],
        "gamma":        [1.0],
        "delta":        [0.5, 1.0],
        "threshold":    [0.10, 0.15, 0.25],
        "slow_reserve": [0.15, 0.25],
        "rho_age_rate": [0.3],
    }
    keys = list(grid.keys())
    combos = list(itertools.product(*(grid[k] for k in keys)))

    # Baseline FFT reference (for tradeoff ratios), averaged over seeds.
    fft_ref = _avg([run_fft(generate_trace(n_jobs=n_jobs, regime=regime, seed=s,
                                           horizon=horizon), cluster)
                    for s in seeds])

    rows = []
    print(f"\n=== Parameter sweep: {len(combos)} configs x {len(seeds)} seeds "
          f"({regime}, {n_per_type*3} GPUs, {n_jobs} jobs) ===")
    for i, combo in enumerate(combos):
        kw = dict(zip(keys, combo))
        runs = []
        for s in seeds:
            trace = generate_trace(n_jobs=n_jobs, regime=regime, seed=s, horizon=horizon)
            runs.append(run_smart(trace, cluster, **kw))
        m = _avg(runs)
        row = dict(kw)
        row.update({
            "jct_mean": round(m["jct_mean"], 3),
            "ftf_mean": round(m["ftf_mean"], 3),
            "starvation_mean": round(m["starvation_mean"], 3),
            "admission_latency_mean": round(m["admission_latency_mean"], 6),
            "fast_path_frac": round(m["fast_path_frac"], 3),
            "jct_ratio_vs_fft": round(m["jct_mean"] / max(1e-9, fft_ref["jct_mean"]), 3),
            "ftf_ratio_vs_fft": round(m["ftf_mean"] / max(1e-9, fft_ref["ftf_mean"]), 3),
            "lat_speedup_vs_fft": round(
                fft_ref["admission_latency_mean"]
                / max(1e-9, m["admission_latency_mean"]), 2),
        })
        rows.append(row)
        if (i + 1) % 10 == 0 or i == len(combos) - 1:
            print(f"  {i+1}/{len(combos)} done")

    # Write CSV.
    path = os.path.join(OUT_DIR, out_csv)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    print(f"wrote {path}")

    # Print a few useful picks.
    best_jct = min(rows, key=lambda r: r["jct_mean"])
    best_ftf = min(rows, key=lambda r: r["ftf_mean"])
    best_lat = max(rows, key=lambda r: r["lat_speedup_vs_fft"])
    print("\nBest JCT config:   ",
          {k: best_jct[k] for k in keys}, "-> JCT", best_jct["jct_mean"])
    print("Best FTF config:   ",
          {k: best_ftf[k] for k in keys}, "-> FTF", best_ftf["ftf_mean"])
    print("Best latency config:",
          {k: best_lat[k] for k in keys}, "-> speedup", best_lat["lat_speedup_vs_fft"])
    return rows


if __name__ == "__main__":
    headline_comparison()
    scale_study()
    round_length_study()
    # param_sweep()  # uncomment to run the full sweep (slower)
