# Phase 2 — Dynamic Job Arrivals on the Two-Machine Cluster

Research gap: *limited adaptability to dynamic job arrivals with centralized
scheduling.* Baseline: FFT (Mo, Xu & Lau, ICS '25). Cluster: PC 1 controller +
PC 2 with 36 emulated GPU slots (T4/V100/A10, 12 each), Cat 8 direct link.

---

## 0. Read this first: the existing suite does not test the gap

`workload.generate_trace` branches only on `"steady"` and `"bursty"`. Every
other regime name falls silently through to the stationary `"mixed"`
generator. Verified by hashing the generated traces:

```
steady    aa8575549c50
mixed     f7c8bf6a8b8f
bursty    1e09d0009b2a
dynamic   f7c8bf6a8b8f   <-- byte-identical to mixed
heavy     f7c8bf6a8b8f   <-- byte-identical to mixed
random    f7c8bf6a8b8f   <-- byte-identical to mixed
```

Two consequences:

1. The 36-run suite is **18 distinct experiments duplicated**, not 36. Report
   it that way. An examiner reading `workload.py` finds this in two minutes,
   and finding it yourself is a much better position than having it found.
2. The regime literally named `dynamic` has a **constant arrival rate**.
   Nothing collected so far tests the research gap. That is what Phase 2 is
   for.

`bursty` is worth a sentence of care too. It clusters arrivals around random
centres, which raises the *variance* of the arrival process, but the rate is
still stationary in expectation. Variance is not non-stationarity, and the
distinction is exactly the one your gap turns on. Say so before you are asked.

The patched `workload.py` now prints a loud warning on any unrecognised regime
name, so this class of error cannot recur silently.

---

## 1. What "dynamic" means here, and why this construction

Arrivals follow a **non-homogeneous Poisson process** with a time-varying
intensity λ(t). Sampling uses **inverse transform on the cumulative intensity**,
not thinning.

That choice matters and you should be able to defend it. Thinning (Lewis &
Shedler 1979) is the usual NHPP method, but it yields a *random* number of
arrivals. Two schedulers would then see traces carrying different total work,
and the `done_count >= total_jobs` completion criterion would compare unequal
runs. The conditional-uniformity property of the NHPP (Çinlar, *Introduction to
Stochastic Processes*, Ch. 4) says that conditioned on N(H) = n, the n arrival
epochs are distributed as n i.i.d. draws with density λ(t)/Λ(H). Drawing n
uniforms and inverting Λ therefore gives **exactly n jobs with the exact shape
of λ(t)**. Textbook, not a workaround.

Non-stationarity is applied in **two dimensions**, because real surges change
what arrives, not merely how much: each phase carries its own model-mix weights
and a worker-request scale factor. Job length `W_j` keeps an **identical**
log-normal distribution in every phase — deliberately, so that total work is
attributable to the arrival process and any JCT difference is a scheduling
effect rather than a silent change in job size.

### The four regimes

| regime | λ(t) | mix / workers | isolates |
|---|---|---|---|
| `dyn_step` | 1.0 → 2.2 → 1.0, surge = middle quarter | shifts heavier during surge | step response: dead time, overshoot, settling |
| `dyn_sine` | 1 + 0.55·sin, three periods | **held constant** | pure rate tracking, no composition confound |
| `dyn_mmpp` | 2-state CTMC, HIGH = 2.0×, exponential holding | **held constant** | robustness with no lookahead possible |
| `dyn_flash` | three sharp Gaussian impulses on a trickle | shifts heavier in impulses | impulse response, overload recovery |

`dyn_sine` and `dyn_mmpp` vary rate alone on purpose. `dyn_step` moves rate and
composition together, which is realistic but confounded; having both lets you
answer "is it the rate or the job mix?" with data rather than assertion.

The three existing regimes are untouched and still hash byte-identically, so
they serve as the **stationary control arm**.

---

## 2. Why these metrics, and two traps I hit building them

`analyze_results.metrics` reports run-wide means. Under a load shock those
average the calm stretches together with the stressed one: a scheduler that
handles a surge badly for twenty rounds and well for sixty still posts a
respectable mean. The gap is about behaviour *during* the change, so the
measurement must be windowed and cohort-conditioned.

### The framing: step response

A load step is a step input; the cluster is the plant; the scheduler is the
controller. A round-gated centralized scheduler has a structural **dead time**
of up to one full round before it can observe a new arrival, and corrects in
discrete round-sized kicks. The classical descriptors transfer directly and
give an examiner familiar vocabulary: dead time, overshoot, settling time.

`dyn_stranded_gpu_rounds` is the single most damning metric: GPU-rounds that sat
idle **while jobs were queued**. A GPU freeing at the start of a round cannot be
reassigned until the next solve, so the capacity is simply lost. That is a pure
work-conservation violation and it is the research gap expressed as a number.
It is the one metric that needs `probe.py`, because slot occupancy is mutated
inside the PC 2 agents and cannot be reconstructed from the event log. Backlog
over time *can* be reconstructed exactly, and is.

### Trap 1 — the ratio form punishes the faster scheduler

The obvious adaptability metric is `shock_cohort / baseline_cohort` within one
scheduler. It is unusable here. SMART's calm-period time-to-first-execution is
~0.003 rounds, so a perfectly good 0.5-round shock response reports as a **160×
degradation**, while FFT's 0.5-round calm baseline makes the identical 0.5-round
response look like **1.0×**. The ratio would hand FFT a win for being slow all
the time.

The headline is therefore **additive excess in rounds**:

```
excess(metric) = metric(shock cohort) - metric(baseline cohort)     [rounds]
```

measured within one scheduler, then compared across. Any constant absolute-JCT
advantage cancels, which is what lets this claim survive the honest admission
that SMART does not beat FFT on absolute JCT. Floored ratios are still reported
as secondary; do not headline them.

### Trap 2 — the same asymmetry in settling time

Defining "settled" as *backlog returns to this scheduler's own pre-shock level*
sets SMART a stricter bar than FFT, because SMART idles at backlog 0 and FFT at
1–2. Settling now uses a **common absolute target** (backlog ≤ 1) for both.

Both traps share a shape: any metric defined relative to a scheduler's own
baseline will reward whichever scheduler has the worse baseline. Worth stating
in the methodology chapter — it is a genuine methodological contribution, not
just housekeeping.

---

## 3. Calibration — and why it is not optional

An uncalibrated dynamic run is wasted cluster time, in both directions:

* **too light** — both schedulers place everything instantly, the surge leaves
  no trace, and they look identical;
* **too heavy** — the surge saturates, every metric collapses into raw queueing
  delay, and you are measuring cluster size rather than scheduling policy. Both
  schedulers converge, because neither can conjure GPUs that do not exist.

Target bands, enforced by `calibrate_load.py`:

| quantity | band | reason |
|---|---|---|
| calm-phase ρ | 0.25 – 0.35 | the baseline cohort must be a genuinely loaded cluster; it is the denominator of every comparison |
| surge-phase ρ | 1.3 – 2.4 | transient overload the cluster **can** work off |
| run-wide ρ | ≤ 0.85 worst seed | the backlog provably drains, so settling time is finite and measurable |

Three things this exposed that are worth recording in the thesis:

1. **In the stock replayer, ρ is mathematically independent of `n_jobs`.**
   `horizon = n_jobs × 1.4`, so scaling job count scales the horizon with it and
   the two cancel. There was no way to calibrate utilisation at all. Hence the
   new `EMU_HORIZON_PER_JOB` knob — default 1.4, so every existing regime
   replays byte-identically.
2. **Modelling work at each job's fastest GPU understates ρ by roughly 40%.**
   A loaded cluster spills jobs onto slower hardware. The calibrator uses
   slot-weighted effective throughput across feasible types.
3. **A long gentle surge is capacity-bound and discriminates poorly.** The step
   surge was shortened from a third of the horizon to a quarter for this reason.

Calibrated settings for `n_jobs = 100`, 36 GPUs, `TIME_SCALE = 100`:

```
dyn_step   EMU_HORIZON_PER_JOB=1.84   horizon 184 r   calm 0.34 / surge 1.89
dyn_flash  EMU_HORIZON_PER_JOB=1.84   horizon 184 r   calm 0.33 / surge 2.31
dyn_sine   EMU_HORIZON_PER_JOB=2.19   horizon 219 r   calm 0.27 / peak 0.96
dyn_mmpp   EMU_HORIZON_PER_JOB=2.19   horizon 219 r   calm 0.24 / high 0.88
```

`dyn_sine` and `dyn_mmpp` peak just below ρ = 1. That is intended: they test
*tracking* and *unpredictability*, not overload. `dyn_step` and `dyn_flash`
supply the overload. Re-run the calibrator if you change `n_jobs`,
`N_PER_TYPE` or the model zoo.

`TIME_SCALE` stays at **100**. Changing it would break comparability with the
static suite, and the project's own finding that 400× corrupts results stands.

---

## 4. Code changes — four files patched, three added, ILP core untouched

The attribution argument from V08 is preserved exactly: `ilp_core.py`,
`brain.py`, `dispatcher.py`, `fft_scheduler_proc.py` and `node_agent.py` are
**not touched**. Any Phase 2 difference is attributable to the arrival process.

**Added**

| file | role |
|---|---|
| `arrival_process.py` | the four non-stationary generators; contrast constants are the only tuning knobs |
| `dynamic_metrics.py` | cohort-conditioned and step-response metrics |
| `probe.py` | 5 Hz read-only cluster-state sampler (free slots over time) |
| `calibrate_load.py` | solves `HORIZON_PER_JOB` per regime; refuses to hide saturation |
| `dryrun_dynamic.py` | offline wiring test, no cluster needed |
| `run_dynamic.py` | the Phase 2 runner |
| `compare_dynamic_all.py` | seed-averaged thesis table |
| `docker-compose.pc1.dynamic.yml` | adds the probe service |

**Patched**

| file | change |
|---|---|
| `workload.py` | dispatch dynamic regimes; warn loudly on unknown names. Stationary traces verified byte-identical. |
| `trace_replayer.py` | `EMU_HORIZON_PER_JOB` (default 1.4); publish the arrival plan to Redis |
| `orchestrator.py` | compute adaptability metrics **before** shutdown, record full run config |
| compose | probe service, in **both** profiles |

The orchestrator change is load-bearing. Redis runs without persistence, so
`docker compose down` destroys `metrics:events`. Anything not extracted before
shutdown is gone and the run must be repeated.

The probe runs under **both** profiles or neither. Running it only under `smart`
would add Redis load to one arm and not the other, and a reviewer is entitled to
ask whether that is what moved the numbers.

---

## 5. Execution

**PC 2 — once, then leave it.** The auto-reset loop in `node_agent.py` handles
state between runs.

```powershell
docker compose up -d --build
```

**PC 1 — before burning cluster time:**

```powershell
python calibrate_load.py all --jobs 100 --seeds 1,2,3   # confirm the bands
python dryrun_dynamic.py dyn_flash 1                    # confirm the wiring
python run_dynamic.py --dry-run                         # confirm the plan
```

**Then:**

```powershell
$env:EMU_N_PER_TYPE="12"; $env:EMU_TIME_SCALE="100"
python run_dynamic.py --tier 1          # 12 runs, ~2.5 h
python run_dynamic.py --tier 2          # 12 more, ~2.5 h
python compare_dynamic_all.py --verbose --csv results_dynamic.csv
```

Tier 1 (`dyn_step` + `dyn_flash`, 3 seeds, 2 schedulers) answers the research
gap on its own. Run it first and look at the table before committing the second
evening. `run_dynamic.py` resumes: a run counts as complete only if every job
finished **and** the adaptability metrics were extracted, so a timed-out run is
re-run rather than silently skipped.

---

## 6. Statistics, honestly

Three seeds is three samples. The t(2) 95% critical value is 4.303, so intervals
are wide and a p-value from them would be close to meaningless. In the synthetic
wiring test, a mean of 2.736 carried a half-width of ±4.94 — that is what n = 3
buys you on a high-variance trace, and the table shows it rather than hiding it.

FFT and SMART see the **identical trace** for a given (regime, seed), so the
comparison is **paired**, which is far more informative than the unpaired means.
The defensible claim is:

> SMART was better on all three paired seeds, by a mean margin of X rounds.

not "p < 0.05". Where a metric splits 2–1, say so and call it inconclusive.
If you want a real significance claim the cost is more seeds, not a different
test: 5 seeds per cell adds roughly 2 hours.

A cell is marked `CONFIRMED` only when all three paired seeds completed with
every job finished; otherwise `NOT QUOTABLE`.

---

## 7. What the dry run predicts, and the limitation to volunteer

An offline capacity model over the real traces (a wiring test — **not**
quotable, and it ignores migration, preemption, fairness and the ILP entirely):

```
                        dyn_flash              dyn_step
                      FFT    SMART           FFT    SMART
excess TTFE          4.92     2.26          6.38     5.76
shock-cohort TTFE    5.57     2.51          7.16     5.77
reaction (rounds)    0.43    0.004          0.24    0.000
backlog peak           11        8             9        7
```

Separation is large for sharp impulses and shrinks as the surge becomes a
sustained plateau. **Volunteer this before an examiner finds it.** The
explanation is the strong part of the answer: sustained overload is
capacity-bound, not policy-bound, so no admission policy can help once the
cluster is the binding constraint. Decentralised, event-driven admission buys
you the *transition* — the reaction to change — which is precisely and only
what the research gap claims.

Other limitations to state before results, per the project standard:

* Emulated GPUs. No real tensors, no real gradients, no real NCCL traffic.
  Throughputs come from a model zoo, not measurement.
* The arrival processes are synthetic and parameterised by me. They reproduce
  the *statistical character* of non-stationary cluster load; they are not a
  replay of a real production trace. The FFT paper's own Trace 7 ("highest
  intensive arrival rate") is a comparable device, which is the precedent to
  cite.
* n = 3 seeds. Stated above.
* The probe adds a small, identical instrumentation load to both arms.
* `dyn_step` confounds rate with composition by design; `dyn_sine` and
  `dyn_mmpp` are the clean rate-only controls that let you separate them.

---

## 8. The claim this phase supports

Not "SMART beats FFT." The claim is narrower, defensible, and matches the gap:

> Under non-stationary arrivals, a centralized round-gated scheduler pays a
> structural dead time of up to one round before it can respond to a change in
> load, and strands capacity while jobs queue. Decentralised event-driven
> admission removes the dead time and reduces the excess wait, backlog peak and
> stranded capacity attributable to the transition, while matching FFT's
> run-wide JCT to within measurement noise. The advantage is largest for sharp
> arrival bursts and diminishes as the surge becomes sustained, because
> sustained overload is capacity-bound rather than policy-bound.

The last clause is the one that makes the rest credible.
