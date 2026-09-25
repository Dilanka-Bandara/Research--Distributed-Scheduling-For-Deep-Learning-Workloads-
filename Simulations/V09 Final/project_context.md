# Project Context

## File Structure

```text
.
├── .dockerignore
├── .env
├── .env.pc1.template
├── .env.pc2.template
├── .gitignore
├── Dockerfile
├── PC2_SETUP_AND_AGENT_GUIDE.md
├── README.md
├── analyze_results.py
├── brain.py
├── common.py
├── compare_all.py
├── config.py
├── dispatcher.py
├── docker-compose.pc1.yml
├── docker-compose.pc2.yml
├── docker-compose.yml
├── fast_solver.py
├── fft_scheduler_proc.py
├── ilp_core.py
├── node_agent.py
├── orchestrator.py
├── preflight.ps1
├── preflight.py
├── run_all_36.py
├── sync_to_pc2.ps1
├── test_connection.py
├── trace_replayer.py
└── workload.py
```

## Files

### .dockerignore

```
# Keep .env OUT of Docker images — it's machine-specific
.env
.env.pc1.template
.env.pc2.template

# Results and archives (not needed inside containers)
runs/
runs_v1/
*.zip
*.pdf

# Python cache
__pycache__/
*.pyc

# Docs and config files (not needed inside containers)
*.md
.git/
.gitignore

```

### .env

```
# Environment configuration for Option B (PC 1 - Controller)
EMU_N_PER_TYPE=12
EMU_TIME_SCALE=100
EMU_REDIS_HOST=redis
EMU_REDIS_PORT=6379

```

### .env.pc1.template

```
# Environment configuration for PC 1 (Controller / Master Node)
# Copy this file to .env on PC 1.
# .env is git-ignored and docker-ignored — never ship it between machines.
EMU_N_PER_TYPE=12
EMU_TIME_SCALE=100
EMU_REDIS_HOST=redis
EMU_REDIS_PORT=6379

```

### .env.pc2.template

```
# Environment configuration for PC 2 (Dedicated GPU Compute Cluster)
# Copy this file to .env on PC 2.
# .env is git-ignored and docker-ignored — never ship it between machines.
#
# NOTE: docker-compose.pc2.yml hardcodes EMU_REDIS_HOST to 192.168.100.1
# so the .env value below is ONLY used if you run node_agent.py outside
# Docker for debugging. The compose file always wins for containerised runs.
EMU_N_PER_TYPE=12
EMU_TIME_SCALE=100
EMU_REDIS_HOST=192.168.100.1
EMU_REDIS_PORT=6379

```

### .gitignore

```
# Machine-specific environment (never share between PC 1 and PC 2)
.env

# Python
__pycache__/
*.pyc

# Results (large, generated)
runs/
runs_v1/

# Archives
*.zip

```

### Dockerfile

```
# One image for every component — the compose service picks the command.
FROM python:3.12-slim

# iproute2 provides `tc` for the optional netem network-shaping experiments
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/*

# Patient pip (slow links): long timeout, many retries, one layer per package
# so completed downloads are cached across build retries.
RUN pip install --no-cache-dir --timeout 180 --retries 10 redis
RUN pip install --no-cache-dir --timeout 180 --retries 10 numpy
RUN pip install --no-cache-dir --timeout 180 --retries 10 scipy

WORKDIR /app
COPY *.py /app/

# default command is overridden per service in docker-compose.yml
CMD ["python", "-c", "print('specify a command in docker-compose.yml')"]

```

### PC2_SETUP_AND_AGENT_GUIDE.md

```markdown
# Handover & Debugging Guide for PC 2 (Remote Compute Worker)

> **Context for PC 2 AI Agent:**  
> This project is a distributed GPU cluster scheduling emulation for a research thesis. The cluster consists of **36 simulated GPUs** distributed across two physical Windows machines connected directly via a high-speed **Cat 8 Ethernet cable**.

---

## 1. System Architecture & Topology

```
╔════════════════════════════════════════════╗       Cat 8 Cable       ╔════════════════════════════════════════════╗
║         PC 1 (Controller / Master)         ║ ══════════════════════> ║         PC 2 (Dedicated GPU Cluster)       ║
║          IP: 192.168.100.1:6379            ║                         ║              IP: 192.168.100.2             ║
╠════════════════════════════════════════════╣                         ╠════════════════════════════════════════════╣
║ • Redis Server (Global Recorder)           ║                         ║ • agent-t4 container   (12 GPU slots)      ║
║ • Brain (ILP Optimizer)                    ║                         ║ • agent-v100 container (12 GPU slots)      ║
║ • Dispatcher (Handshake Coordinator)       ║                         ║ • agent-a10 container  (12 GPU slots)      ║
║ • Orchestrator (Replayer & Metrics)        ║                         ║                                            ║
║────────────────────────────────────────────║                         ║────────────────────────────────────────────║
║ Total GPUs on PC 1: 0 (Pure Controller)    ║                         ║ Total GPUs on PC 2: 36 GPUs                ║
╚════════════════════════════════════════════╝                         ╚════════════════════════════════════════════╝
                                  Total Cluster: 36 GPUs
```

---

## 2. PC 2 Role & Responsibilities

PC 2 acts as the **Dedicated 36-GPU Compute Cluster**. It runs three containerized Node Agents:
1. `agent-t4`: Manages 12 slots of T4 GPUs.
2. `agent-v100`: Manages 12 slots of V100 GPUs.
3. `agent-a10`: Manages 12 slots of A10 GPUs.
Total = 36 GPUs (100% of the cluster hardware).

These containers connect over the physical Cat 8 network to **Redis on PC 1** (`192.168.100.1:6379`).
All job allocations, decentralised handshakes (`agent:<type>:req`), and worker progress events (`metrics:events`) flow back and forth across the physical wire through Redis.

---

## 3. Network Configuration on PC 2

* **Physical Interface**: Ethernet NIC connected to PC 1 via Cat 8 cable.
* **IPv4 Configuration (Static)**:
  * IP Address: `192.168.100.2`
  * Subnet Mask: `255.255.255.0`
  * Default Gateway: `192.168.100.1` (or blank)
* **Connectivity Test**:
  * `ping 192.168.100.1` must return `< 1ms`.

---

## 4. Expected Files on PC 2

Inside the working directory (e.g. `V08/` or `Option B/`) on PC 2:

```
V08/
├── Dockerfile
├── docker-compose.yml      <-- (Configured for PC 2 agents only)
├── node_agent.py          <-- (Core agent process)
├── common.py              <-- (Redis helpers & telemetry)
├── config.py              <-- (Cluster parameters & time scaling)
└── test_connection.py     <-- (Diagnostic tool)
```

---

## 5. File Configurations for PC 2

### A. `docker-compose.yml` (on PC 2)
```yaml
x-common-env: &env
  EMU_REDIS_HOST: ${EMU_REDIS_HOST:-192.168.100.1}
  EMU_REDIS_PORT: 6379
  EMU_TIME_SCALE: ${EMU_TIME_SCALE:-100}
  EMU_N_PER_TYPE: ${EMU_N_PER_TYPE:-12}

x-agent: &agent
  build: .
  environment: *env
  cap_add: [NET_ADMIN]

services:
  agent-v100:
    <<: *agent
    command: ["python", "node_agent.py", "V100"]

  agent-a10:
    <<: *agent
    command: ["python", "node_agent.py", "A10"]
```

### B. `Dockerfile` (on PC 2)
```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --timeout 180 --retries 10 redis numpy scipy

WORKDIR /app
COPY *.py /app/

CMD ["python", "-c", "print('specify a command in docker-compose.yml')"]
```

---

## 6. Automated Multi-Run Workflow (All 36 Runs)

The updated system allows all 36 runs to execute completely automatically:

### On PC 2 (Run ONCE):
```powershell
# In PowerShell inside Option B (or V08):
docker compose up -d --build
docker compose logs -f
```
> **Notice**: Thanks to the continuous auto-reset loop in `node_agent.py`, **you never have to restart PC 2 between runs!**
> PC 2 will stay running, automatically reset its slots between runs, and serve all 36 runs continuously.

### On PC 1 (Automated Master Runner):
```powershell
# Run all 36 runs automatically (with automatic resume if interrupted):
python run_all_36.py
```
* Or run only the SMART scheduler (18 runs): `python run_all_36.py --mode smart`
* Or run only the FFT scheduler (18 runs): `python run_all_36.py --mode fft`
* When finished, it automatically runs `python compare_all.py` and outputs the full thesis evaluation table.

---

## 7. Troubleshooting & Common Issues on PC 2

### Issue 1: `Error: Connection refused` or `Cannot connect to Redis at 192.168.100.1:6379`
* **Root Cause**: PC 1's Redis container is not running, or Windows Firewall on PC 1 is blocking port 6379.
* **Fix**:
  1. On PC 1, ensure Redis is up: `docker compose -f docker-compose.pc1.yml ps`
  2. On PC 1, open firewall port 6379:
     `New-NetFirewallRule -DisplayName "Allow-Redis-6379" -Direction Inbound -LocalPort 6379 -Protocol TCP -Action Allow`
  3. On PC 2, test with: `python test_connection.py 192.168.100.1`

### Issue 2: `Docker container cannot resolve 192.168.100.1`
* **Root Cause**: WSL2 / Docker Desktop virtual bridge networking issue.
* **Fix**: On PC 2, verify you can ping `192.168.100.1` from the Windows host. Docker Desktop uses host routing for external IPs by default.

### Issue 3: `Agent registered with wrong slot count (e.g. 8 instead of 12)`
* **Root Cause**: `EMU_N_PER_TYPE` was not set before launching.
* **Fix**:
  ```powershell
  $env:EMU_N_PER_TYPE="12"
  docker compose down
  docker compose up -d
  ```

### Issue 4: Checking Live Registration Status
Run inside Python on PC 2:
```python
import redis
r = redis.Redis(host="192.168.100.1", port=6379, decode_responses=True)
print("T4 Free Slots:", r.get("free:T4"))     # Should be 12 (hosted by PC 1)
print("V100 Free Slots:", r.get("free:V100")) # Should be 12 (hosted by PC 2)
print("A10 Free Slots:", r.get("free:A10"))   # Should be 12 (hosted by PC 2)
```

```

### README.md

```markdown
# Option B — Docker Compose Virtual Cluster

Option A's validated processes, containerized: each component runs in its own
container with its own network namespace on a real Docker bridge network. The
decentralised handshake, event triggers, and all telemetry now cross actual
network boundaries. This is the "virtual cluster" chapter of the thesis and a
one-command reproducible artifact.

Same code as Option A (`node_agent.py`, `dispatcher.py`, `brain.py`,
`fft_scheduler_proc.py`, …) — only the packaging is new, so all Option A
results remain directly comparable.

## Topology

```
                 ┌────────────┐
                 │   redis    │  Global Recorder (component 5)
                 └─────┬──────┘
     ┌─────────┬───────┼────────┬──────────────┐
┌────┴───┐ ┌───┴────┐ ┌┴───────┐ ┌───────────┐ ┌───────────────┐
│agent-t4│ │agent-  │ │agent-  │ │dispatcher │ │brain          │  (profile: smart)
│  (T4)  │ │v100    │ │a10     │ │(comp. 2)  │ │(comp. 6)      │
└────────┘ └────────┘ └────────┘ └───────────┘ └───────────────┘
                                  └── or ──> │fft-scheduler│      (profile: fft)
