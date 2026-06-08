# Scheduler Simulation: Enhanced Decentralised Score-Based Scheduler vs. FFT

A discrete-event simulation comparing your architecture against the FFT baseline
(Mo et al., ICS '25) on identical workloads.

## What it does

Both schedulers are driven over the **same** job trace, round by round, so the
comparison is apples-to-apples. Models are parameter tuples (no real training);
arrivals/durations/worker-counts follow Philly-style statistical distributions.

- **FFT baseline** (`fft_baseline.py`): faithful per-round Integer Linear Program
  over the M-N-T allocation space, with the three-term cost (JCT term phi,
  switching penalty s, fairness compensation rho*theta) and the work-conserving
  constraint (Eq. 7). This is the centralized bottleneck.
- **Your architecture** (`smart_scheduler.py`): O(1) Smart Scoring Dispatcher +
  dual-path routing (Fast/Slow) + historical-cache / micro-profiling + an
  asynchronous FFT optimizer over the Slow-Path queue + Decentralised Handshake.

## Files

| File | Purpose |
|------|---------|
| `workload.py` | cluster (T4/V100/A100), model zoo, trace generator |
| `fft_baseline.py` | FFT per-round ILP scheduler |
| `smart_scheduler.py` | your decoupled scheduler |
| `engine.py` | discrete-event loop + metric collection |
| `run_experiment.py` | runs everything, writes the three charts |

Run with: `python3 run_experiment.py`

## Results summary (80 jobs, seed 7)

| Metric (bursty) | FFT | Enhanced | Ratio |
|---|---|---|---|
| Mean JCT (min) | 14.6 | 55.9 | 3.8x |
| Mean FTF | 2.25 | 5.30 | 2.4x |
| **Admission latency (ms)** | **9.1** | **0.019** | **~480x faster** |

Burst-scaling study: FFT admission latency climbs from ~18 ms to ~57 ms as the
burst grows from 10 to 200 simultaneous arrivals; your dispatcher stays flat at
~0.02 ms because it is O(1) per job and independent of the active-set size.

## How to frame this in the viva (the honest tradeoff)

Your contribution is **not** "better at everything." It is a deliberate,
well-motivated tradeoff that directly addresses your research gap
("limited adaptability to dynamic job arrivals with centralized scheduling"):

> "We decouple admission control from global optimization. The Smart Dispatcher
> admits jobs in O(1) time, so admission latency stays flat under bursts where
> FFT's per-round ILP grows with the active set. The cost is that fast-path jobs
> may run on sub-optimal GPUs until the asynchronous FFT optimizer rebalances the
> slow-path queue, which shows up as higher mean JCT and FTF. For latency-sensitive,
> bursty clusters this is a favourable trade; for steady-state throughput-critical
> clusters, centralized FFT remains preferable."

**Anticipated panel questions and your answers:**

- *"Why is your JCT/FTF worse?"* -> By design. Instant admission means a job takes
  whatever good-enough slot is free now, not the globally optimal one. The async
  optimizer corrects this over time but not instantly. That deferral is the
  measured cost.
- *"Doesn't the fast path starve FFT's fairness?"* -> We cap fast-path occupancy at
  (1 - reserve) of the cluster (default 25% reserved), guaranteeing the async
  optimizer always has capacity. Without this, a greedy fast path can starve the
  slow path -- which we observed and fixed.
- *"Is the FFT baseline fair?"* -> Yes -- it solves the actual paper ILP with cvxpy,
  is work-conserving, and beats your system on JCT/FTF. We did not handicap it.

## Knobs you can tune (in `smart_scheduler.py`)

- `SLOW_RESERVE` (0.25): capacity reserved for the async optimizer.
- `threshold` (2.5): Fast-Path match strictness. Lower -> more jobs to Slow Path.
- `BATCH` (24): max jobs per async ILP solve (bounds solver cost).
- `ASYNC_PERIOD` (2, in `engine.py`): how often the background optimizer runs.

## Caveats (state these honestly)

- Single-GPU-type-per-job per round (matches FFT's training-stall avoidance).
- Migration overhead is modelled from state size + fixed checkpoint/NCCL cost.
- The exact JCT/FTF gap depends on the tuning knobs above; the *direction* of the
  tradeoff (admission win, efficiency cost) is robust across random seeds.
