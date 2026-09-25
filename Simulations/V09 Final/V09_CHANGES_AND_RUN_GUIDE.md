# V09 — Bug Fixes, Architecture Improvement, and Two-Machine Validation

**Status:** tested on one machine (real processes, real Redis, 40 jobs,
`dyn_flash`, seed 1, including an injected 2-second clock skew). **Not yet
run on the two-PC cluster. Nothing here is quotable yet.**

`ilp_core.py` and `fast_solver.py` are **unchanged** (hashes verified), so the
V08 attribution argument still holds: FFT and SMART share the same ILP, and
differences come from the architecture around it.

---

## Part 1 — What was wrong in V08

### Bug 1 (critical): the brain froze for 58 seconds at a time

**Symptom in your run:** two slow-path jobs waited ~19.5 rounds to start.
FFT's worst wait was 0.78 rounds. Last time I explained this as an
architectural trade-off (greedy placement vs. FFT's global view). **That
explanation was wrong.**

**Cause.** `_corrective_migrations` looped over a job snapshot taken *before*
`_place_queued_jobs` ran in the same wake. Suppose a job was queued when the
snapshot was taken (so `gpu=None`) and was then placed. It was no longer in
`queued_ids`, so it was treated as a running job to migrate. The brain then
sent `evict` to GPU type `None`. No agent listens on `agent:None:req`, so the
brain blocked.

**Why 58 s and not 10 s.** The Dockerfile installed `redis` unpinned, which
gives redis-py 8.x. That version automatically retries a timed-out blocking
read. I measured a `BRPOP` with `timeout=10` returning after **58.2 s**. At
100× that is 19.5 rounds, which matches your stuck jobs exactly.

**Evidence** (in `evidence/`):
- The traced brain logged `gpu_type=None op=evict took 58.5s`.
- In one 40-job reproduction the brain froze **three times**, which means it
  was dead for about 40% of the run.
- During job 36's 19.7-round wait there were **zero** ILP solves. Normally
  there would be one every 0.33 rounds.
- While the brain was frozen, the dispatcher's fast path kept working. That is
  why only slow-path jobs starved.

**Consequence:** every V08 SMART run is affected, including the Phase 1 static
suite. SMART's JCT, starvation and settling-time figures were measured with its
brain down for long stretches.

### Other bugs

| # | Bug | Effect | Who it affected |
|---|---|---|---|
| 2 | `now()` was `time.time()` on each host. Arrivals were stamped on PC 1, but `first_exec_ts` and `finish_ts` on PC 2. | Every TTFE, JCT and starvation value was off by the PC1/PC2 clock offset. Containers use the WSL2 VM clock, so `w32tm /resync` on Windows does not fix this. | Both schedulers, two-machine runs only |
| 3 | Eviction was "evict, `sleep(0.15)`, reserve". | A late checkpoint could overwrite `status=running`, so a job looked queued while running and could be reserved twice. A fast-path arrival could also take slots that had been freed for a starving job. | Both; worse across two machines |
| 4 | Agent workers survived the end of a run. | On PC 2 (agents persist between runs), a leftover worker inflated `free` and incremented the next run's `done_count`. | Both, two-machine only |
| 5 | On a cache miss, the fast path reserved with `stall_rounds=0`. | SMART skipped the 60 s profiling stall that FFT always pays. The "40× fewer profiling stalls" figure came from this. | Unfair to FFT |
| 6 | FFT slept a full round *after* each round's processing. | FFT's rounds drifted longer, more so on two machines. | Unfair to FFT |
| 7 | The result files didn't record which code produced them. | Your run used an older `arrival_process.py` with a different cohort definition, and nothing in the JSON could reveal that. | Reproducibility |
| 8 | Both earlier cohort definitions were flawed. | The original treated `dyn_flash`'s first impulse as baseline. The interim fix left a baseline of only 8 jobs. | Metrics |

---

## Part 2 — What V09 changes

| File | Change |
|---|---|
| `common.py` | Redis client has an explicit socket timeout and **no retries**, so BRPOP returns on schedule. **One cluster clock:** every process anchors to the Redis server clock (minimum-RTT sample, error ≤ RTT/2). RPCs to an unknown GPU type fail immediately and are logged. Adds a code fingerprint. |
| `node_agent.py` | Evict is **synchronous**: it acknowledges only after the checkpoint, release and requeue. **Atomic evict-and-reserve**: one request evicts the victims and then reserves, so nothing can take the slots in between. The reservation is committed before the ACK. A **run-epoch guard** retires leftover workers. Adds `first_progress_ts`. Re-syncs the clock each run and registers as `label:gpu:codehash`. |
| `brain.py` | Migration candidates are **re-read from Redis** before acting (fixes Bug 1). Preemptive placement and Mechanism E use the atomic reserve. All `sleep(0.15)` calls removed. |
| `dispatcher.py` | On a cache miss, the fast path pays the profiling stall. SMART now shows one stall per distinct model. |
| `fft_scheduler_proc.py` | Rounds fire on **fixed boundaries**. Uses the synchronous evict. Both changes help FFT. |
| `arrival_process.py` | Cohorts: **shock** = inside any stress window; **recovery** = within 10 rounds after a window closes; **baseline** = all other calm arrivals. |
| `dynamic_metrics.py` | Adds time-to-useful-work, recovery excess, and a **scheduler watchdog** (longest gap between solves). |
| `orchestrator.py` | Every result records its provenance: code hash, redis-py version, per-process clock offsets, and whether PC 1 and PC 2 code matches. Raw events are always archived to `runs/raw/*.json.gz`. Prints a warning if a run is invalid. |
| `run_dynamic.py` | A run counts as complete only if it is **valid**: no scheduler stall, no bad RPC, and matching code on both PCs. |
| `compare_dynamic_all.py` | New headline metrics. A cell is marked `CONFIRMED` only if all three seeds are valid. |
| `Dockerfile` | Versions **pinned**: `redis==8.1.0`, `numpy==2.4.4`, `scipy==1.17.1`. |

### The architecture improvement

The main improvement is **atomic evict-and-reserve inside the node agent**. In
V08, freeing capacity took three steps: the brain evicted, slept, then
reserved. The node agent — which by your own design owns its hardware — was
never actually in control of the whole operation. Now it is: one request,
handled by the only process allowed to change that slot table. This removed a
race and the slot theft by the fast path. It also puts the code in line with
the decentralised design principle you describe in the thesis.

---

## Part 3 — Single-machine validation results

Same regime (`dyn_flash`), seed (1) and job count (40) as your run.

| metric | V08 FFT (yours) | V08 SMART (yours) | **V09 FFT** | **V09 SMART** | V09 SMART +2 s skew |
|---|---|---|---|---|---|
| longest gap between solves (rounds) | — | ~19.5 (reproduced) | 1.006 | **0.363** | 0.356 |
| bad-target RPCs / RPC timeouts | — | — | 0 / 0 | **0 / 0** | 0 / 0 |
| run-wide mean JCT | 18.71 | 19.95 | 18.87 | **17.95** | 17.94 |
| TTFE p99 (rounds) | 1.03 | **19.52** | 1.18 | **0.02** | 0.02 |
| surge-job wait to start | 0.67 | 0.002 | 0.70 | **0.002** | — |
| surge-job wait to useful work | — | — | 0.90 | **0.036** | — |
| peak backlog | 6 | 3 | 6 | **1** | 1 |
| idle usable GPU-rounds while jobs queued | — | — | 330.6 | **0.0** | 0.0 |
| profiling stalls | 40 | 1 | 40 | **7** | 7 |
| preemptions | 21 | 18 | 20 | **30** | 30 |

### What this shows

1. **The freeze is gone.** SMART's longest solve gap dropped from ~19.5 to
   0.36 rounds, which matches its configured 0.3-round wake interval. The
   19.5-round tail waits have disappeared (p99 went from 19.52 to 0.02).
2. **The clock fix works.** With 2 s of skew injected into the agents, the
   agents measured their offset as −2000 ms, and **every metric matched the
   unskewed run** (JCT 17.94 vs 17.95). V08 would have added +0.67 rounds to
   every TTFE.
3. **SMART now beats FFT on run-wide JCT** in this run (0.95×), where V08 had
   it 7% worse. Treat this with caution: one seed, one machine.
4. **The profiling comparison is now honest:** 7 vs 40, not 1 vs 40.
5. **The clearest evidence for the research gap** is the idle-capacity metric.
   FFT left **330 GPU-rounds** of usable capacity idle while jobs were waiting
   for its next round; SMART left **zero**. That is the cost of centralised
   round-gating, expressed as a single number.

### Honest points to state before an examiner raises them

- **SMART now preempts more (30 vs 20).** Preemption costs checkpoint and
  migration time, and it belongs in the trade-off discussion.
- **The "excess wait, surge vs calm" metric came out near zero for both
  schedulers at this load.** FFT's wait is ~0.7 rounds for *every* job,
  whether or not there is a surge, because it is a fixed round-gating dead
  time rather than something load builds up. So the evidence for the gap is
  the **absolute** surge-job wait, reaction time, peak backlog and idle usable
  capacity — not the load-sensitivity excess. At n=100 with more load the
  excess may start to separate; report whatever the data shows.
- **Phase 1 static results were produced by V08.** State this. If time
  allows, re-run the static regimes on V09 so both phases use the same code.

---

## Part 4 — Running V09 on the two-machine cluster

### Step 1 — Put the V09 files in place (PC 1)

Back up your V08 folder first. Then copy **everything** in this folder into it,
overwriting existing files:

```
common.py  node_agent.py  brain.py  dispatcher.py  fft_scheduler_proc.py
orchestrator.py  dynamic_metrics.py  arrival_process.py  workload.py
trace_replayer.py  probe.py  compare_dynamic_all.py  run_dynamic.py
calibrate_load.py  dryrun_dynamic.py  docker-compose.pc1.dynamic.yml  Dockerfile
```

Keep your own `config.py`, `ilp_core.py`, `fast_solver.py`,
`analyze_results.py` and compose files. V09 does not change them.

### Step 2 — Copy the same folder to PC 2

Copy **every `.py` file** plus the `Dockerfile`, not just the agent files. The
agents report a hash over all `.py` files in their folder, and the run is
rejected if it differs from PC 1. That check exists to catch exactly what
happened with `arrival_process.py`.

Verify on both PCs that the command below prints the same 12 characters:

```powershell
python -c "from common import code_fingerprint; print(code_fingerprint())"
```

### Step 3 — Label the machines (optional but recommended)

Add `EMU_NODE_LABEL` to each compose file's environment block, set to `PC1` on
PC 1 and `PC2` on PC 2. The result files then show `PC2:T4:<hash>` instead of a
container ID, which proves where the agents ran.

### Step 4 — Rebuild both images

The Dockerfile changed, so a plain `up` would reuse the old image.

```powershell
# PC 2
$env:EMU_N_PER_TYPE="12"
docker compose down
docker compose build --no-cache
docker compose up -d
docker compose logs -f     # look for: Ready (epoch 1) ... clock offset ... ms
```

```powershell
# PC 1
docker compose -f docker-compose.pc1.yml build --no-cache
```

In the PC 2 logs, the line `clock offset +NNN ms` shows the real PC1/PC2
offset. V09 corrects for it, so there is nothing to fix — but **write the
number down for the thesis**. It shows how much V08's two-machine timestamps
were off.

### Step 5 — Smoke test: one short paired run

```powershell
# PC 1
$env:EMU_N_PER_TYPE="12"; $env:EMU_TIME_SCALE="100"
python run_dynamic.py --regime dyn_flash --seeds 1 --jobs 40
```

Then check that **all** of these hold in `runs/smart_dyn_flash_s1.json`:

| key | must be |
|---|---|
| `n_finished` | 40 |
| `sched_gaps_over_2_rounds` | 0 |
| `rpc_bad_target` | 0 |
| `provenance.code_match` | `true` |
| `agent_hosts` | entries starting `PC2:` (if you labelled) |
| `provenance.clock` | PC 2 entries show the real offset; PC 1 entries ~0 |

If any fail, **stop** and send me the JSON plus the matching
`runs/raw/*.events.json.gz`. The raw archive is what made this diagnosis
possible.

Delete the smoke-test files afterwards, since they are 40-job runs:

```powershell
Remove-Item runs\*_dyn_flash_s1.json, runs\raw\*_dyn_flash_s1.*
```

### Step 6 — Tier 1, the real runs

```powershell
python run_dynamic.py --dry-run
python run_dynamic.py --tier 1          # dyn_step + dyn_flash, 3 seeds, ~3 h
python compare_dynamic_all.py --verbose --csv results_dynamic_v09.csv
```

`run_dynamic.py` automatically re-runs any invalid run.

### Step 7 (recommended) — Re-run Phase 1 on V09

Phase 1 ran on V08, whose brain froze. For a consistent thesis, re-run the
three genuinely distinct static regimes (`steady`, `mixed`, `bursty`) on V09
using your existing `run_all_36.py`, restricted to those regimes. That is
18 runs rather than 36, since `dynamic`, `heavy` and `random` were copies of
`mixed`.

---

## Part 5 — The claim V09 supports (once the two-machine data confirms it)

> A centralised round-gated scheduler imposes a fixed dead time of up to one
> round on every arriving job and leaves usable GPUs idle while jobs wait for
> the next round boundary. Decentralised, event-driven admission removes that
> dead time and the idle capacity, while matching or improving on FFT's
> run-wide JCT with the same ILP core. The cost is more frequent preemption.

Along the way, the project found and fixed a class of distributed-systems
bugs that only emulation on a real network exposes: cross-host clock skew,
sleep-based synchronisation between processes, and library-level retry
behaviour hiding a failed RPC. That belongs in the thesis as a methodological
contribution. It also continues your earlier finding that emulation catches
bugs that simulation cannot.