```

## Setup (Windows)

1. Docker Desktop installed and running (WSL2 backend).
2. In this folder: `docker compose --profile smart build` (first time only;
   also builds the image used by every other profile).

## Run — your architecture (smart)

```bash
docker compose --profile smart up -d
docker compose --profile tools run --rm orchestrator 60 bursty 1 smart 1800
docker compose --profile smart down
```

Arguments: `<jobs> <regime> <seed> <label> <timeout_s>`.
Results appear on the host in `./runs/smart_bursty_s1.json`.

## Run — FFT baseline (same trace), then compare

```bash
docker compose --profile fft up -d
docker compose --profile tools run --rm orchestrator 60 bursty 1 fft 1800
docker compose --profile fft down

python analyze_results.py --compare runs/fft_bursty_s1.json runs/smart_bursty_s1.json
```

(The compare step runs on the host — it only reads the JSON files;
`pip install redis scipy numpy` if the import complains.)

**Clean state is automatic**: Redis runs without persistence, so
`down` → `up` between runs = a fresh cluster. No manual FLUSHALL, ever.

## Configuration

Host environment variables flow into every container:

```powershell
$env:EMU_TIME_SCALE="100"    # keep <=150; see Option A README for the floor argument
$env:EMU_N_PER_TYPE="8"      # 8 => 24 GPUs; 12 => 36 GPUs (match your simulation!)
docker compose --profile smart up -d
```

## Optional: netem network shaping (the Option B extra)

With the stack up, impose realistic NIC behaviour inside an agent and watch
the handshake RPC cost respond:

```bash
# add 0.2 ms latency + 10 Gbit rate cap on agent-t4's interface
docker compose --profile smart exec agent-t4 tc qdisc add dev eth0 root netem delay 0.2ms rate 10gbit
# remove again
docker compose --profile smart exec agent-t4 tc qdisc del dev eth0 root
```

Run the same trace with and without shaping and compare
`decision_latency_ms` / handshake behaviour — a small dedicated experiment
showing the control plane living on a real (shaped) network.

**Honest scope note:** migration state-transfer remains the scaled-stall model
(identical to Option A and the simulation — that is what keeps all three tiers
comparable). netem here shapes the *control-plane* messaging. If you later
want migration enforced by the network itself, the clean extension is: on
migration, transfer `state_gb / TIME_SCALE` gigabytes of real payload between
agent containers over a 10 Gbit-capped link — the arithmetic works out so real
transfer time equals the scaled stall. Document it as future work unless you
have spare weeks.

## What Option B adds over A (for the thesis write-up)

- Components in separate network namespaces: no shared memory, all
  coordination over the wire — the word "distributed" made literal.
- A reproducible one-command artifact (`docker-compose.yml`) an examiner or
  supervisor can rerun.
- A topology diagram that visually mirrors the architecture figure.
- The netem experiment: control-plane behaviour under shaped networks.

## Honest limits (state these)

- Still one physical host underneath; container isolation is namespace-level,
  not physical. This is the emulation boundary the FFT paper itself draws for
  its simulator tier — say so.
- Docker Desktop on Windows adds a WSL2 VM hop; absolute milliseconds will
  differ from Option A native runs. Report round-normalized ratios and
  seed averages (see Option A README's accuracy section — all of it applies).
- Wall-clock budget per run is the same as Option A; add ~1 min for
  container startup.

## Troubleshooting

- `orchestrator` exits with "agents never came up" → `docker compose ps` and
  `docker compose logs agent-t4`; usually the build failed or Redis is
  unhealthy.
- Runs directory empty → the orchestrator saves into the container path
  `/app/runs`, volume-mounted to `./runs`; run compose commands from this
  folder.
- Everything hangs → `docker compose --profile smart down` always resets.

```

### analyze_results.py

```python
"""
analyze_results.py — turn the telemetry into thesis metrics.

All times converted back to ROUNDS of scaled time so numbers are directly
comparable with scheduler_simulation_v2 output (the validation figure).
Decision latency is reported in REAL milliseconds too — the stopwatch
measurement of your gap (O(1) dispatch vs round-gated central solve).

  python analyze_results.py --mode smart --save runs/smart.json
  python analyze_results.py --compare runs/fft.json runs/smart.json
"""
from __future__ import annotations
import argparse, json, os, statistics
from common import R
from config import real_to_rounds

def collect(r):
    ev = [json.loads(x) for x in r.lrange("metrics:events", 0, -1)]
    jobs = {}
    for jid_key in r.keys("jobs:*"):
        h = r.hgetall(jid_key)
        j = {k: json.loads(v) for k, v in h.items()}
        jobs[j["id"]] = j
    return ev, jobs

def metrics(ev, jobs):
    fin = [j for j in jobs.values() if j.get("finish_ts")]
    jct = [real_to_rounds(j["finish_ts"] - j["arrival_ts"]) for j in fin]
    ftf = []
    for j in fin:
        best = max(j["theta"].values())
        ideal = j["W"] / max(1e-6, best)
        ftf.append(real_to_rounds(j["finish_ts"] - j["arrival_ts"]) / max(1e-6, ideal))
    starv = [real_to_rounds(j["first_exec_ts"] - j["arrival_ts"])
             for j in fin if j.get("first_exec_ts")]
    dlat_real = [e["latency"] for e in ev if e["type"] == "decision"]
    solves = [e["wall"] for e in ev if e["type"] in ("brain_solve", "fft_solve")]
    mean = lambda xs: statistics.mean(xs) if xs else 0.0
    return {
        "n_jobs": len(jobs), "n_finished": len(fin),
        "jct_mean_rounds": mean(jct),
        "ftf_mean": mean(ftf),
        "starvation_mean_rounds": mean(starv),
        "decision_latency_ms_mean": 1000 * mean(dlat_real),
        "decision_latency_rounds_mean": mean([real_to_rounds(x) for x in dlat_real]),
        "solve_wall_ms_mean": 1000 * mean(solves),
        "fast_path_frac": sum(1 for e in ev if e["type"] == "decision"
                              and e.get("route") == "fast") / max(1, len(dlat_real)),
        "handshake_rejects": sum(1 for e in ev if e["type"] == "handshake_reject"),
        "preemptions": sum(1 for e in ev if e["type"] == "preempted"),
        "migrations": sum(1 for e in ev if e["type"] == "migration"),
        "profile_stalls": sum(1 for e in ev if e["type"] == "profile_stall"),
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="run")
    ap.add_argument("--save")
    ap.add_argument("--compare", nargs=2)
    a = ap.parse_args()
    if a.compare:
        f = json.load(open(a.compare[0])); s = json.load(open(a.compare[1]))
        print(f"{'metric':32} {'FFT':>12} {'smart':>12} {'ratio/speedup':>14}")
        rows = [("jct_mean_rounds", "ratio"), ("ftf_mean", "ratio"),
                ("starvation_mean_rounds", "ratio"),
                ("decision_latency_ms_mean", "speedup"),
                ("solve_wall_ms_mean", "-"), ("fast_path_frac", "-"),
                ("handshake_rejects", "-"), ("preemptions", "-"),
                ("migrations", "-"), ("profile_stalls", "-")]
        for k, kind in rows:
            fv, sv = f.get(k, 0), s.get(k, 0)
            extra = (f"x{sv/max(1e-9,fv):.2f}" if kind == "ratio" else
                     f"x{fv/max(1e-9,sv):.1f}" if kind == "speedup" else "")
            print(f"{k:32} {fv:12.3f} {sv:12.3f} {extra:>14}")
        return
    r = R()
    ev, jobs = collect(r)
    m = metrics(ev, jobs)
    print(json.dumps(m, indent=2))
    if a.save:
        os.makedirs(os.path.dirname(a.save), exist_ok=True)
        json.dump(m, open(a.save, "w"), indent=2)
        print("saved ->", a.save)

if __name__ == "__main__":
    main()

```

### brain.py

