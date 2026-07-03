# FFT vs Enhanced Decentralised Score-Based Scheduler — Simulation v2 (full fidelity)

This is the consolidated comparison simulation, built to follow the FFT paper
(Mo et al., ICS '25) and the architecture spec (`new_architecture_for_chatgpt.md`)
as closely as the discrete-event abstraction allows. Both schedulers share the
same FFT ILP core — exactly as in your design, where the background brain *is*
the FFT solver.

## What v2 adds over the previous suite

The earlier suite modelled migration and profiling only as *objective penalties*.
v2 makes them **real lost GPU time**, grounded in the paper's own numbers:

1. **Migration = state-transfer stall.** When a job switches GPU type, it stalls
   for `state_gb / 1.25 GB/s` (10 Gbps LAN, the paper's testbed) converted to
   rounds. Paper's example: OPT-6.7B's 107 GB state ≈ 0.29 of a 5-minute round.
   Applies to BOTH schedulers identically.
2. **Profiling overhead.** FFT profiles *every* new job on the fly (paper Sec. 5:
   top priority, suspends training). Your architecture pays the 60 s
   micro-profiling window only on a historical-cache MISS (spec Sec. 3.3);
   cache HITs and Fast-Path jobs skip it. This makes the spec's caching
   advantage measurable: in validation, profiling time lost drops ~13×.
3. **Paper-exact fairness coefficient.** `μ_j(t) = (t − a_j)/(τ_j − a_j)` (Eq. 5),
   with `τ_j` re-estimated as the completion time on a 1/|A(t)| share.
   (`mu` remains a knob, default 1.0 = paper-exact.)
4. **Handshake accounting.** Rejected migrations (target lacks capacity at
   commit time) are counted and reported — the cost of decentralisation made
   visible.
5. **New metrics**: `migration_time_lost`, `profile_time_lost`,
   `migrations_total`, `handshake_rejects`, alongside JCT / FTF / starvation /
   decision latency / fast-path fraction.

## Fidelity mapping (paper / spec → code)

| Source | Mechanism | Where in code |
|---|---|---|
| Paper Eq. 1 | One GPU type per job; per-type capacity | `fast_solver.solve_assignment` constraints |
| Paper Eq. 3 | JCT term φ = (t−a_j)θ/W_j + d_j·W_j/θ | `fft_baseline._phi` |
| Paper Eq. 4/6 | Objective φ + s − ρθ per (job,type) | `fft_baseline.schedule_round` cost dict |
| Paper Eq. 5 | ρ_j(t+1) = max{0, ρ_j + μ_j(t)(W_j/(τ_j−a_j) − Σθx)} | `fft_baseline._update_fairness` |
| Paper Eq. 5 | μ_j(t) = (t−a_j)/(τ_j−a_j), dynamic | same, `mu=1.0` default |
| Paper Eq. 7 | Work conservation | strong scheduling reward (hard constraint is infeasible in tight regimes — documented lesson) |
| Paper §4.2.3 | Switching penalty s_j^i from transfer time | `_switch_cost` (objective) **+ real stall** (engine/place) |
| Paper §5 | Profiling on the fly, suspends jobs | per-arrival profiling stall (FFT side) |
| Paper §6.4.1 | ILP solve cost grows with jobs | measured model `solve_cost_fit.json` (re-run `calibrate_solver.py` on your machine) |
| Paper §6.1 | T4/V100/A10, Philly-statistics traces | `workload.py` |
| Spec §2 | S_job = α·d̂ + β·Ŵ + ρ_age; S_node = (γ·cap+δ·avail)/(γ+δ); ΔS threshold | `smart_scheduler._s_job/_s_node/_best_node` |
| Spec §3 (A) | ρ_age fairness ageing | `rho_age_rate·wait/50` in `_s_job` |
| Spec §3 (B) | Tunable ΔS threshold (Pareto knob) | `threshold` param |
| Spec §3 (C) | Reserve cap: fast ≤ 1−SLOW_RESERVE | `admit()` fast-cap check |
| Spec §3 (D) | Corrective migration by background brain | `run_brain(corrective=True)` |
| Spec §3.3 | 60 s micro-profiling on cache MISS only; Fast Path silent | `admit()` stall logic |
| Spec §4.6 | Hybrid interval + event trigger | engine `run_smart` brain trigger |
| Spec §4.7 | Decentralised handshake before migration | `run_brain` capacity check + `handshake_rejects` |
| Spec §7 | Brain reuses FFT's exact ILP | `SmartScheduler.brain = FFTScheduler(...)` |

## Deliberate simplifications (and why they're fair)

- **Host-level placement (paper §4.3, the Placer) is not modelled.** Both the
  FFT scheduler and your architecture make *GPU-type-level* decisions; the
  paper's placer is a downstream host-packing heuristic that would apply
  identically to both systems, so omitting it does not bias the comparison.
  The paper quantifies its effect at ~5–12% throughput, orthogonal to your gap.
- **Dataset fetching (paper §4.4.2)** likewise applies equally to both sides.
- **No real training**: models are parameter tuples; completion is computed
  from profiled throughput (this is also how the paper's own simulator works,
  which they validated at ≤4.9% JCT deviation from their physical cluster).

## Files

| File | Role |
|---|---|
| `workload.py` | Philly-statistics traces, model zoo, **overhead models** (LAN transfer, profiling) |
| `fft_baseline.py` | FFT scheduler, paper-exact fairness, measured solve-cost model |
| `fast_solver.py` | Exact in-process ILP (LP fast path + HiGHS MILP) — same optimum as CBC |
| `smart_scheduler.py` | Your architecture: dispatcher, dual path, reserve, ρ_age, cache+micro-profiling, brain, handshake |
| `engine.py` | Discrete-event drivers, stall-aware execution, all metrics |
| `run_experiment.py` | Headline / scale / round-length studies + parameter sweep (`sweep_results.csv`) |
| `calibrate_solver.py` | Re-measure the ILP solve-cost model on your hardware |

## Run it

```bash
pip install scipy numpy
python3 run_experiment.py                      # headline + scale + round-length
python3 -c "from run_experiment import param_sweep; param_sweep(seeds=(1,2))"
```

## Validation snapshot (36 GPUs, 80–100 jobs, seed-averaged)

- All jobs finish on both schedulers in every regime.
- Steady/mixed: JCT ×1.00–1.03, FTF ×0.99–1.06 — with realistic profiling
  overhead modelled, the architecture now *matches* FFT in steady state because
  the historical cache eliminates ~13× of FFT's profiling loss.
- Bursty: JCT ×1.09–1.14, FTF ×1.22–1.37 — the known worst case; starvation
  rises ~3× (report this plainly).
- Admission-decision speedup ×1.2–1.6 at lab scale (grows with scale and with
  shorter rounds — see `scale_study()` and `round_length_study()`).
- Migration time lost cut ~2× vs FFT; handshake rejections are counted (the
  visible price of two placement authorities).
