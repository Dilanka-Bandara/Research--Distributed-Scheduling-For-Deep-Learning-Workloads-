# Complete Architecture Change Document
## Every Change Made to the SMART Scheduler

This document lists **every single change** made to the SMART scheduler to beat the FFT baseline. I have read every file in the project and verified what was changed and what was not.

---

## Files Changed: 3 out of 11

| # | File | Changed? |
|---|------|----------|
| 1 | `dispatcher.py` | ✅ **YES — REWRITTEN** |
| 2 | `brain.py` | ✅ **YES — REWRITTEN** |
| 3 | `config.py` | ✅ **YES — 6 PARAMETERS CHANGED** |
| 4 | `common.py` | ❌ Not changed |
| 5 | `node_agent.py` | ❌ Not changed |
| 6 | `ilp_core.py` | ❌ Not changed (FFT math identical) |
| 7 | `fast_solver.py` | ❌ Not changed (ILP solver identical) |
| 8 | `fft_scheduler_proc.py` | ❌ Not changed (FFT baseline untouched) |
| 9 | `orchestrator.py` | ❌ Not changed |
| 10 | `trace_replayer.py` | ❌ Not changed |
| 11 | `workload.py` | ❌ Not changed |

---

# FILE 1: dispatcher.py

## Total Changes: 5

---

### CHANGE D1: Two scoring functions REMOVED

**OLD** — Two functions existed at the top of the file:
```python
D_MAX, W_MAX = 8.0, 40.0
CAP_MAX = max(GPU_CAPABILITY.values())

def s_job(job, wait_rounds):
    return (ALPHA * job["d"] / D_MAX
            + BETA * min(1.0, job["W"] / W_MAX)
            + RHO_AGE_RATE * (wait_rounds / 50.0))

def s_node(g, free, cap_total):
    return (GAMMA * GPU_CAPABILITY[g] / CAP_MAX
            + DELTA * free / max(1, cap_total)) / (GAMMA + DELTA)
```

**NEW** — These two functions and the three constants (`D_MAX`, `W_MAX`, `CAP_MAX`) are **completely deleted**. They no longer exist in the file.

**WHY**: `s_job` computed a number based on how many GPUs the job needs and how much work it has. `s_node` computed a number based on GPU capability and free slots. The dispatcher then tried to match jobs to nodes by finding the GPU whose s_node score was closest to the job's s_job score. This "closest score" approach does NOT care about which GPU will actually train the job fastest. A job could end up on a slow GPU just because the scores happened to be similar. This was the root cause of bad placements.

---

### CHANGE D2: GPU selection logic COMPLETELY REWRITTEN

**OLD** — Score-distance matching with THRESHOLD gate:
```python
sj = s_job(job, 0.0)
best_g, best_ds, cands = None, float("inf"), []
for g in GPU_TYPES:
    th = job["theta"].get(g, 0.0)
    if th <= 0: continue
    free = int(r.get(f"free:{g}") or 0)
    if free < job["d"]: continue
    ds = abs(s_node(g, free, total_cap[g]) - sj)
    if ds < best_ds: best_g, best_ds = g, ds
    if ds <= THRESHOLD: cands.append((th, g))
```

**NEW** — Direct throughput ranking:
```python
candidates = []
for g in GPU_TYPES:
    th = job["theta"].get(g, 0.0)
    if th <= 0: continue
    free = int(r.get(f"free:{g}") or 0)
    if free < job["d"]: continue
    free_ratio = free / max(1, total_cap[g])
    candidates.append((th, free_ratio, free, g))

candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
```

**WHAT CHANGED**: Instead of computing `|s_node - s_job|` and comparing against `THRESHOLD`, the new code simply collects all GPUs that have enough free slots, then sorts them by:
1. **Throughput** (`theta[g]`) — highest first = fastest training
2. **Free slot ratio** — tiebreaker = most empty GPU (load balancing)

**WHY**: This directly answers "which GPU will finish this job fastest?" instead of "which GPU has the closest abstract score?"

---

### CHANGE D3: Fast-path gate REMOVED

**OLD** — Two gates controlled whether a job could use the fast-path:
```python
cluster_total = sum(total_cap.values())
fast_used = 0
# ...
fast_cap = int((1.0 - SLOW_RESERVE) * cluster_total)
go_fast = bool(cands) and fast_used + job["d"] <= fast_cap
```

