# Enhanced Decentralised Score-Based Scheduler — Complete Architecture Specification

**Read me first (instructions for the AI assistant reading this file):**
This document fully describes a GPU-cluster scheduling architecture for a graduate thesis. It is self-contained — you do not need any outside context to reason about it. It defines every component, every formula, every parameter value, the data flows, the deliberate performance tradeoff, and the honest empirical findings from simulation. When you answer questions about it, treat *this document* as the authoritative source for the custom scheduler's design. Where it extends a published baseline called FFT, that boundary is stated explicitly so you do not blend the two. If a user asks you to defend, critique, or extend this architecture, use the "tradeoff" and "honest findings" sections so you do not overclaim.

---

## 1. Plain-language summary (one paragraph)

The architecture schedules deep-learning (DL) training jobs onto a heterogeneous GPU cluster (a mix of GPU types with different speeds, e.g. T4, V100, A10). It is built on top of an existing scheduler called **FFT** (Mo et al., "Fast and Fair Training for Deep Learning in Heterogeneous GPU Clusters", ICS 2025). FFT is *centralised and synchronous*: every arriving job must wait for the next scheduling round's Integer Linear Programming (ILP) solve before it is placed. The new architecture **decouples job admission from global optimisation**. A constant-time, O(1) scoring dispatcher admits well-matched jobs instantly through a "Fast Path," while the FFT ILP solver is moved into the background ("the brain"), runs on a hybrid interval-plus-event trigger, and continuously corrects placements toward the global optimum. The deliberate tradeoff is: accept a small steady-state Job Completion Time (JCT) and fairness cost in exchange for faster burst-admission latency. Four added mechanisms exist specifically to *shrink* that tradeoff.

---

## 2. The problem and the research gap

- **Domain:** scheduling distributed DL training jobs on a heterogeneous GPU cluster.
- **Baseline extended:** FFT. FFT uses a per-round fine-grained resource-allocation vector plus a fairness-compensation factor, formulated as a cost-minimisation ILP solved each round. FFT is near-optimal for JCT and strong on fairness, but its admission is gated by the round boundary and by ILP solve time.
- **Research gap targeted (verbatim):** *"Limited adaptability to dynamic job arrivals with centralised scheduling."* Under bursty arrivals, FFT's centralised, round-synchronous admission creates head-of-line queueing delay because every new job waits for the global solver. This architecture attacks that specific bottleneck.

---

## 3. Symbols and terminology (complete glossary)

| Symbol / term | Meaning |
|---|---|
| `d_j` | Number of GPU workers requested by job `j`. |
| `W_j` | Number of training epochs required by job `j`. |
| `θ_j^i` (theta) | Throughput of job `j` on GPU type `i`, in epochs completed per scheduling round. Measured by profiling. |
| `C_n` | Compute-capability score of node `n` (a function of its GPU type). |
| `A_n` | Real-time availability of node `n` (free fraction). |
| `S_job` | Job Demand Score (how "heavy" a job is). |
| `S_node` | Node Capacity Score (how capable and free a node is). |
| `ΔS` (delta-S) | Match distance, `|S_node − S_job|`. Smaller = better fit. |
| `α, β` (alpha, beta) | Weights inside the Job Demand Score. |
| `γ, δ` (gamma, delta) | Weights inside the Node Capacity Score. |
| `ρ_age` (rho-age) | **NEW.** A fairness/ageing term added to `S_job` that grows the longer a job waits. A lightweight echo of FFT's compensation factor. |
| `ρ_j(t)` (rho-j) | FFT's original fairness compensation factor, accumulated inside the background solver's objective. |
| `s_j^i(t)` | FFT's switching/migration penalty term (discourages over-frequent migration). |
| `SLOW_RESERVE` | Fraction of total cluster capacity reserved for globally-scheduled (Slow-Path) jobs. The Fast Path is capped at `1 − SLOW_RESERVE`. |
| Fast Path | Instant local admission for well-matched jobs; no global solve at admission time. |
| Slow Path | Queue + profiling route for mismatched or oversized jobs; placed by the background ILP. |
| JCT | Job Completion Time. Primary efficiency metric. Lower is better. |
| Makespan | Time to complete all jobs in a trace. |
| FTF | Finish-Time-Fairness, `ρ = T_sh / T_id` (shared-cluster completion time ÷ exclusive-cluster completion time). Primary fairness metric. Lower / tighter is better. |
| Starvation time | Time from job submission until its first execution. |

