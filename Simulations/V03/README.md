# FFT vs Enhanced Decentralised Score-Based Scheduler — Comparison Simulation

A discrete-event simulation comparing the FFT baseline (Mo et al., ICS '25)
against the upgraded Enhanced Decentralised Score-Based Scheduler, with full
metric collection and parameter sweeping for finding optimal α/β/γ/δ and the
four upgrade dials.

## Files

| File | Role |
|---|---|
| `workload.py` | Synthetic Philly-statistics trace generator + model zoo + per-GPU-type throughput. No real training; jobs are parameter tuples. |
| `fft_baseline.py` | FFT scheduler: per-round ILP cost-minimisation (JCT + switching + fairness `ρ_j(t)`). Solve cost is a **measured** model, not a guess. |
| `smart_scheduler.py` | Upgraded architecture: O(1) fairness-aware dispatcher, dual-path routing, tunable threshold, reserve budget, corrective background FFT solver. |
| `engine.py` | Discrete-event engine + the two drivers (`run_fft`, `run_smart`) + metric collection. |
| `calibrate_solver.py` | Measures **real CBC solve times** and fits the solve-cost model. Re-run on your hardware. |
| `run_experiment.py` | Headline comparison, scale study, round-length study, and the parameter sweep. |
| `solve_cost_fit.json` | The fitted solve-cost model (regenerate with `calibrate_solver.py`). |

## Quick start

```bash
pip install pulp numpy --break-system-packages
python3 calibrate_solver.py          # measure solver cost on YOUR machine first
python3 run_experiment.py            # headline + scale + round-length studies
```

To run the parameter sweep (find optimal α/β/γ/δ/threshold/slow_reserve):

```python
from run_experiment import param_sweep
param_sweep(n_jobs=200, n_per_type=40, seeds=(1,2), regime="bursty")
# -> writes sweep_results.csv with every config's JCT, FTF, latency, and
#    ratios vs FFT. Load it to pick your operating point / Pareto frontier.
```

Edit the `grid` dict in `param_sweep()` to widen or refine the search.

## Metrics (read this — the comparison hinges on the definition)

- **admission_latency = scheduling-DECISION latency**: time from arrival until
  the scheduler decides a placement.
  - FFT pays (wait to next round) + (measured solve time, grows with active jobs).
  - Smart: Fast-Path jobs decided in O(1) on arrival; Slow-Path jobs decided at
    the next event-triggered background brain run (solve cost does not block it).
  This is the architectural quantity. It is **not** time-to-free-GPU.
- **starvation**: time from arrival to first execution (capacity-bound, similar
  across schedulers — reported separately to keep decision-latency honest).
- **jct / makespan / ftf**: from actual simulated execution.

## Honest findings (important for the viva)

These come from actual simulated execution with a **measured** FFT solve cost.
Nothing is tuned to make the new architecture win.

1. **Steady-state tradeoff is small and shrinks with scale.** At 36 GPUs the
   upgraded scheduler costs roughly ×1.03–1.09 JCT and ×1.07–1.19 FTF versus
   FFT. By 300 GPUs / 600 jobs these ratios fall to ~×1.01 — the cost nearly
   vanishes at scale.

2. **The admission-latency advantage is MODEST at lab scale, not order-of-
   magnitude.** Measured speedup is ×1.2–2.2 across regimes and round lengths at
   realistic sizes, rising to ~×3.7 with tuned dispatcher weights. The reason is
   physical and defensible: with a realistic 5-minute scheduling round, the FFT
   ILP solve (~28 ms + 0.09 ms/job, measured) is negligible relative to the
   round, so FFT's decision latency is dominated by round-boundary waiting, which
   the Fast Path removes but only by ~half a round.

3. **A ~480× advantage is only physically reachable when the solve dominates the
   round** — i.e. very large clusters (thousands of jobs) or sub-second reactive
   scheduling. The `round_length_study()` and `scale_study()` make this explicit.
   If you need to claim a large number, you must run in (and justify) that
   regime; at 36 GPUs with 5-minute rounds it is not supported by the measured
   solve cost.

   **Recommended framing:** present the advantage as a *function of scale and
   round length* (the studies produce exactly this), not a single headline
   multiplier. "The advantage grows as the cluster grows and as scheduling
   becomes more reactive; at lab scale it is modest but the steady-state cost is
   also near-zero" is a far stronger and more defensible thesis than an
   unexplained 480×.

## Reproducibility

- All runs are deterministic given a seed (verified).
- Results are stable across seeds and the three regimes (steady/mixed/bursty).
- The solve-cost model is the only hardware-dependent input; regenerate it with
  `calibrate_solver.py` before quoting any latency numbers.