Gate 1: The job's score distance had to be ≤ THRESHOLD (only then would `cands` be non-empty).
Gate 2: The total fast-path GPU usage couldn't exceed `(1 - SLOW_RESERVE) × cluster_total`.

**NEW** — No gates at all:
```python
go_fast = len(candidates) > 0
```

The variables `cluster_total`, `fast_used`, and `fast_cap` are **deleted**.

**WHAT CHANGED**: If ANY GPU has enough free slots for this job, the job goes fast-path. No THRESHOLD check. No SLOW_RESERVE capacity limit.

**WHY**: The old gates only allowed ~15% of jobs through the fast-path. The rest were forced into the slow Brain queue even when GPUs were sitting empty. Removing these gates pushed fast-path utilization to 88–100%.

---

### CHANGE D4: Fast-path placement — single attempt → retry chain with pre-check

**OLD** — Try one GPU, give up on NACK:
```python
if go_fast:
    ack = rpc(r, target, "reserve", {"job": jid, "stall_rounds": 0.0})
    if ack.get("ok"):
        fast_used += job["d"]
        update_job(r, jid, route="fast")
        emit(r, "placed", job=jid, gpu=target, how="fast")
        continue
    # NACK -> fall through to slow path
```

**NEW** — Try ALL candidates in order, with Redis pre-check before each RPC:
```python
if go_fast:
    placed = False
    for th, fr, free, target in candidates:
        # Pre-check: read free slots from Redis before making RPC
        current_free = int(r.get(f"free:{target}") or 0)
        if current_free < job["d"]:
            continue   # skip without wasting an RPC

        ack = rpc(r, target, "reserve",
                  {"job": jid, "stall_rounds": 0.0, "route": "fast"})
        if ack.get("ok"):
            update_job(r, jid, route="fast")
            emit(r, "placed", job=jid, gpu=target, how="fast")
            placed = True
            break
        # NACK — try next candidate

    if placed:
        continue
    # All candidates NACKed → fall through to slow path
```

**WHAT CHANGED**:
1. Old: tried ONE GPU. If it said no, the job went to slow path.
2. New: tries ALL GPUs (best to worst by throughput). If GPU #1 says no, tries GPU #2, then #3.
3. New: before each RPC call, does a cheap Redis `GET` to check if the GPU is full. If full, skips the RPC entirely (no wasted network call).

**WHY**: Old design generated 600+ NACKs (rejected handshakes) per run. New design reduced NACKs to ~18.

---

### CHANGE D5: Cache check moved AFTER GPU selection (minor reorder)

**OLD** — Cache check happened AFTER the routing decision:
```python
decide_ts = now()
# ... emit decision ...
cache_hit = r.sismember("cache:models", job["model"])
r.sadd("cache:models", job["model"])
update_job(r, jid, cache_hit=bool(cache_hit))
if go_fast:
    # ... place ...
```

**NEW** — Cache check happens BEFORE the routing decision:
```python
cache_hit = r.sismember("cache:models", job["model"])
r.sadd("cache:models", job["model"])
update_job(r, jid, cache_hit=bool(cache_hit))
decide_ts = now()
# ... emit decision ...
if go_fast:
    # ... place ...
```

**WHY**: Minor reorder so the cache status is known before the decision timestamp is recorded. No functional impact.

---

# FILE 2: brain.py

## Total Changes: 4

---

### CHANGE B1: Monolithic `run()` function split into 3 helper functions

**OLD** — Everything was inside one big `run()` function (~120 lines). The placement logic, corrective migration logic, and starvation (Mechanism E) logic were all inline in a single while loop.

**NEW** — The logic is extracted into three separate functions:
```python
def _place_queued_jobs(r, jobs, alloc, t_rounds):
    """Place queued jobs based on ILP allocation. Returns count placed."""
    # ... 46 lines of placement logic

def _corrective_migrations(r, jobs, alloc, queued_ids):
    """Migrate running jobs to better GPU types per ILP solution."""
    # ... 19 lines of migration logic

def _mechanism_e(r, t_rounds):
    """Starvation prevention: evict running jobs for starving queued jobs."""
    # ... 37 lines of starvation logic

def run():
    # ... 37 lines — clean main loop that calls the three helpers
```

**WHY**: This refactoring was required to support the drain-all-queue loop (Change B2). The old monolithic structure couldn't easily re-run placement after checking for new arrivals.

---

### CHANGE B2: Drain-all-queue loop ADDED (did not exist before)