```python
"""
brain.py — component 6, the Asynchronous Global FFT Scheduler (smart mode).

OPTIMISED VERSION (Fixes 4, 5):
  Fix 4 — Micro-batch brain with drain-all-queue (Pollux-inspired):
          After each ILP solve and placement round, immediately re-check the
          queue. If new jobs arrived during the solve, process them without
          waiting for the next BRPOP timeout. Tight inner loop ensures the
          brain doesn't go back to sleep with unplaced jobs in the queue.

  Fix 5 — Immediate starvation response (Tiresias LAS-inspired):
          STARVE_AFTER_ROUNDS reduced to 0.3 (from 0.5), MAX_EVICT_PER_WAKE
          increased to 6 (from 4). Evicted jobs get credit: pending_stall = 0
          (skip re-profiling since they were already running).

Hybrid trigger implemented literally: BRPOP on chan:queue_event with
timeout = BRAIN_INTERVAL -> wakes on queue events OR on the interval.
Each wake: exact FFT ILP (ilp_core) over queued + running jobs; commits
queue placements and corrective migrations through the agents' reserve RPC
(NACK = real handshake rejection). Then Mechanism E: any queued job that has
NEVER executed and has waited >= STARVE_AFTER rounds evicts running jobs
(longest-remaining first) on its best feasible type — restoring the FFT
paper's Theorem-4.2 bounded-first-execution property. Solve time here is
REAL blocking time, measured with a wall clock.
"""
from __future__ import annotations
import time
from common import R, now, emit, load_job, update_job, rpc, safe_brpop
from config import (BRAIN_INTERVAL_ROUNDS, STARVE_AFTER_ROUNDS,
                    MAX_EVICT_PER_WAKE, N_PER_TYPE, rounds_to_real,
                    real_to_rounds)
from workload import GPU_TYPES
from ilp_core import FairnessState, solve

def active_jobs(r):
    out = []
    for jid in r.lrange("queue:global", 0, -1):
        j = load_job(r, int(jid))
        if j and j["status"] != "done": out.append(j)
    for g in GPU_TYPES:
        for jid in r.smembers(f"running:{g}"):
            j = load_job(r, int(jid))
            if j and j["status"] != "done": out.append(j)
    return out

def _place_queued_jobs(r, jobs, alloc, t_rounds):
    """Place queued jobs based on ILP allocation. Returns count placed."""
    queued_ids = {int(x) for x in r.lrange("queue:global", 0, -1)}
    # SRPT queue prioritization: evaluate shortest remaining jobs first
    queued_jobs = [j for j in jobs if j["id"] in queued_ids]
    queued_jobs.sort(key=lambda x: (x["W"] - x["progress"]) / max(1e-3, max(x["theta"].values())))

    placed = 0
    for j in queued_jobs:
        jid = j["id"]; target = alloc.get(jid)
        if target is None or j.get("gpu") == target: continue
        free_t = int(r.get(f"free:{target}") or 0)
        if free_t >= j["d"]:
            stall = j.get("pending_stall", 0.0) or 0.0
            ack = rpc(r, target, "reserve",
                      {"job": jid, "stall_rounds": stall,
                       "migrate_from": j.get("gpu")})
            if ack.get("ok"):
                r.lrem("queue:global", 0, str(jid))
                update_job(r, jid, pending_stall=0.0)
                emit(r, "placed", job=jid, gpu=target, how="brain")
                placed += 1
        else:
            # Coordinated Preemptive Placement: target full, evict lower-priority victim first
            rem_j = (j["W"] - j["progress"]) / max(1e-3, j["theta"].get(target, 1.0))
            running_victims = []
            for vid in r.smembers(f"running:{target}"):
                v = load_job(r, int(vid))
                if v and v.get("first_exec_ts"):
                    rem_v = (v["W"] - v["progress"]) / max(1e-3, v["theta"].get(target, 1.0))
                    if rem_v > rem_j * 1.3:
                        running_victims.append((rem_v, int(vid), v["d"]))
            running_victims.sort(reverse=True)
            if running_victims and (free_t + running_victims[0][2] >= j["d"]):
                victim_id = running_victims[0][1]
                if rpc(r, target, "evict", {"job": victim_id}).get("ok"):
                    time.sleep(0.15)
                    stall = j.get("pending_stall", 0.0) or 0.0
                    ack = rpc(r, target, "reserve",
                              {"job": jid, "stall_rounds": stall,
                               "migrate_from": j.get("gpu")})
                    if ack.get("ok"):
                        r.lrem("queue:global", 0, str(jid))
                        update_job(r, jid, pending_stall=0.0)
                        emit(r, "placed", job=jid, gpu=target, how="brain_preempt")
                        placed += 1
    return placed

def _corrective_migrations(r, jobs, alloc, queued_ids):
    """Migrate running jobs to better GPU types per ILP solution."""
    running_jobs = [j for j in jobs if j["id"] not in queued_ids]
    migrated = 0
    for j in running_jobs:
        jid = j["id"]; target = alloc.get(jid)
        if target is None or j.get("gpu") == target: continue
        src = j.get("gpu")
        free_t = int(r.get(f"free:{target}") or 0)
        if free_t < j["d"] or migrated >= 3:
            continue
        if rpc(r, src, "evict", {"job": jid}).get("ok"):
            time.sleep(0.15)  # let the worker checkpoint & requeue
            ack = rpc(r, target, "reserve",
                      {"job": jid, "migrate_from": src})
            if ack.get("ok"):
                r.lrem("queue:global", 0, str(jid))
                emit(r, "placed", job=jid, gpu=target, how="corrective")
                migrated += 1

def _mechanism_e(r, t_rounds):
    """Starvation prevention: evict running jobs for starving queued jobs."""
    evicted = 0
    for jid_s in list(r.lrange("queue:global", 0, -1)):
        if evicted >= MAX_EVICT_PER_WAKE: break
        j = load_job(r, int(jid_s))
        if not j or j.get("first_exec_ts") or j["status"] == "done": continue
        wait_r = real_to_rounds(now() - j["arrival_ts"])
        if wait_r < STARVE_AFTER_ROUNDS: continue
        feas = sorted(((th, g) for g, th in j["theta"].items() if th > 0),
                      reverse=True)
        for _, g in feas:
            victims = []
            for vid in r.smembers(f"running:{g}"):
                v = load_job(r, int(vid))
                if v and v.get("first_exec_ts"):
                    victims.append((v["W"] - v["progress"], int(vid), v["d"]))
            victims.sort(reverse=True)          # longest-remaining first
            free = int(r.get(f"free:{g}") or 0)
            picked = []
            for rem, vid, vd in victims:
                if free >= j["d"]: break
                picked.append(vid); free += vd
            if free < j["d"]: continue
            for vid in picked:
                rpc(r, g, "evict", {"job": vid})
            time.sleep(0.15)                    # checkpoints land
            # Fix 5: Starving job skips re-profiling (already arrived & known)
            stall = j.get("pending_stall", 0.0) or 0.0
            ack = rpc(r, g, "reserve",
                      {"job": j["id"], "stall_rounds": stall})
            if ack.get("ok"):
                r.lrem("queue:global", 0, str(j["id"]))
                update_job(r, j["id"], pending_stall=0.0)
                emit(r, "placed", job=j["id"], gpu=g, how="mechE")
                evicted += 1
            break

def run():
    r = R()
    fair = FairnessState(mu=1.0)
    cap = {g: N_PER_TYPE for g in GPU_TYPES}
    t0 = float(r.get("run_t0") or now())
    emit(r, "brain_up")
    while r.get("shutdown") != "1":
        safe_brpop(r, "chan:queue_event", max(1.0, rounds_to_real(BRAIN_INTERVAL_ROUNDS)))
        t_rounds = real_to_rounds(now() - t0)
        jobs = active_jobs(r)
        if not jobs: continue

        # ---- Fix 4: Drain-all-queue loop (Pollux-inspired) ----
        # After each solve, re-check the queue. If new jobs arrived during
        # the solve, process them immediately without waiting for BRPOP.
        max_drain_iters = 3   # safety cap: prevent infinite loop under flood
        for drain_iter in range(max_drain_iters):
            t_solve = now()
            alloc = solve(jobs, cap, t_rounds, fair)
            solve_wall = now() - t_solve
            emit(r, "brain_solve", n=len(jobs), wall=solve_wall)

            queued_ids = {int(x) for x in r.lrange("queue:global", 0, -1)}
            placed = _place_queued_jobs(r, jobs, alloc, t_rounds)
            _corrective_migrations(r, jobs, alloc, queued_ids)

            # Mechanism E (starvation prevention)
            _mechanism_e(r, t_rounds)

            # Drain check: if we placed jobs, re-check queue for more
            queue_len = r.llen("queue:global")
            if placed == 0 or queue_len == 0:
                break  # nothing more to do this cycle
            # Refresh jobs for next iteration
            jobs = active_jobs(r)
            if not jobs:
                break

if __name__ == "__main__":
    run()

```

### common.py

```python
"""common.py — shared helpers for the Option A emulation."""
from __future__ import annotations
import json, time, uuid
import redis
from redis.exceptions import TimeoutError as RTimeout, ConnectionError as RConnErr
from config import REDIS_HOST, REDIS_PORT

def R() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

def now() -> float:
    return time.time()

# ---------------- job records ----------------
def job_key(jid): return f"jobs:{jid}"

def save_job(r, j: dict):
    r.hset(job_key(j["id"]), mapping={k: json.dumps(v) for k, v in j.items()})

def load_job(r, jid) -> dict | None:
    h = r.hgetall(job_key(jid))
    if not h: return None
    return {k: json.loads(v) for k, v in h.items()}

def update_job(r, jid, **fields):
    r.hset(job_key(jid), mapping={k: json.dumps(v) for k, v in fields.items()})

# ---------------- telemetry ----------------
def emit(r, etype: str, **kw):
    kw.update(type=etype, ts=now())
    r.rpush("metrics:events", json.dumps(kw))

# ---------------- RPC: the decentralised handshake, for real ----------------
# The brain/dispatcher never mutates a node's slots directly. It sends a request
# through Redis; the TARGET agent verifies its own hardware table and ACKs or
# NACKs. This is the component-7 handshake as an actual network round-trip.
def safe_brpop(r, key, timeout: float):
    """BRPOP that never raises on socket/read timeouts (returns None instead)."""
    try:
        return r.brpop(key, timeout=max(1, int(round(timeout))))
    except (RTimeout, RConnErr):
        return None

def rpc(r, gpu_type: str, op: str, payload: dict, timeout=10.0) -> dict:
    resp = f"resp:{uuid.uuid4().hex}"
    r.lpush(f"agent:{gpu_type}:req", json.dumps({"op": op, "resp": resp, **payload}))
    got = safe_brpop(r, resp, timeout)
    if got is None:
        return {"ok": False, "err": "timeout"}
    return json.loads(got[1])

def shutdown_set(r): r.set("shutdown", "1")
def shutting_down(r): return r.get("shutdown") == "1"

```

### compare_all.py

```python
"""
compare_all.py — Aggregate and compare all FFT vs SMART runs across all regimes and seeds.

Usage:
  python compare_all.py
"""
import glob, json, os
import numpy as np

REGIMES = ["bursty", "dynamic", "heavy", "mixed", "random", "steady"]
METRICS = [
    ("jct_mean_rounds", "Mean JCT (rounds)", "lower"),
    ("starvation_mean_rounds", "Starvation Mean (rounds)", "lower"),
    ("ftf_mean", "Finish-Time Fairness (FTF)", "lower"),
    ("decision_latency_ms_mean", "Decision Latency (ms)", "speedup"),
    ("migrations", "Total Migrations", "lower"),
    ("profile_stalls", "Profile Stalls", "lower"),
    ("fast_path_frac", "Fast-Path Fraction", "higher"),
    ("preemptions", "Preemptions", "lower"),
]

def load_regime_stats(label, regime):
    files = glob.glob(f"runs/{label}_{regime}_s*.json")
    if not files:
        return None
    data = [json.load(open(f)) for f in files]
    stats = {}
    for key, _, _ in METRICS:
        vals = [d.get(key, 0) for d in data if key in d]
        stats[key] = np.mean(vals) if vals else 0.0
    return stats

def main():
    print("=" * 90)
    print("      COMPREHENSIVE CLUSTER EVALUATION: BASELINE (FFT) vs PROPOSED (SMART)")
    print("=" * 90)

    for regime in REGIMES:
        fft_data = load_regime_stats("fft", regime)
        smart_data = load_regime_stats("smart", regime)

        if not fft_data or not smart_data:
            continue

        print(f"\n>> REGIME: {regime.upper()} (Averaged across seeds)")
        print(f"{'Metric':<30} {'FFT (Baseline)':>18} {'SMART (Proposed)':>18} {'Comparison':>18}")
        print("-" * 90)

        for key, name, mode in METRICS:
            f_val = fft_data.get(key, 0)
            s_val = smart_data.get(key, 0)

            if mode == "speedup":
                comp = f"{f_val / max(1e-6, s_val):.1f}x Faster"
            elif mode == "lower":
                diff = ((s_val - f_val) / max(1e-6, f_val)) * 100
                comp = f"{diff:+.1f}%"
            elif mode == "higher":
                comp = f"{s_val*100:.1f}% fast-path"
            else:
                comp = ""

            print(f"{name:<30} {f_val:>18.3f} {s_val:>18.3f} {comp:>18}")

    print("\n" + "=" * 90)

if __name__ == "__main__":
    main()

```

### config.py

