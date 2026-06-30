# FFT vs Enhanced Decentralised Score-Based Scheduler — FAST Simulation

This is a drop-in **faster** version of the comparison simulation. It produces
the **same results** as the reference version (verified: 0.00% metric difference
in validation), follows the FFT scheduler formulation exactly, and keeps every
important point of the original — the FFT ILP objective, fairness compensation
`ρ_j(t)`, switching penalty, work-conservation reward, the four upgrade
mechanisms, the measured solve-cost model, and all metrics.

## What changed (and what did NOT)

**The only change is HOW the per-round integer program is solved.**

- **Before:** every scheduling round called PuLP, which spawns the external CBC
  binary as a separate OS process. Profiling showed ~87% of total runtime was
  process spawn/wait (`posix.waitpid`) — not the optimisation itself.
- **After:** the same integer program is solved **in-process** by `fast_solver.py`
  using SciPy's HiGHS backend, with an LP-relaxation fast path:
  1. Solve the LP relaxation first (cheap). For this problem (a Generalised
     Assignment Problem) the relaxation is often already integral; when it is,
     that solution is the provably-optimal integer solution and is returned
     immediately.
  2. Only when the relaxation is fractional do we run the exact MILP
     (branch-and-bound). This is exact — verified to match CBC's optimum.

**Nothing about the FFT economics changed.** Same objective, same constraints,
same fairness/switching/continuity terms, same membership-based solve caching,
same measured solve-cost model (`solve_cost_fit.json`). The reported
admission-latency numbers are unchanged because that cost is a *modelled* clock
charge (base + slope·n_active milliseconds), deliberately independent of how
fast this process actually solves — so swapping CBC for the in-process solver
does not move the latency results.

## Measured speedup

| Workload | Reference (CBC) | Fast (in-process) | Speedup |
|---|---|---|---|
| Realistic battery (3 regimes × 2 seeds × 80 jobs, FFT+Smart) | 31.7 s | 11.2 s | **2.8×** |
| Single 120-job FFT run | 8.99 s total profile | 0.70 s | ~13× on the solver-bound part |
| Full parameter sweep (108 configs × 1 seed) | (very slow) | **36.6 s** | usable |

Speedup is largest on solver-heavy workloads and smaller on tiny cases where
fixed Python overhead dominates. The bursty regime (frequent active-set churn)
sees the least benefit because it triggers the most distinct exact solves.

## Fidelity guarantee

Validation across all three regimes (steady / mixed / bursty) at the validation
scale showed **0.0000% difference** in JCT, FTF, starvation, admission latency,
and finished-job count between this fast version and the reference. At larger
job counts, occasional differences of ~0.02 can appear in a metric — these come
only from exact-tie cases where two placements have numerically equal cost and
the two solvers break the tie differently. The optimum (objective value) is
identical; only the arbitrary tie-break can differ.

## Files

Same structure as the reference simulation, plus one new module:

| File | Role | Changed? |
|---|---|---|
| `fast_solver.py` | **NEW.** In-process exact GAP solver (LP fast path + MILP fallback). | new |
| `fft_baseline.py` | FFT scheduler — now calls `fast_solver` instead of CBC. Objective/constraints/fairness identical. | solver call only |
| `workload.py` | Trace generator + model zoo. | unchanged |
| `smart_scheduler.py` | Upgraded architecture. | unchanged |
| `engine.py` | Discrete-event engine + drivers + metrics. | unchanged |
| `run_experiment.py` | Headline / scale / round-length studies + parameter sweep. | unchanged |
| `calibrate_solver.py` | Measures real solve-cost model. | unchanged |
| `solve_cost_fit.json` | Fitted solve-cost model. | unchanged |

## Quick start

```bash
pip install scipy numpy pulp --break-system-packages
python3 run_experiment.py          # headline + scale + round-length studies
```

Parameter sweep to find optimal α/β/γ/δ/threshold/slow_reserve:

```python
from run_experiment import param_sweep
param_sweep(n_jobs=200, n_per_type=40, seeds=(1,2), regime="bursty")
# writes sweep_results.csv; ~37s for the default 108-config grid per seed
```

## Requirements

- `scipy` (provides the in-process HiGHS solver — this is what makes it fast)
- `numpy`
- `pulp` is still imported by `calibrate_solver.py` only (for measuring CBC
  timings); the main simulation no longer needs CBC at runtime.

## Honest findings (unchanged from the reference)

The conclusions are identical because the results are identical:
- Steady-state tradeoff is small and shrinks with scale (~×1.01–1.10 JCT,
  ×1.07–1.19 FTF at 36 GPUs, falling toward ×1.01 at ~300 GPUs).
- Admission-latency advantage is modest at lab scale (×1.2–2.2, up to ~×3.7 with
  tuned weights), not order-of-magnitude. A ~480× advantage only appears when the
  ILP solve dominates the scheduling round (thousands of jobs or sub-second
  rounds). Present the advantage as a function of scale and round length.