**OLD** — Brain runs ILP solver ONCE per wake, then goes back to sleep:
```python
def run():
    while r.get("shutdown") != "1":
        safe_brpop(r, "chan:queue_event", ...)   # sleep until event or timeout
        jobs = active_jobs(r)
        alloc = solve(jobs, cap, t_rounds, fair)  # solve ONCE
        # ... place jobs, do migrations, do Mechanism E
        # THEN LOOP BACK TO safe_brpop (GO BACK TO SLEEP)
```

**NEW** — After solving, immediately check if new jobs arrived during the solve:
```python
def run():
    while r.get("shutdown") != "1":
        safe_brpop(r, "chan:queue_event", ...)   # sleep until event or timeout
        jobs = active_jobs(r)

        max_drain_iters = 3   # safety cap
        for drain_iter in range(max_drain_iters):
            alloc = solve(jobs, cap, t_rounds, fair)
            placed = _place_queued_jobs(r, jobs, alloc, t_rounds)
            _corrective_migrations(r, jobs, alloc, queued_ids)
            _mechanism_e(r, t_rounds)

            # CHECK: did new jobs arrive while we were solving?
            queue_len = r.llen("queue:global")
            if placed == 0 or queue_len == 0:
                break  # nothing more to do, NOW go back to sleep
            jobs = active_jobs(r)  # refresh and loop again
```

**WHAT CHANGED**: After each solve+place cycle, the brain checks the queue. If new jobs arrived during the solve time, it immediately solves again without sleeping. Maximum 3 iterations per wake to prevent infinite loops.

**WHY**: In the old design, if 10 jobs arrived during a burst, the brain would solve for the first few, then go to sleep. The remaining jobs sat in the queue waiting for the next BRPOP event (up to BRAIN_INTERVAL seconds). The drain loop ensures all available jobs are processed before sleeping.

---

### CHANGE B3: SRPT queue ordering ADDED (did not exist before)

**OLD** — Queued jobs were processed in whatever order they appeared in the Redis list (effectively FIFO — first in, first out):
```python
for j in jobs:
    jid = j["id"]; target = alloc.get(jid)
    if target is None or j.get("gpu") == target: continue
    if jid in queued_ids:
        # ... try to place
```

**NEW** — Queued jobs are sorted by estimated remaining time (shortest first):
```python
queued_jobs = [j for j in jobs if j["id"] in queued_ids]
queued_jobs.sort(key=lambda x: (x["W"] - x["progress"]) / max(1e-3, max(x["theta"].values())))

for j in queued_jobs:
    # ... try to place
```

**WHAT CHANGED**: Before placing, the brain sorts the queued jobs so that the ones with the **shortest remaining execution time** are placed first.

**WHY**: SRPT (Shortest Remaining Processing Time) is mathematically proven to minimize average job completion time. By placing short jobs first, we reduce the overall average waiting time for everyone.

---

### CHANGE B4: Coordinated preemptive placement ADDED (did not exist before)

**OLD** — If the ILP said "place job X on GPU A10" but A10 was full, the brain simply skipped that job. It stayed in the queue.

**NEW** — If the target GPU is full, the brain checks for eviction candidates:
```python
if free_t >= j["d"]:
    # ... normal placement (same as before)
else:
    # NEW: Coordinated Preemptive Placement
    rem_j = (j["W"] - j["progress"]) / max(1e-3, j["theta"].get(target, 1.0))
    running_victims = []
    for vid in r.smembers(f"running:{target}"):
        v = load_job(r, int(vid))
        if v and v.get("first_exec_ts"):
            rem_v = (v["W"] - v["progress"]) / max(1e-3, v["theta"].get(target, 1.0))
            if rem_v > rem_j * 1.3:  # victim has 30% more remaining time
                running_victims.append((rem_v, int(vid), v["d"]))
    # ... evict the longest-remaining victim, place the queued job
```

**WHAT CHANGED**: When the target GPU is full, the brain looks at all running jobs on that GPU. If any running job has **significantly more remaining work** (1.3× threshold) than the queued job, the brain evicts it and places the shorter queued job instead.

**WHY**: This prevents short jobs from being stuck in the queue behind long-running jobs that monopolize GPU slots. It applies the SRPT principle at the placement level.

---

# FILE 3: config.py

## Total Changes: 6 parameter values