```python
"""
config.py — Option A virtual-cluster emulation.

TIME SCALING: everything physical (5-min rounds, 60 s profiling, state-transfer
seconds) is divided by TIME_SCALE to run in real wall-clock time. At 100x, one
5-minute scheduling round lasts 3 s. Do not go below ~50x: real solver and
Redis latencies (tens of ms) would start distorting the scaled physics.

REDIS SCHEMA (the Global Recorder, component 5, is a REAL Redis instance):
  arrivals                LIST  replayer LPUSHes job JSON at its (scaled) arrival time
  jobs:<id>               HASH  job record: model,d,W,theta,state_gb,progress,status,timestamps
  queue:global            LIST  slow-path job ids (component 4)
  chan:queue_event        LIST  event trigger: LPUSH token wakes the brain (BRPOP)
  agent:<type>:req        LIST  RPC requests to a node agent {op,job,resp}
  resp:<uuid>             LIST  RPC responses (the decentralised handshake is a real
                                request->check->ack round-trip through Redis)
  free:<type>             STR   advertised free worker slots per GPU type
  running:<type>          SET   job ids currently executing on that type
  cache:models            SET   historical cache of profiled models (component 3)
  metrics:events          LIST  JSON telemetry events from every component
  total_jobs / done_count STR   completion tracking
  shutdown                STR   set to "1" to stop all processes
"""
import os

REDIS_HOST = os.environ.get("EMU_REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("EMU_REDIS_PORT", "6379"))

TIME_SCALE = float(os.environ.get("EMU_TIME_SCALE", "100"))   # 100x: 5-min round -> 3 s
ROUND_SECONDS = 300.0          # FFT paper default scheduling round
PROFILE_SECONDS = 60.0         # micro-profiling window (spec) / FFT on-the-fly profiling
LAN_GBPS = 1.25                # 10 Gbps testbed LAN (paper Sec. 2.2)
TICK_REAL = 0.05               # worker progress/preemption poll tick (real seconds)

# --- architecture parameters (tuned for dynamic job arrival adaptability) ---
# Fix 6: Optimised parameters grounded in A-SRPT / Tiresias / Gandiva research
ALPHA, BETA, GAMMA, DELTA = 1.0, 0.5, 1.0, 1.0
THRESHOLD = 0.25               # legacy (kept for reference, no longer used by dispatcher)
SLOW_RESERVE = 0.0             # Fix 2: let fast-path use full cluster capacity (Gandiva-style)
RHO_AGE_RATE = 0.5             # stronger priority boost for waiting jobs (Tiresias LAS-inspired)
STARVE_AFTER_ROUNDS = 0.3      # Fix 5: faster Mechanism E trigger (was 0.5)
MAX_EVICT_PER_WAKE = 6         # Fix 5: more aggressive eviction capacity (was 4)
JCT_AWARE_FAST = True
BRAIN_INTERVAL_ROUNDS = 0.3    # Fix 4: more frequent brain wakes (was 0.5)

N_PER_TYPE = int(os.environ.get("EMU_N_PER_TYPE", "8"))

def scaled(seconds_physical: float) -> float:
    """Physical seconds -> real (wall-clock) seconds under TIME_SCALE."""
    return seconds_physical / TIME_SCALE

def rounds_to_real(rounds: float) -> float:
    return scaled(rounds * ROUND_SECONDS)

def real_to_rounds(real_dt: float) -> float:
    return real_dt * TIME_SCALE / ROUND_SECONDS

```

### dispatcher.py

```python
"""
dispatcher.py — component 2, the O(1) Throughput-Maximising Dispatcher (smart mode).

OPTIMISED VERSION (Fixes 1, 2, 3):
  Fix 1 — Throughput-maximising GPU selection (A-SRPT / Gandiva inspired):
          Instead of abstract score-distance matching, we directly pick the GPU
          type that maximises job throughput (theta[g]) among feasible types.
          Tiebreak by load-balance (most free slots). Still O(1): 3 GPU types.

  Fix 2 — Aggressive fast-path (Gandiva-style work conservation):
          SLOW_RESERVE = 0 → fast path can use full cluster capacity.
          Any job with a feasible GPU and free slots goes fast-path.

  Fix 3 — NACK storm elimination (Predictive Backfill inspired):
          Redis free-slot pre-check BEFORE the RPC handshake. If free < d_j,
          skip directly to slow path. Eliminates 600+ wasted NACKs per run.

Performance: O(1) per arrival — 3 GPU types × arithmetic.
"""
from __future__ import annotations
import time
from common import R, now, emit, load_job, update_job, rpc, safe_brpop
from config import (ALPHA, BETA, GAMMA, DELTA, THRESHOLD, SLOW_RESERVE,
                    RHO_AGE_RATE, JCT_AWARE_FAST, PROFILE_SECONDS,
                    ROUND_SECONDS, N_PER_TYPE, real_to_rounds)
from workload import GPU_TYPES, GPU_CAPABILITY

def run():
    r = R()
    total_cap = {g: N_PER_TYPE for g in GPU_TYPES}
    emit(r, "dispatcher_up")
    while r.get("shutdown") != "1":
        got = safe_brpop(r, "arrivals", 1)
        if not got: continue
        jid = int(got[1])
        t_pop = now()
        job = load_job(r, jid)

        # ---- Fix 1: Throughput-maximising GPU selection (A-SRPT inspired) ----
        # For each feasible GPU type, score by: throughput (primary), then
        # load-balance by free-slot ratio (secondary). Pick the best.
        candidates = []   # list of (throughput, free_ratio, free_slots, gpu_type)
        for g in GPU_TYPES:
            th = job["theta"].get(g, 0.0)
            if th <= 0: continue
            free = int(r.get(f"free:{g}") or 0)
            # Fix 3: Only consider GPUs with enough free slots (pre-check)
            if free < job["d"]: continue
            free_ratio = free / max(1, total_cap[g])
            candidates.append((th, free_ratio, free, g))

        # Sort: highest throughput first; tiebreak by most free (load balance)
        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)

        # ---- Fix 2: Aggressive fast-path — no SLOW_RESERVE gate ----
        # If we have any candidate with free slots, go fast-path immediately.
        go_fast = len(candidates) > 0

        # Cache check (skip profiling for known models)
        cache_hit = r.sismember("cache:models", job["model"])
        r.sadd("cache:models", job["model"])
        update_job(r, jid, cache_hit=bool(cache_hit))

        decide_ts = now()
        update_job(r, jid, decide_ts=decide_ts)
        emit(r, "decision", job=jid, latency=decide_ts - t_pop,
             route="fast" if go_fast else "slow")

        if go_fast:
            # Try candidates in throughput order until one ACKs
            placed = False
            for th, fr, free, target in candidates:
                # Fix 3: Re-verify free slots right before RPC (concurrent dispatch)
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
                # NACK — try next candidate (if any)

            if placed:
                continue
            # All candidates NACKed → fall through to slow path

        # ---- Slow path: queue for the brain's ILP solver ----
        stall = 0.0 if cache_hit else PROFILE_SECONDS / ROUND_SECONDS
        if stall: emit(r, "profile_stall", job=jid, rounds=stall)
        update_job(r, jid, route="slow", pending_stall=stall)
        r.lpush("queue:global", jid)
        r.lpush("chan:queue_event", "1")     # event trigger -> wake the brain

if __name__ == "__main__":
    run()

```

### docker-compose.pc1.yml

```yaml
# Option B — PC 1 (Controller / Master Node)
# Hosts:
#   - Redis (Global Recorder, Port 6379)
#   - Brain (ILP Optimizer) / FFT Scheduler
#   - Dispatcher (Handshake Coordinator)
#   - Orchestrator (Workload Replayer)

x-common-env: &env
  EMU_REDIS_HOST: redis
  EMU_REDIS_PORT: 6379
  EMU_TIME_SCALE: ${EMU_TIME_SCALE:-100}
  EMU_N_PER_TYPE: ${EMU_N_PER_TYPE:-12}
  PYTHONUNBUFFERED: "1"

services:
  redis:
    image: redis:7
    command: ["redis-server", "--save", "", "--appendonly", "no", "--protected-mode", "no"]
    ports:
      - "0.0.0.0:6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
      timeout: 2s
      retries: 15
    profiles: [smart, fft, tools]

  dispatcher:
    build: .
    command: ["python", "dispatcher.py"]
    environment: *env
    depends_on:
      redis:
        condition: service_healthy
    profiles: [smart]

  brain:
    build: .
    command: ["python", "brain.py"]
    environment: *env
    depends_on:
      redis:
        condition: service_healthy
    profiles: [smart]

  fft-scheduler:
    build: .
    command: ["python", "fft_scheduler_proc.py"]
    environment: *env
    depends_on:
      redis:
        condition: service_healthy
    profiles: [fft]

  orchestrator:
    build: .
    entrypoint: ["python", "orchestrator.py"]
    environment: *env
    volumes:
      - ./runs:/app/runs
    profiles: [tools]

```

### docker-compose.pc2.yml

```yaml
# Option B — PC 2 (Dedicated 36-GPU Compute Cluster)
# Hosts:
#   - agent-t4   (12 slots)
#   - agent-v100 (12 slots)
#   - agent-a10  (12 slots)
#   Total on PC 2 = 36 GPUs
#
# Usage:
#   1. Start worker agents on PC 2:
#      docker compose -f docker-compose.pc2.yml up -d --build
#   2. Monitor live agent logs:
#      docker compose -f docker-compose.pc2.yml logs -f

x-common-env: &env
  EMU_REDIS_HOST: "192.168.100.1"
  EMU_REDIS_PORT: 6379
  EMU_TIME_SCALE: ${EMU_TIME_SCALE:-100}
  EMU_N_PER_TYPE: ${EMU_N_PER_TYPE:-12}
  PYTHONUNBUFFERED: "1"

x-agent: &agent
  build: .
  environment: *env
  cap_add: [NET_ADMIN]
  restart: unless-stopped

services:
  # T4 agent on PC 2 (12 slots)
  agent-t4:
    <<: *agent
    command: ["python", "node_agent.py", "T4"]

  # V100 agent on PC 2 (12 slots)
  agent-v100:
    <<: *agent
    command: ["python", "node_agent.py", "V100"]

  # A10 agent on PC 2 (12 slots)
  agent-a10:
    <<: *agent
    command: ["python", "node_agent.py", "A10"]

```

### docker-compose.yml

```yaml
# Option B — the virtual cluster as containers on a real Docker network.
#
# Topology mirrors the FFT paper's testbed shape at type granularity:
# three node-agent containers (T4 / V100 / A10 slot tables + workers),
# Redis as the Global Recorder, and either {dispatcher + brain} (smart)
# or the centralised FFT scheduler (fft). Every handshake RPC now crosses
# a real network namespace boundary.
#
#   docker compose --profile smart build
#   docker compose --profile smart up -d
#   docker compose --profile tools run --rm orchestrator 60 bursty 1 smart 1800
#   docker compose --profile smart down
#
#   docker compose --profile fft up -d
#   docker compose --profile tools run --rm orchestrator 60 bursty 1 fft 1800
#   docker compose --profile fft down
#
# Results land in ./runs on the host. `down` between runs = clean state
# (Redis has no persistence), so no manual flushing is ever needed.

x-common-env: &env
  EMU_REDIS_HOST: redis
  EMU_TIME_SCALE: ${EMU_TIME_SCALE:-100}
  EMU_N_PER_TYPE: ${EMU_N_PER_TYPE:-8}

x-agent: &agent
  build: .
  environment: *env
  depends_on:
    redis:
      condition: service_healthy
  cap_add: [NET_ADMIN]        # allows optional `tc` netem shaping inside
  profiles: [smart, fft]

services:
  redis:
    image: redis:7
    command: ["redis-server", "--save", "", "--appendonly", "no"]
    ports: ["6379:6379"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
      timeout: 2s
      retries: 15
    profiles: [smart, fft]

  agent-t4:
    <<: *agent
    command: ["python", "node_agent.py", "T4"]
  agent-v100:
    <<: *agent
    command: ["python", "node_agent.py", "V100"]
  agent-a10:
    <<: *agent
    command: ["python", "node_agent.py", "A10"]

  dispatcher:
    build: .
    command: ["python", "dispatcher.py"]
    environment: *env
    depends_on: [agent-t4, agent-v100, agent-a10]
    profiles: [smart]

  brain:
    build: .
    command: ["python", "brain.py"]
    environment: *env
    depends_on: [agent-t4, agent-v100, agent-a10]
    profiles: [smart]

  fft-scheduler:
    build: .
    command: ["python", "fft_scheduler_proc.py"]
    environment: *env
    depends_on: [agent-t4, agent-v100, agent-a10]
    profiles: [fft]

  orchestrator:
    build: .
    entrypoint: ["python", "orchestrator.py"]
    environment: *env
    volumes:
      - ./runs:/app/runs
    profiles: [tools]

```

### fast_solver.py