---

## 4. The seven components (full detail)

The system has seven logical components. Components 1–7 below; the four NEW upgrade mechanisms are embedded inside components 2, 3, and 6 rather than being standalone stages.

### Component 1 — User job submission
Entry point. A user submits a DL training job with metadata: requested workers `d_j` and required epochs `W_j`. The user does **not** supply throughput; throughput `θ_j^i` is measured later by profiling.

### Component 2 — Fairness-aware scoring dispatcher (the O(1) front-end)
The constant-time controller that decides each job's route. On arrival it computes two scores and a match distance.

**Job Demand Score (contains the NEW `ρ_age` term):**
```
S_job = α · d_norm + β · w_norm + ρ_age
```
where, in the built simulation:
- `d_norm = d_j / D_MAX`, with `D_MAX = 8` (max workers, for normalisation),
- `w_norm = min(1, W_j / W_MAX)`, with `W_MAX = 40` (typical max epochs),
- `ρ_age = rho_age_rate · (wait / 50)`, where `wait = current_time − arrival_time`.

**Node Capacity Score:**
```
S_node = (γ · cap + δ · avail) / (γ + δ)
```
where `cap = C_n / CAP_MAX` (normalised capability, `CAP_MAX = 2.7` = A10's speed), and `avail = free_workers / total_workers` on that node type.

**Match distance:** `ΔS = |S_node − S_job|`.

Both scores are normalised to a comparable ~[0,1] range *on purpose*, so the `ΔS` threshold is meaningful. The dispatcher routes the job by comparing `ΔS` to a threshold (see the NEW threshold mechanism below). It stays O(1): it does **not** search the full `M·N·T` allocation space (GPU types × jobs × rounds) that the global ILP explores — it only does cheap score matching.

**NEW mechanism A — `ρ_age` fairness-aware scoring.** Adding `ρ_age` into `S_job` makes the *front-end itself* respect fairness: a job that has been waiting gets a rising score, so it is more likely to be matched and admitted rather than left behind. This is the structural fix for the fairness (FTF) degradation that pure greedy matching caused. Built default: `rho_age_rate = 0.3`.

**NEW mechanism B — tunable ΔS threshold (the Pareto knob).** The threshold on `ΔS` is the single most important control dial. It governs how much arriving traffic skips the global solver:
- Threshold too loose → almost every job takes the Fast Path → excellent latency, but worse JCT/FTF (placements are greedy/local).
- Threshold too tight → almost every job queues for the global solver → JCT/FTF approach FFT's, but the latency advantage is lost (the system degenerates back toward FFT).
- The best operating point is the "knee": where the latency-improvement curve has flattened (most benefit captured) but the JCT/FTF curves have not yet steeply degraded. Sweeping this threshold traces the system's Pareto frontier. Built default: `threshold = 0.15`.

### Component 3 — Decentralised zone (Fast Path)
For jobs whose `ΔS` is within the threshold **and** that fit available capacity **and** that keep the Fast Path under its reserve cap.

- **Local Placer:** immediately locks GPU slots on the matched node. This is the source of the zero-scheduling-delay admission and the burst-latency advantage. No global ILP solve is consulted at this moment.
- **NEW mechanism C — reserve budget cap.** The Fast Path is not allowed to consume the whole cluster. It is capped at `1 − SLOW_RESERVE` of total capacity. `SLOW_RESERVE` protects capacity for globally-scheduled big jobs and prevents Fast-Path traffic from starving the Slow Path. It is itself a tunable dial: higher reserve → better fairness, slightly worse Fast-Path latency; lower reserve → better latency, worse fairness. Built default: `slow_reserve = 0.25`.
- **GPU workers:** the heterogeneous execution substrate (T4 / V100 / A10). Placed jobs run here, accumulating epochs at rate `θ_j^i` per round.

### Component 4 — Queuing and profiling zone (Slow Path)
For jobs that mismatch (`ΔS` over threshold), do not fit, or arrive when the Fast Path is at its reserve cap.

- **Historical cache check (Redis):** look up whether this job's model has been profiled before.
  - **HIT:** reuse the known throughput matrix `θ_j^i`; the job proceeds straight to the Global Queue.
  - **MISS:** the job is sent to brief micro-profiling to measure `θ_j^i`, then queues.
- **Micro-profiler:** runs a short profiling pass to obtain throughput and training-state size for unseen jobs. Caching avoids the repeated, disruptive on-the-fly profiling that FFT performs (FFT's profiling has top priority and suspends running jobs).
- **Global Queue:** a safe "parking" area where jobs wait *with their profile data attached*, ready for the background solver to place them optimally. A job entering the queue fires an event trigger that wakes the background brain immediately.

### Component 5 — Global job recorder (Redis), shared state
The shared-memory backbone. A Redis key-value store tracking:
- Live GPU availability (read by the dispatcher to compute `S_node` and `A_n`).
- The profiling-matrix cache (`θ_j^i` per model).

Read by the dispatcher (to score nodes) and read/written by the background brain (to read job state and write allocation decisions). It is the single source of truth that lets a decentralised front-end and a centralised background brain stay consistent.

### Component 6 — Hybrid event-driven FFT scheduler (the background "brain")
The original FFT ILP solver, **mathematically unchanged**, but relocated to the background and re-triggered differently.

- **Interval trigger:** runs round-by-round housekeeping as FFT normally does.
- **Event trigger (new behaviour vs baseline):** the instant a job enters the Global Queue, an event signal wakes the solver immediately, instead of making the job wait for the next interval. This removes queue idle time for large jobs — the solver computes the migrations needed to clear a path for the queued workload right away.
- It solves FFT's cost-minimisation ILP (balancing JCT via `φ_j^i(t)`, switching via `s_j^i(t)`, and fairness via `ρ_j(t)·θ_j^i`) to decide allocations and migrations.

**NEW mechanism D — corrective migration loop.** A Fast-Path placement is *initial*, not permanent. The background ILP is allowed to **re-place Fast-Path jobs** it judges globally suboptimal, subject to FFT's existing migration-overhead penalty `s_j^i(t)`. This separates two concerns: "admit fast" (Fast Path, O(1), instant) and "converge to optimal" (background ILP, corrective). It is the main mechanism for recovering FFT's JCT optimality *after* fast admission, while keeping the latency win. Migration overhead must be watched so corrective re-placement does not claw back JCT only to spend it on state-transfer churn; the switching penalty and the handshake (Component 7) bound this. Built default: `corrective = True`.

### Component 7 — Decentralised handshake (safety / consistency)
Two placement authorities now exist: instant Fast-Path locks, and background migrations. They can collide on the same hardware. Before any background migration executes, the target node's Local Placer performs a hardware-level availability check (a handshake) and only then accepts the migration. This eliminates race conditions and prevents GPU time being wasted resolving placement collisions. (The centralised baseline never has this conflict because it has a single placement authority; the handshake is the price of decentralisation.)

---

## 5. End-to-end data flow

**Fast Path (well-matched job):**
1. Job arrives with `d_j`, `W_j`.
2. Dispatcher computes `S_job` (including `ρ_age`), reads node state from Redis, computes `S_node` and `ΔS`.
3. If `ΔS ≤ threshold` AND the job fits AND the Fast Path is below its `1 − SLOW_RESERVE` cap → Fast Path.
4. Local Placer locks GPU slots instantly; the job runs on GPU workers.
5. Placement and availability are written to the Redis recorder.
6. The background brain may later issue a *corrective migration* if this placement is globally suboptimal — gated by the handshake.

**Slow Path (mismatched or oversized job):**
1. Job arrives with `d_j`, `W_j`.
2. Dispatcher computes `ΔS`; it exceeds the threshold, or the job doesn't fit, or the Fast Path is at its cap → Slow Path.
3. Historical cache check: HIT → reuse `θ_j^i`; MISS → micro-profile to obtain `θ_j^i`.
4. Job parks in the Global Queue with profile data attached.
5. Entering the queue fires an event trigger that immediately wakes the background ILP.
6. The ILP solves the cost-minimisation problem and issues placement / migration requests.
7. Each migration is verified by the decentralised handshake before executing on GPU workers.
8. State updates are written back to Redis.

---

## 6. The four upgrade mechanisms mapped to the tradeoff each reduces

| # | Mechanism | Tradeoff it attacks | How it helps | Built default |
|---|---|---|---|---|
| A | Fairness-aware scoring (`ρ_age` in `S_job`) | FTF degradation | Makes the Fast Path itself respect fairness instead of bypassing it; a lightweight echo of FFT's `ρ_j(t)`. | `rho_age_rate = 0.3` |
| B | Tunable `ΔS` threshold | Latency vs JCT/FTF balance | The Pareto knob; tuned to the knee where most latency benefit is captured before JCT/FTF degrade steeply. | `threshold = 0.15` |
| C | Reserve budget cap (`SLOW_RESERVE`) | Fast-path starvation / FTF | Reserves capacity for globally-scheduled big jobs; tunable fairness-vs-latency dial. | `slow_reserve = 0.25` |
| D | Corrective migration loop | Steady-state JCT degradation | Background ILP re-places suboptimal Fast-Path jobs, recovering JCT optimality after fast admission, bounded by the switching penalty. | `corrective = True` |

**Critical caveat (keep this in any downstream reasoning):** these mechanisms *narrow* the tradeoff but do **not** eliminate it. Any job admitted without consulting the global optimum is, by definition, at risk of being globally suboptimal — that is the irreducible core of the design. The correct claim is that the tradeoff is *characterised and minimised* (pushed toward the Pareto frontier), not removed.

---

## 7. Dispatcher score-weight parameters (for tuning)

These are the parameters a user sweeps to find an optimal operating point. Built defaults shown.

| Parameter | Role | Default |
|---|---|---|
| `α` (alpha) | Weight on requested workers `d_j` in `S_job` | 1.0 |
| `β` (beta) | Weight on epochs `W_j` in `S_job` | 0.5 |
| `γ` (gamma) | Weight on node capability `C_n` in `S_node` | 1.0 |
| `δ` (delta) | Weight on node availability `A_n` in `S_node` | 1.0 |
| `threshold` | ΔS routing threshold (Pareto knob) | 0.15 |
| `slow_reserve` | Fraction reserved for the Slow Path | 0.25 |
| `rho_age_rate` | Fairness ageing growth rate | 0.3 |
| `corrective` | Enable background re-placement of Fast-Path jobs | True |

A parameter sweep runs many combinations of these over multiple random seeds and records JCT, FTF, starvation, admission latency, and the fast-path fraction, plus ratios versus the FFT baseline, so the user can pick the operating point (for example, minimum JCT subject to a latency floor, or the Pareto-efficient set).

---

## 8. Relationship to the FFT baseline (do not conflate)

- **Mathematics unchanged:** the background brain uses FFT's exact cost-minimisation ILP — JCT term `φ_j^i(t)`, switching term `s_j^i(t)`, fairness term `ρ_j(t)·θ_j^i` — and FFT's opportunistic-migration machinery.
- **What changed is orchestration, not the optimiser:** admission is decoupled from the solve; the solver is backgrounded and event-triggered; a fast heuristic front-end, a reserve cap, a handshake, and a corrective loop are added around it.
- **FFT's guarantees apply to the brain's decisions, not to Fast-Path admissions.** FFT's theoretical bounds (number of unfinished jobs ~ O(√N); bounded JCT) hold for globally-scheduled jobs. Fast-Path placements are heuristic and carry no such guarantee until/unless corrected by the background loop. This is why *this document*, not the FFT paper, is authoritative for Fast-Path behaviour.

---

## 9. Simulation / evaluation method (for fidelity)

- **Method:** custom Python discrete-event simulation. It does **not** run real neural-network training; models are parameter tuples and completion times are computed mathematically from profiled throughput.
- **Cluster model:** heterogeneous nodes (T4 / V100 / A10), virtualised as lightweight Python data structures; Redis global state likewise modelled. Default cluster: 12 GPUs of each type = 36 GPUs (the lab-scale cluster), with simulated scaling up to ~1000 GPUs.
- **GPU model (built values):** T4 speed 1.0 / 16 GB; V100 speed 2.0 / 32 GB; A10 speed 2.7 / 48 GB. Capability scores `C_n` equal the speeds (1.0, 2.0, 2.7). Memory feasibility forbids placing a model on a GPU type with too little memory.
- **Workload:** synthetic traces replicating the statistical properties of the Microsoft Philly trace — Poisson-like arrivals, log-normal processing times, variable worker counts. The trace supplies *distributions only*; model-specific characteristics come from a small model zoo (ResNet, VGG, DenseNet, BERT, GPT-neo, GPT-2, OPT). Three arrival regimes: steady, mixed, bursty.
- **Baselines for comparison:** FFT (primary), and conceptually Gavel, AlloX, PAL, Tiresias.
- **ILP solver:** CBC via PuLP. The FFT per-round solve cost is **measured**, not assumed: a linear model `solve_ms ≈ 27.7 + 0.093 · n_active` was fitted to real CBC timings on the lab machine (3 GPU types). This is converted to a fraction of the scheduling round (default round = 5 minutes = 300 s) when charging FFT's synchronous-admission cost.

### Two important simulation lessons (design constraints discovered during development)
1. **Work-conservation must be a strong reward, not a hard ILP constraint.** As a hard constraint it made the solver infeasible in tight regimes; as a strong scheduling reward in the objective it keeps GPUs busy while the cost terms set priority. Without a scheduling reward, minimising positive costs drives the solver to schedule nothing (all `x = 0`).
2. **Fast-path starvation required the hard global budget cap (`SLOW_RESERVE`).** Without it, Fast-Path traffic could consume the whole cluster and starve big globally-scheduled jobs.

---

## 10. Honest empirical findings (important — do not overclaim)

These come from actual simulated execution with the measured FFT solve cost. Nothing was tuned to make the new architecture win.

1. **The steady-state tradeoff is small and shrinks with scale.** At 36 GPUs the upgraded scheduler costs roughly ×1.01–1.10 JCT and ×1.07–1.19 FTF versus FFT. By ~300 GPUs / 600 jobs these ratios fall toward ~×1.01 — the cost nearly vanishes at scale.

2. **The admission-latency advantage is MODEST at lab scale, not order-of-magnitude.** Measured speedup is about ×1.2–2.2 across regimes and round lengths at realistic sizes, rising to ~×3.7 with tuned dispatcher weights. The reason is physical: with a realistic 5-minute scheduling round, the FFT ILP solve (~28 ms + 0.09 ms/job, measured) is negligible relative to the round, so FFT's decision latency is dominated by round-boundary waiting, which the Fast Path removes but only by about half a round.

3. **A very large (e.g. ~480×) advantage is only physically reachable when the solve dominates the round** — i.e. very large clusters (thousands of jobs) or sub-second reactive scheduling. At 36 GPUs with 5-minute rounds it is not supported by the measured solve cost.

**Recommended framing (use this when defending the work):** present the advantage as a *function of scale and round length*, not a single headline multiplier. "The advantage grows as the cluster grows and as scheduling becomes more reactive; at lab scale it is modest, but the steady-state cost is also near-zero" is a defensible, accurate claim. The architecture gets better on *both* axes as scale increases — the cost shrinks while the latency benefit grows.

**Metric definitions used (so numbers are interpreted correctly):**
- **admission_latency = scheduling-DECISION latency** = time from arrival until the scheduler decides a placement. FFT pays (wait to next round) + (measured solve time, which grows with active-job count). The Fast Path is O(1) at arrival; a Slow-Path job is decided when the event-triggered brain next runs, and the brain's solve does not block that decision because it runs in the background. This is deliberately **not** the time until a free GPU appears.
- **starvation** = time from arrival to first execution (capacity-bound; similar across schedulers because it is governed by physical GPU availability). Reported separately to keep decision-latency honest.
- **JCT / makespan / FTF** = measured from actual simulated execution.

---

## 11. Minimal mental model (if everything above is too long)

Think of it as FFT with a fast front door. A cheap O(1) scorer lets well-matched jobs walk straight in and start running (Fast Path), while FFT's expensive optimal solver runs in the background, woken instantly whenever a hard job arrives, and is allowed to quietly move jobs to better spots afterwards (corrective migration) without ever colliding (handshake). Four dials — an ageing fairness term (`ρ_age`), the match threshold (`ΔS`), the corrective loop, and a reserved slice of the cluster (`SLOW_RESERVE`) — let you trade a little optimality for faster admission, and tune exactly how much. At lab scale the trade is small in both directions; at large scale the benefit grows and the cost shrinks.

---

## 12. Open / not-yet-finalised items (so you don't assume they are fixed)

- The exact functional form of `ρ_age` is a design choice; the built version is `rho_age_rate · (wait / 50)`, but the constant `50` and the linear form are tunable and not theoretically derived.
- The optimal `α, β, γ, δ, threshold, slow_reserve, rho_age_rate` values are found empirically by sweeping; the defaults above are reasonable starting points, not proven optima.
- The measured solve-cost model is hardware-specific and should be re-measured on the target machine before quoting latency numbers.