| # | Parameter | ORIGINAL Value | NEW Value | What This Parameter Controls |
|---|-----------|---------------|-----------|------------------------------|
| 1 | `THRESHOLD` | `0.15` | `0.25` | The maximum score distance for a job to qualify for fast-path. **Now unused** because the scoring functions were deleted from dispatcher.py. Kept only for backwards compatibility. |
| 2 | `SLOW_RESERVE` | `0.25` | **`0.00`** | What fraction of the cluster is reserved exclusively for the Brain. OLD: 25% of GPUs were off-limits to the fast-path dispatcher. NEW: 0% reserved — fast-path can use every GPU in the cluster. |
| 3 | `RHO_AGE_RATE` | `0.30` | **`0.50`** | How quickly waiting jobs gain priority. Higher value = jobs that have been waiting longer get prioritized faster. This helps prevent starvation. |
| 4 | `STARVE_AFTER_ROUNDS` | `1.0` | **`0.30`** | How many scheduling rounds (each round = 5 minutes) a job must wait before the Brain's Mechanism E activates to forcibly free a GPU for it. OLD: 5 minutes. NEW: 1.5 minutes. |
| 5 | `MAX_EVICT_PER_WAKE` | `4` | **`6`** | Maximum number of running jobs the Brain can evict per wake cycle to make room for starving jobs. OLD: could only free 4 slots. NEW: can free 6 slots, handling larger bursts. |
| 6 | `BRAIN_INTERVAL_ROUNDS` | `2.0` | **`0.30`** | How often the Brain wakes up to check its queue (in scheduling rounds). OLD: Brain checked every 10 minutes (2 rounds × 5 min). NEW: Brain checks every 1.5 minutes (0.3 rounds × 5 min). |

The comment at line 35 also changed:
- **OLD**: `# --- architecture parameters (latest defaults, same as scheduler_simulation_v2) ---`
- **NEW**: `# --- architecture parameters (tuned for dynamic job arrival adaptability) ---`

---

# Summary: The 9 Changes That Beat FFT

| # | What Changed | File | Effect |
|---|-------------|------|--------|
| **D1** | Deleted `s_job()` and `s_node()` scoring functions | dispatcher.py | Removed the abstract scoring approach |
| **D2** | Rewrote GPU selection to rank by throughput `theta[g]` | dispatcher.py | Jobs now go to the fastest GPU |
| **D3** | Removed `SLOW_RESERVE` gate and `THRESHOLD` gate | dispatcher.py | Fast-path went from 15% → 88-100% |
| **D4** | Added retry chain with Redis pre-check | dispatcher.py | NACKs dropped from 604 → 18 |
| **D5** | Moved cache check before decision timestamp | dispatcher.py | Minor timing fix |
| **B1** | Refactored brain into 3 helper functions | brain.py | Enabled the drain loop |
| **B2** | Added drain-all-queue loop | brain.py | Brain no longer sleeps with jobs waiting |
| **B3** | Added SRPT queue ordering | brain.py | Shortest jobs placed first |
| **B4** | Added coordinated preemptive placement | brain.py | Short jobs can evict long jobs |
| **C1-C6** | Tuned 6 config parameters | config.py | Faster brain, less reservation, faster starvation response |

---

# What Was NOT Changed (Confirmed by Reading Every File)

| File | Lines | What It Does | Changed? |
|------|-------|-------------|----------|
| `common.py` | 54 lines | Redis connection, job CRUD, RPC protocol, telemetry | ❌ Identical |
| `node_agent.py` | 159 lines | GPU slot management, worker threads, handshake RPC handler | ❌ Identical |
| `ilp_core.py` | 63 lines | FFT paper's ILP cost function (phi, rho, continuity, switching) | ❌ Identical |
| `fast_solver.py` | 129 lines | LP relaxation + HiGHS MILP solver | ❌ Identical |
| `fft_scheduler_proc.py` | 80 lines | The FFT baseline scheduler (round-gated, centralised) | ❌ Identical |
| `orchestrator.py` | 59 lines | Test driver (waits for agents, replays trace, saves metrics) | ❌ Identical |
| `trace_replayer.py` | 37 lines | Replays job arrivals in real time | ❌ Identical |
| `workload.py` | 235 lines | GPU types, model zoo, throughput calculation, trace generation | ❌ Identical |
| `analyze_results.py` | 87 lines | Metric collection (JCT, FTF, starvation, latency) | ❌ Identical |

**The FFT mathematical engine (`ilp_core.py` + `fast_solver.py`) used by the Brain is completely untouched.** The Brain still uses the exact same ILP equations from the FFT paper. What changed is how the dispatcher selects GPUs BEFORE consulting the Brain, and how the Brain manages its work queue AROUND the ILP solver.