```python
"""
fast_solver.py
--------------
Drop-in, in-process replacement for the CBC subprocess solver used by the FFT
scheduler. It solves the SAME integer program and returns the SAME optimal
allocation — fidelity to the FFT formulation is preserved exactly. The only
thing that changes is HOW it is solved:

  1. No subprocess. The original used PuLP -> CBC, which spawns an external CBC
     process on every round. Profiling showed ~87% of total runtime was process
     spawn/wait (posix.waitpid), not the optimisation itself. This solver runs
     entirely in-process via SciPy's HiGHS backend.

  2. LP-relaxation fast path. The FFT per-round problem is a Generalised
     Assignment Problem (assign each job to <=1 GPU type, per-type worker
     capacity). Its LP relaxation is frequently already integral; when it is,
     that solution is the PROVABLY OPTIMAL integer solution and we return it
     immediately without invoking the (more expensive) MILP solver.

  3. Exact MILP fallback. When the LP relaxation is fractional, we solve the
     true MILP with SciPy's branch-and-bound (HiGHS). This is exact — verified
     to match CBC's objective to within 1e-4 across many randomised cases.

The problem solved each round (identical to fft_baseline's formulation):

    minimise  sum_{j,i}  cost[j,i] * x[j,i]
    s.t.      sum_i x[j,i] <= 1            for every job j      (one type per job)
              sum_j d_j * x[j,i] <= cap_i  for every type i     (type capacity)
              x[j,i] in {0,1}
              x[j,i] = 0 if placement (j,i) is infeasible (zero throughput)

`cost[j,i]` is supplied by the caller (it encodes FFT's JCT priority, switching
penalty, fairness reward, continuity, and the work-conservation base reward).
This module does not change the economics — it only finds the optimum faster.
"""

from __future__ import annotations
from typing import Dict, List, Tuple
import numpy as np
from scipy.optimize import milp, linprog, LinearConstraint, Bounds


def solve_assignment(
    job_ids: List[int],
    job_demands: Dict[int, int],
    gpu_types: List[str],
    capacity: Dict[str, int],
    costs: Dict[Tuple[int, str], float],
) -> Dict[int, str]:
    """
    Solve the FFT per-round assignment exactly, in-process.

    Args:
      job_ids:     active job ids (order defines variable order).
      job_demands: job_id -> d_j (workers required).
      gpu_types:   list of GPU type names.
      capacity:    gpu_type -> available worker count.
      costs:       (job_id, gpu_type) -> cost. Missing keys are infeasible
                   placements and are simply not created as variables.

    Returns:
      {job_id: gpu_type} for every job that is scheduled this round.
    """
    # Build variable index only for feasible (present) placements.
    idx: Dict[Tuple[int, str], int] = {}
    c: List[float] = []
    for jid in job_ids:
        for g in gpu_types:
            key = (jid, g)
            if key in costs:
                idx[key] = len(c)
                c.append(costs[key])
    if not c:
        return {}

    c_arr = np.asarray(c, dtype=float)
    nvar = len(c_arr)

    # Constraint rows.
    rows: List[np.ndarray] = []
    lo: List[float] = []
    hi: List[float] = []

    # One type per job:  sum_i x[j,i] <= 1
    for jid in job_ids:
        present = [idx[(jid, g)] for g in gpu_types if (jid, g) in idx]
        if not present:
            continue
        r = np.zeros(nvar)
        r[present] = 1.0
        rows.append(r); lo.append(0.0); hi.append(1.0)

    # Per-type capacity:  sum_j d_j x[j,i] <= cap_i
    for g in gpu_types:
        present = [(idx[(jid, g)], job_demands[jid])
                   for jid in job_ids if (jid, g) in idx]
        if not present:
            continue
        r = np.zeros(nvar)
        for vi, dj in present:
            r[vi] = dj
        rows.append(r); lo.append(0.0); hi.append(float(capacity[g]))

    A = np.asarray(rows, dtype=float)
    lo_arr = np.asarray(lo, dtype=float)
    hi_arr = np.asarray(hi, dtype=float)

    # --- Fast path: LP relaxation. If integral, it is the exact MILP optimum. ---
    # linprog needs A_ub x <= b_ub; encode lo <= A x <= hi as two-sided.
    A_ub = np.vstack([A, -A])
    b_ub = np.concatenate([hi_arr, -lo_arr])
    lp = linprog(c_arr, A_ub=A_ub, b_ub=b_ub,
                 bounds=[(0.0, 1.0)] * nvar, method="highs")
    if lp.x is not None and np.all(np.abs(lp.x - np.round(lp.x)) < 1e-6):
        sol = np.round(lp.x)
        return {jid: g for (jid, g), k in idx.items() if sol[k] > 0.5}

    # --- Exact fallback: true MILP (HiGHS branch-and-bound, in-process). ---
    res = milp(
        c=c_arr,
        constraints=LinearConstraint(A, lo_arr, hi_arr),
        integrality=np.ones(nvar),
        bounds=Bounds(0.0, 1.0),
    )
    if res.x is None:
        return {}
    sol = np.round(res.x)
    return {jid: g for (jid, g), k in idx.items() if sol[k] > 0.5}

```

### fft_scheduler_proc.py

```python
"""
fft_scheduler_proc.py — the FFT baseline as a real process (fft mode).

Centralised and synchronous, per the paper (Sec. 4.1): allocations are decided
only at round boundaries. An arriving job WAITS for the next round's ILP solve
— its measured admission latency = (solve completion) - (arrival), which is
exactly the centralised bottleneck your research gap names. Every new job pays
the on-the-fly profiling stall (Sec. 5). Migrations decided by the solve incur
the real (scaled) state-transfer stall via the agents.
"""
from __future__ import annotations
import time
from common import R, now, emit, load_job, update_job, rpc
from config import (ROUND_SECONDS, PROFILE_SECONDS, N_PER_TYPE,
                    rounds_to_real, real_to_rounds)
from workload import GPU_TYPES
from ilp_core import FairnessState, solve

def run():
    r = R()
    fair = FairnessState(mu=1.0)
    cap = {g: N_PER_TYPE for g in GPU_TYPES}
    t0 = float(r.get("run_t0") or now())
    known: dict[int, bool] = {}
    emit(r, "fft_up")
    while r.get("shutdown") != "1":
        time.sleep(rounds_to_real(1.0))              # the round gate
        # drain arrivals that occurred during the round
        while True:
            got = r.rpop("arrivals")
            if got is None: break
            jid = int(got)
            known[jid] = False
            update_job(r, jid, pending_stall=PROFILE_SECONDS / ROUND_SECONDS,
                       route="fft")
            emit(r, "profile_stall", job=jid, rounds=PROFILE_SECONDS / ROUND_SECONDS)
        # active set
        jobs = []
        for jid in list(known):
            j = load_job(r, jid)
            if j and j["status"] != "done": jobs.append(j)
            elif j and j["status"] == "done": known.pop(jid, None)
        if not jobs: continue

        t_rounds = real_to_rounds(now() - t0)
        t_solve = now()
        alloc = solve(jobs, cap, t_rounds, fair)
        solve_end = now()
        emit(r, "fft_solve", n=len(jobs), wall=solve_end - t_solve)

        for j in jobs:
            jid = j["id"]
            if not known.get(jid):
                known[jid] = True
                update_job(r, jid, decide_ts=solve_end)
                emit(r, "decision", job=jid,
                     latency=solve_end - j["arrival_ts"], route="fft")
            target = alloc.get(jid)
            if target is None or j.get("gpu") == target: continue
            if j.get("gpu") is None:
                ack = rpc(r, target, "reserve",
                          {"job": jid, "stall_rounds": j.get("pending_stall", 0.0) or 0.0})
                if ack.get("ok"):
                    update_job(r, jid, pending_stall=0.0)
                    emit(r, "placed", job=jid, gpu=target, how="fft")
            else:
                src = j["gpu"]
                if int(r.get(f"free:{target}") or 0) < j["d"]:
                    continue
                if rpc(r, src, "evict", {"job": jid}).get("ok"):
                    time.sleep(0.15)
                    ack = rpc(r, target, "reserve",
                              {"job": jid, "migrate_from": src})
                    if ack.get("ok"):
                        r.lrem("queue:global", 0, str(jid))
                        emit(r, "placed", job=jid, gpu=target, how="fft_migrate")

if __name__ == "__main__":
    run()

```

### ilp_core.py

```python
"""
ilp_core.py — the FFT paper's per-round optimisation, shared by both the
centralised FFT scheduler process and the smart architecture's background brain
(your design reuses FFT's exact mathematics as the brain, spec Sec. 7).

Costs per (job, type), identical to scheduler_simulation_v2/fft_baseline.py:
  phi_j^i(t)  = (t-a_j)*theta/W_j + d_j*W_j/theta        (JCT term, paper Eq. 3)
  s_j^i(t)    = switch penalty if changing type           (Sec. 4.2.3)
  rho_j(t)    = fairness compensation, paper Eq. 5 with exact mu_j(t)
  continuity  = small keep-running reward (switching-control intent)
  base=100    = strong work-conservation reward (Eq. 7 as reward; hard
                constraint is infeasible in tight regimes — documented lesson)
Solved EXACTLY in-process by fast_solver (LP fast path + HiGHS MILP).
`t` is in ROUNDS of scaled time since run start.
"""
from __future__ import annotations
from typing import Dict, List
from fast_solver import solve_assignment
from workload import GPU_TYPES

SWITCH_PENALTY = 0.5

class FairnessState:
    def __init__(self, mu: float = 1.0):
        self.rho: Dict[int, float] = {}
        self.mu = mu

    def update(self, jobs: List[dict], t_rounds: float):
        n = max(1, len(jobs))
        for j in jobs:
            jid = j["id"]
            self.rho.setdefault(jid, 0.0)
            theta = j["theta"]
            best = max(theta.values()) if theta else 1.0
            tau = j["W"] / max(1e-6, best / n)          # duration on 1/N share
            fair_rate = j["W"] / max(1e-6, tau)
            done_rate = theta.get(j.get("gpu") or "", 0.0)
            age_r = max(0.0, t_rounds - j["arrival_rounds"])
            mu_t = self.mu * age_r / max(1e-6, tau)     # exact mu_j(t), Eq. 5
            self.rho[jid] = max(0.0, self.rho[jid] + mu_t * (fair_rate - done_rate))

def build_costs(jobs: List[dict], t_rounds: float, fair: FairnessState):
    costs, demands, ids = {}, {}, []
    for j in jobs:
        jid = j["id"]; ids.append(jid); demands[jid] = j["d"]
        for g in GPU_TYPES:
            th = j["theta"].get(g, 0.0)
            if th <= 0: continue
            age_r = max(0.0, t_rounds - j["arrival_rounds"])
            phi = age_r * th / max(1e-6, j["W"]) + j["d"] * j["W"] / th
            sw = 0.0 if (j.get("gpu") in (None, g)) else SWITCH_PENALTY * (j["state_gb"] / 10.0)
            cont = 0.5 * th if (j.get("gpu") == g and 0 < j["progress"] < j["W"]) else 0.0
            fairr = fair.rho.get(jid, 0.0) * th
            costs[(jid, g)] = -(100.0 + fairr + cont) + 0.1 * (phi + sw)
    return ids, demands, costs

def solve(jobs: List[dict], capacity: Dict[str, int], t_rounds: float,
          fair: FairnessState) -> Dict[int, str]:
    if not jobs: return {}
    fair.update(jobs, t_rounds)
    ids, demands, costs = build_costs(jobs, t_rounds, fair)
    return solve_assignment(ids, demands, list(GPU_TYPES), capacity, costs)

```

