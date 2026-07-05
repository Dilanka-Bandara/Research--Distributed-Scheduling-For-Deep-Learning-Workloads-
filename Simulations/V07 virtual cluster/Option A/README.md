# Option A — Virtual Cluster Emulation (multi-process + real Redis)

This runs your **latest architecture** and the **FFT baseline** as *real
concurrent processes* talking through a *real Redis* instance. GPU workers are
sleep-based emulators (no real training — same boundary the FFT paper draws
between its simulator and its testbed). What becomes REAL here, versus the
discrete-event simulation:

- the O(1) dispatcher's decision latency (a wall-clock stopwatch, ~0.5 ms)
- FFT's round-gated synchronous admission (measured, not modelled)
- the ILP solve as real blocking time
- the decentralised handshake as an actual request→verify→ACK round-trip
- Mechanism E evictions: a worker really checkpoints, frees slots, requeues

## Component map (spec → process)

| Spec component | Process / mechanism |
|---|---|
| ① Job submission | `trace_replayer.py` — same Philly-statistics generator as the v2 simulation, fired at scaled real timestamps |
| ② Scoring dispatcher | `dispatcher.py` — O(1) scoring + ΔS routing + JCT-aware (Eq. 3) fast placement; decision latency stopwatched |
| ③ Cache + micro-profiler | Redis set `cache:models`; MISS ⇒ scaled 60 s stall on the Slow Path |
| ④ Global queue | Redis list `queue:global`; `chan:queue_event` is the literal event trigger |
| ⑤ Global recorder | Redis itself |
| ⑥ Background FFT brain | `brain.py` — BRPOP(event, timeout=interval) = hybrid trigger; exact ILP via `fast_solver`; corrective migration; Mechanism E |
| ⑦ Decentralised zone | `node_agent.py` ×3 (T4/V100/A10) — slot table, preemptible worker threads, handshake RPC |
| FFT baseline | `fft_scheduler_proc.py` — centralised round loop; arrivals wait for the solve (your gap, embodied) |

## Setup (Windows)

1. **Redis** (native Windows Redis is dead — use Docker Desktop):
   ```
   docker run -d --name redis -p 6379:6379 redis:7
   ```
   (Alternative: WSL2 → `sudo apt install redis-server && redis-server`)
2. **Python deps** (in this folder):
   ```
   pip install redis scipy numpy
   ```
3. Sanity: `python -c "import redis; print(redis.Redis().ping())"` → `True`

## Run

```bash
# your architecture
python launcher.py --scheduler smart --jobs 60 --regime bursty --seed 1 --timeout 900
# FFT baseline, SAME trace
python launcher.py --scheduler fft   --jobs 60 --regime bursty --seed 1 --timeout 900
# head-to-head
python analyze_results.py --compare runs/fft_bursty_s1.json runs/smart_bursty_s1.json
```

Regimes: steady / mixed / bursty / dynamic / random / heavy (from `workload.py`).
Each run flushes Redis — run modes sequentially, never in parallel.

## Time scaling — read this before quoting numbers

`EMU_TIME_SCALE` (default **100**) divides all physical durations: at 100×,
a 5-min round = 3 s; a 60-job trace ≈ 10–20 min wall. **Do not go above
~150×**: control-plane constants (50 ms worker ticks, RPC latencies,
checkpoint settle sleeps) stop being negligible against the scaled round and
distort the physics — we observed exactly this at 400× during development
(churny migrations, inflated JCT). At 100× those artifacts are ~1% effects.
State your chosen factor and this floor argument in the thesis.

Wall-time budget ≈ `(horizon_rounds + longest_job_tail) × 300 / SCALE` seconds;
set `--timeout` generously (900+ s for 60 jobs at 100×).

## What to report (ties to your research gap)

1. **Measured admission latency**: dispatcher ~0.5 ms vs FFT's round-gated
   hundreds of ms (at 100×, mean ≈ half a round + solve). This is
   "limited adaptability with centralized scheduling" as a stopwatch number.
2. **Starvation / JCT / FTF ratios** from `--compare` (reported in rounds —
   directly comparable with the v2 simulation).
3. **Validation figure** (thesis-grade): run the same seed/regime/jobs through
   `scheduler_simulation_v2` and through this emulation; report the JCT
   deviation. The FFT paper validated its simulator against its physical
   cluster at ≤4.9% deviation — you mirror that methodology one tier down.
   ```python
   from workload import generate_trace, make_cluster
   from engine import run_smart   # in scheduler_simulation_v2
   m = run_smart(generate_trace(n_jobs=60, regime="bursty", seed=1,
                                horizon=max(60, 60*1.4)), make_cluster(8))
   print(m["jct_mean"])           # compare with jct_mean_rounds from the emulation
   ```
   (Use `make_cluster(8)` to match `EMU_N_PER_TYPE=8`, or set both to 12.)

## Honest limits

- Emulation validates the **control plane** (latencies, races, triggers,
  preemption), not training physics — throughputs stay profiler-model numbers.
- Everything shares one host: OS jitter exists; localhost ≠ network
  (that is Option B — containerise these same processes with Docker Compose
  and `tc/netem` bandwidth caps for network-enforced migration costs).
- Runs are NOT bit-deterministic (real concurrency); seeds fix the workload
  only. Report seed-averaged results, same as the simulation.

## Known smoke-test status (sandbox, before hand-off)

End-to-end verified: both modes complete full traces; Mechanism E evictions
checkpoint-and-requeue correctly; handshake NACKs counted; the
evict-before-capacity-check race in corrective migration was found and fixed
(rejects dropped 200 → 1 in the post-fix run). First runs on your machine
should start small (`--jobs 20`) to calibrate wall-time before scaling up.

## Troubleshooting

- `ConnectionError` → Redis not up (`docker ps`, port 6379).
- Jobs stuck at end of run → raise `--timeout`; check agent processes alive.
- Windows: if `subprocess` spawning misbehaves, run components in separate
  terminals manually in this order: 3× `python node_agent.py <TYPE>`, then
  `python dispatcher.py` + `python brain.py` (or `python fft_scheduler_proc.py`),
  then `python trace_replayer.py <jobs> <regime> <seed>`.