### node_agent.py

```python
"""
node_agent.py — component 7 (Decentralised Zone), one PROCESS per GPU type.

Owns the hardware truth for its type: a slot table nobody else may mutate.
All placement goes through its RPC loop — that IS the decentralised handshake:
  reserve : verify free slots >= d, start a worker thread, ACK; else NACK
            (a NACK observed by the brain = a real handshake rejection)
  evict   : set the worker's preempt flag; worker checkpoints progress to Redis,
            frees its slots, job returns to the global queue (Mechanism E)

Workers are threads that SLEEP in ticks (no real training), advancing
progress at theta epochs per scaled round, consuming any stall (profiling /
migration state-transfer) first — same physics as scheduler_simulation_v2.
"""
from __future__ import annotations
import json, socket, sys, threading, time
from common import R, now, emit, load_job, update_job, safe_brpop
from config import (TICK_REAL, real_to_rounds, rounds_to_real, scaled,
                    LAN_GBPS, N_PER_TYPE, REDIS_HOST, REDIS_PORT)

class Agent:
    def __init__(self, gpu_type: str, slots: int):
        self.g = gpu_type
        self.slots = slots
        self.free = slots
        self.lock = threading.Lock()
        self.preempt: dict[int, threading.Event] = {}
        self.r = None

    # ---------------- worker thread ----------------
    def _worker(self, job: dict, stall_rounds: float):
        r = R()
        jid = job["id"]
        prog = job["progress"]
        stall = stall_rounds
        t0 = now()
        update_job(r, jid, status="running", gpu=self.g)
        if prog == 0 and not job.get("first_exec_ts"):
            update_job(r, jid, first_exec_ts=t0)
            emit(r, "first_exec", job=jid, gpu=self.g)
        theta = job["theta"][self.g]
        last = t0
        while True:
            time.sleep(TICK_REAL)
            t = now(); dt_rounds = real_to_rounds(t - last); last = t
            if stall > 0:                      # profiling / state-transfer stall
                use = min(stall, dt_rounds); stall -= use; dt_rounds -= use
            prog += theta * dt_rounds
            if self.preempt.get(jid, threading.Event()).is_set():
                # ---- Mechanism E eviction: checkpoint, free, requeue ----
                update_job(r, jid, progress=prog, status="queued", gpu=None)
                self._release(jid, job["d"])
                r.lpush("queue:global", jid)
                r.lpush("chan:queue_event", "1")
                emit(r, "preempted", job=jid, gpu=self.g, progress=prog)
                return
            if prog >= job["W"]:
                update_job(r, jid, progress=prog, status="done",
                           finish_ts=now(), gpu=None)
                self._release(jid, job["d"])
                r.incr("done_count")
                emit(r, "finished", job=jid, gpu=self.g)
                return
            update_job(r, jid, progress=prog)

    def _release(self, jid, d):
        with self.lock:
            self.free += d
            if self.r:
                self.r.set(f"free:{self.g}", self.free)
                self.r.srem(f"running:{self.g}", jid)
                self.r.srem("running:fast", jid)
            self.preempt.pop(jid, None)

    # ---------------- RPC loop (the handshake) ----------------
    def run(self):
        print(f"[{self.g}] Agent started with {self.slots} slots. Target Redis: {REDIS_HOST}:{REDIS_PORT}", flush=True)
        while True:
            # 1. Wait for Redis to be reachable and previous shutdown cleared
            attempts = 0
            while True:
                try:
                    r = R()
                    r.ping()
                    if r.get("shutdown") != "1":
                        self.r = r
                        break
                except Exception:
                    pass
                if attempts % 5 == 0:
                    print(f"[{self.g}] Waiting for Redis at {REDIS_HOST}:{REDIS_PORT}...", flush=True)
                attempts += 1
                time.sleep(1)

            # 2. Reset local state for fresh run
            with self.lock:
                self.free = self.slots
                self.preempt.clear()

            try:
                r = self.r
                r.set(f"free:{self.g}", self.free)
                emit(r, "agent_up", gpu=self.g, slots=self.slots)
                r.sadd("agent_hosts", socket.gethostname())
                print(f"[{self.g}] Ready for run. Advertised {self.free} slots to Redis.", flush=True)

                # 3. Process requests until shutdown signal is received
                while True:
                    if r.get("shutdown") == "1":
                        print(f"[{self.g}] Run completed (shutdown signal received). Draining workers...", flush=True)
                        # --- Run-epoch reset: wait for worker threads to finish ---
                        deadline = time.time() + 10.0
                        while self.preempt and time.time() < deadline:
                            time.sleep(0.2)
                        if self.preempt:
                            print(f"[{self.g}] WARNING: {len(self.preempt)} workers did not drain in 10s", flush=True)
                        # Drain stale RPC requests left over from this run
                        drained = 0
                        while True:
                            stale = safe_brpop(r, f"agent:{self.g}:req", 0.1)
                            if stale is None:
                                break
                            drained += 1
                        if drained:
                            print(f"[{self.g}] Drained {drained} stale RPC request(s)", flush=True)
                        print(f"[{self.g}] Reset complete. Waiting for next run...", flush=True)
                        time.sleep(1.5)
                        break

                    got = safe_brpop(r, f"agent:{self.g}:req", 1)
                    if not got:
                        continue

                    req = json.loads(got[1])
                    op, resp = req["op"], req["resp"]
                    if op == "reserve":
                        job = load_job(r, req["job"])
                        migrate_from = req.get("migrate_from")
                        with self.lock:
                            ok = job is not None and self.free >= job["d"] \
                                 and job["status"] not in ("done", "running")
                            if ok:
                                self.free -= job["d"]
                                r.set(f"free:{self.g}", self.free)
                                r.sadd(f"running:{self.g}", job["id"])
                                if req.get("route") == "fast":
                                    r.sadd("running:fast", job["id"])
                        if ok:
                            stall = req.get("stall_rounds", 0.0)
                            if migrate_from and migrate_from != self.g and job["progress"] > 0:
                                # real migration cost: state transfer over the (scaled) LAN
                                stall += (job["state_gb"] / LAN_GBPS) / 300.0
                                emit(r, "migration", job=job["id"],
                                     src=migrate_from, dst=self.g)
                            ev = threading.Event(); self.preempt[job["id"]] = ev
                            threading.Thread(target=self._worker,
                                             args=(job, stall), daemon=True).start()
                        r.lpush(resp, json.dumps({"ok": bool(ok)}))
                        if not ok:
                            emit(r, "handshake_reject", job=req["job"], gpu=self.g)
                    elif op == "evict":
                        jid = req["job"]
                        ev = self.preempt.get(jid)
                        if ev: ev.set()
                        r.lpush(resp, json.dumps({"ok": ev is not None}))
                    elif op == "ping":
                        r.lpush(resp, json.dumps({"ok": True, "time": time.time()}))
                    else:
                        r.lpush(resp, json.dumps({"ok": False, "err": "bad op"}))
            except Exception as e:
                print(f"[{self.g}] Redis disconnected ({e}). Waiting for next stack to come up...", flush=True)
                time.sleep(1)

if __name__ == "__main__":
    g = sys.argv[1]
    slots = int(sys.argv[2]) if len(sys.argv) > 2 else N_PER_TYPE
    Agent(g, slots).run()

```

### orchestrator.py

```python
"""
orchestrator.py — Option B run driver (executes INSIDE the compose network).

Replaces launcher.py's process-spawning role: in Option B the components are
already running as containers, so this only (1) waits until all node agents
have registered their slot tables, (2) replays the trace, (3) waits for
completion, (4) analyzes and saves metrics to the mounted ./runs volume,
(5) signals shutdown so `docker compose down` exits cleanly.

Usage (from the host):
  docker compose --profile smart up -d
  docker compose --profile tools run --rm orchestrator 60 bursty 1 smart 1800
  docker compose --profile smart down
"""
from __future__ import annotations
import json, os, sys, time
from common import R
from workload import GPU_TYPES
from trace_replayer import replay
from analyze_results import collect, metrics

def main():
    jobs = int(sys.argv[1]); regime = sys.argv[2]; seed = int(sys.argv[3])
    mode = sys.argv[4] if len(sys.argv) > 4 else "run"
    timeout = float(sys.argv[5]) if len(sys.argv) > 5 else 1800.0
    r = R()

    print("waiting for node agents to register slot tables...")
    for _ in range(120):
        if all(r.exists(f"free:{g}") for g in GPU_TYPES):
            break
        time.sleep(0.5)
    else:
        sys.exit("agents never came up — check `docker compose ps` / logs")
    print("agents ready:", {g: r.get(f'free:{g}') for g in GPU_TYPES})

    replay(jobs, regime, seed)            # blocks until all arrivals fired
    print("replay done; waiting for jobs to finish...")

    t0 = time.time()
    while time.time() - t0 < timeout:
        total, done = r.get("total_jobs"), r.get("done_count")
        if total and done and int(done) >= int(total):
            print(f"all {total} jobs finished")
            break
        time.sleep(2.0)
    else:
        print("TIMEOUT — saving PARTIAL metrics (check n_finished!)")

    m = metrics(*collect(r))
    agent_hosts = list(r.smembers("agent_hosts") or [])
    m["agent_hosts"] = agent_hosts

    os.makedirs("runs", exist_ok=True)
    path = f"runs/{mode}_{regime}_s{seed}.json"
    json.dump(m, open(path, "w"), indent=2)
    print(json.dumps(m, indent=2)); print("saved ->", path)
    r.set("shutdown", "1")

if __name__ == "__main__":
    main()

```

### preflight.ps1

```powershell
Write-Host "[*] Starting Redis container on PC 1 for preflight tests..."
docker compose -f docker-compose.pc1.yml up -d redis

Write-Host "[*] Running preflight diagnostic..."
# Run the python script using the tools profile to ensure it has all dependencies (redis, etc.)
docker compose -f docker-compose.pc1.yml --profile tools run --rm orchestrator python preflight.py

Write-Host "[*] Shutting down Redis..."
docker compose -f docker-compose.pc1.yml down

```

### preflight.py

```python
import os, sys, time, statistics
from common import R, rpc
from workload import GPU_TYPES
from config import N_PER_TYPE

def main():
    print("=" * 60)
    print("   PREFLIGHT CHECK: Two-Machine Distributed Emulation")
    print("=" * 60)

    # 1. Connect to Redis
    try:
        r = R()
        r.ping()
        print("[+] Redis reachable on PC 1")
    except Exception as e:
        print(f"[-] Redis unreachable: {e}")
        print("    Did you run 'docker compose -f docker-compose.pc1.yml up -d redis'?")
        sys.exit(1)

    # 2. Measure Redis RTT (Thesis Requirement)
    rtts = []
    for _ in range(10):
        t0 = time.time()
        r.ping()
        rtts.append((time.time() - t0) * 1000)
    print(f"[+] Redis RTT on PC 1: {statistics.mean(rtts):.3f} ms (min: {min(rtts):.3f}, max: {max(rtts):.3f})")

    # 3. Check for PC 2 Agents
    print("\n[*] Waiting for PC 2 agents to connect (max 10s)...")
    r.set("shutdown", "0")  # Ensure they aren't waiting to reset
    agents_ready = 0
    for _ in range(20):
        agents_ready = sum(1 for g in GPU_TYPES if r.exists(f"free:{g}"))
        if agents_ready == len(GPU_TYPES):
            break
        time.sleep(0.5)

    if agents_ready < len(GPU_TYPES):
        print(f"[-] Only {agents_ready}/{len(GPU_TYPES)} agents connected!")
        print("    Did you run 'docker compose -f docker-compose.pc2.yml up -d' on PC 2?")
        print("    Is the Cat 8 cable connected and firewall open?")
        sys.exit(1)

    # 4. Verify Slot Counts
    slot_errors = 0
    for g in GPU_TYPES:
        slots = int(r.get(f"free:{g}") or 0)
        if slots != N_PER_TYPE:
            print(f"[-] Agent {g} reported {slots} slots, but expected {N_PER_TYPE}!")
            slot_errors += 1
        else:
            print(f"[+] Agent {g} registered {slots} slots.")
    if slot_errors > 0:
        print("    PC 2 might be using an old .env or didn't rebuild properly.")
        sys.exit(1)

    # 5. Measure Clock Skew via RPC
    print("\n[*] Measuring Cat 8 RPC Latency & Clock Skew...")
    # Send ping RPC to PC 2's T4 agent
    t_start = time.time()
    ack = rpc(r, "T4", "ping", {}, timeout=5.0)
    t_end = time.time()
    
    if not ack.get("ok"):
        print("[-] Agent RPC ping failed or timed out!")
        sys.exit(1)

    rpc_rtt_ms = (t_end - t_start) * 1000
    pc2_time = ack["time"]
    
    # Calculate skew: PC 2's time compared to the midpoint of our RPC request
    expected_pc2_time = t_start + ((t_end - t_start) / 2)
    skew_ms = (pc2_time - expected_pc2_time) * 1000

    print(f"[+] Cross-machine RPC RTT: {rpc_rtt_ms:.2f} ms")
    if abs(skew_ms) > 20:
        print(f"[-] WARNING: Clock Skew Detected: {skew_ms:+.1f} ms")
        print("    PC 2's clock is significantly out of sync with PC 1.")
        print("    This will corrupt Starvation metrics (which cross machine boundaries).")
        print("    Fix: Sync time on both Windows machines via 'w32tm /resync' before running.")
    else:
        print(f"[+] Clocks are well synchronised (Skew: {skew_ms:+.1f} ms)")

    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED. You are cleared to run the 36-experiment suite.")
    print("=" * 60)

if __name__ == "__main__":
    main()

```

### run_all_36.py

```python
"""
run_all_36.py — Master Automated Test Runner for Distributed 36-GPU Simulation.

Runs all 36 workload combinations across PC 1 and PC 2:
  2 Modes (smart, fft) x 6 Regimes (bursty, dynamic, heavy, mixed, random, steady) x 3 Seeds (1, 2, 3) = 36 runs.

Usage:
  python run_all_36.py              # Run all 36 runs automatically
  python run_all_36.py --mode smart # Run only 18 runs for SMART scheduler
  python run_all_36.py --mode fft   # Run only 18 runs for FFT baseline
  python run_all_36.py --force      # Re-run completed runs (default resumes/skips completed)
"""
import argparse, json, os, subprocess, sys, time

ALL_MODES = ["smart", "fft"]
ALL_REGIMES = ["bursty", "dynamic", "heavy", "mixed", "random", "steady"]
ALL_SEEDS = [1, 2, 3]

COMPOSE_FILE = "docker-compose.pc1.yml"

def run_cmd(cmd, check=True):
    """Run shell command and return CompletedProcess."""
    return subprocess.run(cmd, shell=True, check=check)

def is_run_completed(mode, regime, seed, jobs):
    """Check if output json already exists and finished successfully."""
    path = f"runs/{mode}_{regime}_s{seed}.json"
    if not os.path.exists(path):
        return False
    try:
        data = json.load(open(path))
        return data.get("n_finished", 0) >= jobs
    except Exception:
        return False

def clean_shutdown(mode):
    """Safely bring down PC 1 containers."""
    try:
        subprocess.run(
            f"docker compose -f {COMPOSE_FILE} --profile {mode} down",
            shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:
        pass

def main():
    parser = argparse.ArgumentParser(description="Automate all 36 cluster runs across PC 1 and PC 2.")
    parser.add_argument("--mode", choices=["all", "smart", "fft"], default="all", help="Scheduler mode to run")
    parser.add_argument("--regime", choices=["all"] + ALL_REGIMES, default="all", help="Specific regime or all")
    parser.add_argument("--seeds", default="1,2,3", help="Comma-separated seeds, e.g. '1,2,3'")
    parser.add_argument("--jobs", type=int, default=60, help="Number of jobs per run (default: 60)")
    parser.add_argument("--timeout", type=int, default=1800, help="Timeout in seconds per run")
    parser.add_argument("--force", action="store_true", help="Force re-run even if run output file exists")
    args = parser.parse_args()

    modes = ALL_MODES if args.mode == "all" else [args.mode]
    regimes = ALL_REGIMES if args.regime == "all" else [args.regime]
    seeds = [int(s.strip()) for s in args.seeds.split(",")]

    total_runs = len(modes) * len(regimes) * len(seeds)
    print("=" * 80)
    print(f"   STARTING AUTOMATED EXPERIMENT SUITE ({total_runs} TOTAL RUNS)")
    print(f"   Modes:   {modes}")
    print(f"   Regimes: {regimes}")
    print(f"   Seeds:   {seeds}")
    print(f"   Jobs:    {args.jobs} per run | Timeout: {args.timeout}s")
    print("=" * 80)

    current_idx = 0
    completed_count = 0
    skipped_count = 0

    current_mode = None

    try:
        for mode in modes:
            for regime in regimes:
                for seed in seeds:
                    current_idx += 1
                    label = f"[{current_idx}/{total_runs}] {mode.upper()} | {regime} | seed {seed}"
                    
                    if not args.force and is_run_completed(mode, regime, seed, args.jobs):
                        print(f"\n>> {label} -> ALREADY COMPLETED (Skipping)")
                        skipped_count += 1
                        continue

                    print(f"\n" + "=" * 80)
                    print(f"   RUNNING {label}")
                    print(f"=" * 80)

                    current_mode = mode

                    # Step 1: Start PC 1 stack
                    print(f"[*] Starting PC 1 stack (profile: {mode})...")
                    run_cmd(f"docker compose -f {COMPOSE_FILE} --profile {mode} up -d")

                    # Step 2: Give 3 seconds for Redis to be fully healthy and PC 2 agents to discover it
                    time.sleep(3)

                    # Step 3: Launch the orchestrator run
                    print(f"[*] Launching orchestrator for {args.jobs} jobs...")
                    cmd = (
                        f"docker compose -f {COMPOSE_FILE} --profile tools "
                        f"run --rm orchestrator {args.jobs} {regime} {seed} {mode} {args.timeout}"
                    )
                    res = run_cmd(cmd, check=False)

                    # Step 4: Cleanly bring down PC 1 stack to ensure fresh state for next seed
                    print(f"[*] Resetting PC 1 cluster state...")
                    run_cmd(f"docker compose -f {COMPOSE_FILE} --profile {mode} down")

                    if res.returncode == 0:
                        completed_count += 1
                        print(f"[+] Run {current_idx}/{total_runs} finished successfully.")
                    else:
                        print(f"[-] Run {current_idx}/{total_runs} failed or timed out (code: {res.returncode}).")

                    # Step 5: Brief cooldown before starting next run
                    time.sleep(3)

    except KeyboardInterrupt:
        print("\n[!] Execution interrupted by user (Ctrl+C). Cleaning up containers...")
        if current_mode:
            clean_shutdown(current_mode)
        sys.exit(1)

    print("\n" + "=" * 80)
    print(f"   EXPERIMENT SUITE FINISHED!")
    print(f"   Completed: {completed_count} | Skipped (already done): {skipped_count} | Total: {total_runs}")
    print("=" * 80)

    # Automatically generate the summary comparison table
    print("\nGenerating final comparative results...")
    try:
        run_cmd("python compare_all.py", check=False)
    except Exception as e:
        print(f"Could not run compare_all.py automatically: {e}")

if __name__ == "__main__":
    main()

```

### sync_to_pc2.ps1

```powershell
param (
    [string]$Destination = ""
)

if ($Destination -eq "") {
    $Destination = Read-Host "Enter the UNC path to the Option B folder on PC 2 (e.g., \\192.168.100.2\Share\Option B)"
}

if (-not (Test-Path $Destination)) {
    Write-Error "Destination path not accessible: $Destination"
    exit 1
}

Write-Host "[*] Syncing code to $Destination..."

# Sync all files, excluding .env, results, and caches. /MIR mirrors the directory (deletes missing files).
robocopy . $Destination /MIR /XD runs runs_v1 __pycache__ .git /XF .env *.zip *.pdf *.pyc

# robocopy exit codes: 0-7 are success (1 = files copied, 2 = extra files deleted, etc.)
if ($LASTEXITCODE -ge 8) {
    Write-Error "Robocopy failed with exit code $LASTEXITCODE"
    exit 1
}

Write-Host "`n[+] Sync complete!"
Write-Host "⚠️  IMPORTANT: Because Python files were updated, you MUST rebuild the agents on PC 2!"
Write-Host "    Go to PC 2 and run:"
Write-Host "    docker compose -f docker-compose.pc2.yml up -d --build"

```

### test_connection.py

```python
"""
test_connection.py — Verify Cat 8 connection and Redis RPC latency between PC 1 and PC 2.

Usage:
  python test_connection.py [host]
  Example: python test_connection.py 192.168.100.1
"""
import sys, time
import redis

host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
port = 6379

print(f"[*] Testing connection to Redis at {host}:{port}...")

try:
    r = redis.Redis(host=host, port=port, socket_connect_timeout=3, decode_responses=True)
    t0 = time.time()
    res = r.ping()
    dt = (time.time() - t0) * 1000
    print(f"[+] PING response: {res} (RTT: {dt:.2f} ms)")

    # Test roundtrip latency over 10 iterations
    latencies = []
    for i in range(10):
        t0 = time.time()
        r.set(f"test:ping:{i}", "1", ex=10)
        _ = r.get(f"test:ping:{i}")
        latencies.append((time.time() - t0) * 1000)
    
    avg_lat = sum(latencies) / len(latencies)
    min_lat = min(latencies)
    max_lat = max(latencies)
    print(f"[+] 10-packet Redis RPC test:")
    print(f"    - Avg Latency: {avg_lat:.3f} ms")
    print(f"    - Min Latency: {min_lat:.3f} ms")
    print(f"    - Max Latency: {max_lat:.3f} ms")
    print("[SUCCESS] Cat 8 direct link and Redis connection verified successfully!")

except Exception as e:
    print(f"[-] Connection failed: {e}")
    print("\nTroubleshooting tips:")
    print(" 1. Check if the Cat 8 cable is plugged in and Ethernet adapters show 'Connected'.")
    print(" 2. Verify static IPs (PC 1: 192.168.100.1, PC 2: 192.168.100.2).")
    print(" 3. Ensure Windows Firewall on PC 1 allows inbound TCP port 6379.")
    print(" 4. Ensure Redis is running on PC 1 (`docker compose -f docker-compose.pc1.yml --profile smart up -d`).")

```

### trace_replayer.py

```python
"""trace_replayer.py — component 1. Replays the workload trace in REAL time.

Reuses the exact Philly-statistics generator from scheduler_simulation_v2, so
the emulation and the discrete-event simulation can consume the SAME trace
(same seed/regime/jobs) — that is what enables the emulation-vs-simulation
validation figure (the FFT paper validated its simulator against its physical
testbed the same way, <=4.9% JCT deviation)."""
from __future__ import annotations
import sys, time
from common import R, now, save_job, emit
from config import rounds_to_real
from workload import generate_trace, profile_job

def replay(n_jobs: int, regime: str, seed: int):
    r = R()
    trace = generate_trace(n_jobs=n_jobs, regime=regime, seed=seed,
                           horizon=max(60, n_jobs * 1.4))
    for j in trace:
        profile_job(j)             # theta known to the SYSTEM's profiler model
    r.set("total_jobs", len(trace))
    t0 = now(); r.set("run_t0", t0)
    emit(r, "run_start", n_jobs=len(trace), regime=regime, seed=seed)
    for j in trace:
        wait = t0 + rounds_to_real(j.arrival) - now()
        if wait > 0: time.sleep(wait)
        rec = {"id": j.job_id, "model": j.model, "d": j.d_j, "W": j.W_j,
               "theta": j.theta, "state_gb": j.state_gb,
               "arrival_ts": now(), "arrival_rounds": j.arrival,
               "progress": 0.0, "status": "arrived", "gpu": None}
        save_job(r, rec)
        r.lpush("arrivals", str(j.job_id))
        emit(r, "arrival", job=j.job_id, model=j.model, d=j.d_j, W=j.W_j)
    emit(r, "replay_done")

if __name__ == "__main__":
    replay(int(sys.argv[1]), sys.argv[2], int(sys.argv[3]))

```

### workload.py

```python
"""
workload.py
-----------
Synthetic DL training-job trace generator.

Replicates the *statistical* properties of the Microsoft Philly trace
(Poisson-like arrivals, log-normal processing times, variable worker counts)
rather than executing real neural networks. Each job carries per-GPU-type
throughput derived from a small model zoo, so the schedulers can compute
completion times mathematically.

Nothing here trains anything. A "job" is a parameter tuple.
"""

from __future__ import annotations
import math
import random
from dataclasses import dataclass, field
from typing import Dict, List


# ----------------------------------------------------------------------
# GPU types. Throughput multipliers are relative speeds (epochs/round)
# for a reference model. Higher = faster device.
# Loosely modelled on the T4 / V100 / A10 ordering used in the FFT paper.
# ----------------------------------------------------------------------
GPU_TYPES: Dict[str, Dict[str, float]] = {
    "T4":   {"speed": 1.0, "memory_gb": 16},   # low end
    "V100": {"speed": 2.0, "memory_gb": 32},   # mid
    "A10":  {"speed": 2.7, "memory_gb": 48},   # high end (per FFT speedups 1.6-2.71x)
}

# Capability score C_n per GPU type (used by the dispatcher's S_node).
GPU_CAPABILITY: Dict[str, float] = {
    "T4": 1.0,
    "V100": 2.0,
    "A10": 2.7,
}


# ----------------------------------------------------------------------
# Model zoo. base_epoch_cost = work units per epoch on a *reference*
# (speed = 1.0) GPU. mem_gb = memory footprint, used to forbid placing
# large models on small-memory GPUs. state_gb = checkpoint/migration size.
# ----------------------------------------------------------------------
MODEL_ZOO: Dict[str, Dict[str, float]] = {
    "ResNet50":  {"base_epoch_cost": 1.0,  "mem_gb": 8,  "state_gb": 0.3},
    "VGG19":     {"base_epoch_cost": 1.3,  "mem_gb": 11, "state_gb": 0.5},
    "DenseNet":  {"base_epoch_cost": 1.1,  "mem_gb": 9,  "state_gb": 0.4},
    "BERT-base": {"base_epoch_cost": 2.0,  "mem_gb": 14, "state_gb": 1.2},
    "GPT-neo":   {"base_epoch_cost": 4.0,  "mem_gb": 22, "state_gb": 50.0},
    "GPT-2":     {"base_epoch_cost": 5.0,  "mem_gb": 28, "state_gb": 60.0},
    "OPT-6.7B":  {"base_epoch_cost": 8.0,  "mem_gb": 30, "state_gb": 107.0},
}

MODEL_NAMES = list(MODEL_ZOO.keys())


@dataclass
class Job:
    job_id: int
    arrival: float            # arrival time (in rounds, continuous)
    model: str
    d_j: int                  # requested workers (GPUs)
    W_j: int                  # required epochs

    # --- filled in by profiling / scheduling ---
    theta: Dict[str, float] = field(default_factory=dict)  # throughput per GPU type (epochs/round)
    state_gb: float = 0.0
    mem_gb: float = 0.0

    # --- runtime bookkeeping (mutated by the engine) ---
    epochs_done: float = 0.0
    admit_time: float | None = None       # when it first got a GPU slot
    first_exec_time: float | None = None  # first time actually executed
    finish_time: float | None = None
    current_gpu: str | None = None        # GPU type it is currently on
    migrations: int = 0
    stall: float = 0.0            # rounds of no-progress remaining (migration/profiling)
    migration_time_lost: float = 0.0  # cumulative rounds lost to state transfer
    profile_time_lost: float = 0.0    # cumulative rounds lost to profiling

    def remaining_epochs(self) -> float:
        return max(0.0, self.W_j - self.epochs_done)

    def is_done(self) -> bool:
        return self.epochs_done >= self.W_j

    def throughput_on(self, gpu_type: str) -> float:
        return self.theta.get(gpu_type, 0.0)


def compute_throughput(model: str, gpu_type: str) -> float:
    """Epochs completed per scheduling round for `model` on `gpu_type`.

    Scaled so a typical job occupies its GPUs for many rounds (as real DL
    training does), which creates genuine queueing/contention under load.
    """
    base = MODEL_ZOO[model]["base_epoch_cost"]
    speed = GPU_TYPES[gpu_type]["speed"]
    # epochs/round = device speed / per-epoch cost. Small values => long jobs.
    return round(speed / base * 0.5, 4)


def can_fit(model: str, gpu_type: str) -> bool:
    """Memory feasibility: big models cannot run on small-memory GPUs."""
    return MODEL_ZOO[model]["mem_gb"] <= GPU_TYPES[gpu_type]["memory_gb"]


def profile_job(job: Job) -> None:
    """Fill in throughput / memory / state for a job (the 'micro-profiler')."""
    job.mem_gb = MODEL_ZOO[job.model]["mem_gb"]
    job.state_gb = MODEL_ZOO[job.model]["state_gb"]
    job.theta = {}
    for g in GPU_TYPES:
        job.theta[g] = compute_throughput(job.model, g) if can_fit(job.model, g) else 0.0


# ----------------------------------------------------------------------
# Trace generation
# ----------------------------------------------------------------------
def generate_trace(
    n_jobs: int = 200,
    regime: str = "mixed",
    seed: int = 0,
    horizon: float = 400.0,
) -> List[Job]:
    """
    Generate a list of Jobs sorted by arrival time.

    regime:
      - "steady": near-uniform arrivals, smaller jobs dominate.
      - "mixed":  Poisson arrivals, balanced model mix.
      - "bursty": arrivals clustered into bursts (stress-tests admission latency).
    """
    rng = random.Random(seed)
    jobs: List[Job] = []

    # Model-mix weights per regime.
    if regime == "steady":
        weights = [0.30, 0.15, 0.20, 0.20, 0.07, 0.05, 0.03]
        mean_interarrival = horizon / n_jobs
    elif regime == "bursty":
        weights = [0.20, 0.10, 0.10, 0.15, 0.18, 0.15, 0.12]  # more big jobs
        mean_interarrival = horizon / n_jobs
    else:  # mixed
        weights = [0.22, 0.13, 0.15, 0.18, 0.12, 0.12, 0.08]
        mean_interarrival = horizon / n_jobs

    # --- arrival times ---
    if regime == "bursty":
        # Cluster arrivals into a handful of bursts.
        n_bursts = max(3, n_jobs // 25)
        burst_centers = sorted(rng.uniform(0, horizon) for _ in range(n_bursts))
        arrivals = []
        for _ in range(n_jobs):
            c = rng.choice(burst_centers)
            a = max(0.0, rng.gauss(c, horizon * 0.01))  # tight cluster around centre
            arrivals.append(a)
        arrivals.sort()
    else:
        # Poisson process (exponential inter-arrival), steady ~ less variance.
        arrivals = []
        t = 0.0
        for _ in range(n_jobs):
            if regime == "steady":
                gap = rng.uniform(0.5 * mean_interarrival, 1.5 * mean_interarrival)
            else:
                gap = rng.expovariate(1.0 / mean_interarrival)
            t += gap
            arrivals.append(t)

    for i, a in enumerate(arrivals):
        model = rng.choices(MODEL_NAMES, weights=weights, k=1)[0]
        # Worker count: log-normal-ish, capped at 8 and at least 1.
        d_j = min(8, max(1, int(round(rng.lognormvariate(0.4, 0.6)))))
        # Epochs: log-normal processing time.
        W_j = max(2, int(round(rng.lognormvariate(2.2, 0.7))))
        job = Job(job_id=i, arrival=round(a, 4), model=model, d_j=d_j, W_j=W_j)
        jobs.append(job)

    jobs.sort(key=lambda j: j.arrival)
    for new_id, j in enumerate(jobs):
        j.job_id = new_id
    return jobs


def make_cluster(n_per_type: int = 12) -> Dict[str, int]:
    """
    Cluster inventory: how many GPU workers exist per type.
    Default 12 each (36 GPUs total), matching the proposal's 36-GPU lab cluster.
    """
    return {g: n_per_type for g in GPU_TYPES}


# ----------------------------------------------------------------------
# Overhead models grounded in the FFT paper's own numbers.
# ----------------------------------------------------------------------
ROUND_SECONDS = 300.0      # 5-minute scheduling round (FFT paper default)
LAN_GBPS = 1.25            # 10 Gbps LAN (paper's testbed) = 1.25 GB/s
PROFILE_SECONDS = 60.0     # micro-profiling window (architecture spec, Slow Path)


def migration_stall_rounds(state_gb: float,
                           round_seconds: float = ROUND_SECONDS,
                           bw_gbps: float = LAN_GBPS) -> float:
    """Rounds of lost GPU time to transfer a job's training state between hosts.

    Paper grounding: migrating OPT-6.7B (107 GB state) over the 10 Gbps LAN
    costs ~86 s (paper's example says 'two minutes'). 107/1.25/300 = 0.285
    rounds, i.e. the job loses ~29% of one 5-minute round when it migrates.
    """
    return (state_gb / bw_gbps) / round_seconds


def profiling_stall_rounds(round_seconds: float = ROUND_SECONDS) -> float:
    """Rounds consumed by profiling a job before it can train.

    The FFT baseline profiles EVERY new job on the fly (paper Sec. 5: profiling
    tasks have top priority and suspend training; total impact up to 0.8%).
    The proposed architecture only pays this on a historical-cache MISS
    (60 s micro-profiling window per the architecture spec); cache HITs and
    Fast-Path jobs skip it (profiled silently on-the-fly).
    """
    return PROFILE_SECONDS / round_seconds


if __name__ == "__main__":
    for reg in ("steady", "mixed", "bursty"):
        trace = generate_trace(n_jobs=50, regime=reg, seed=1)
        profile_job(trace[0])
        print(f"[{reg}] {len(trace)} jobs, first arrival {trace[0].arrival}, "
              f"last {trace[-1].arrival:.1f}, sample model {trace[0].model}, "
              f"theta {trace[0].theta}")

```

