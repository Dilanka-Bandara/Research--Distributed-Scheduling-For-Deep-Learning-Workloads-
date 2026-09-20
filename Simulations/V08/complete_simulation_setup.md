# Complete Simulation Setup Document
## Distributed GPU Cluster Scheduling Emulation — Full Technical Reference

This document describes every detail of how the simulation works. It is intended as a complete knowledge-transfer for another AI or developer to fully understand and reproduce the setup.

---

## 1. Purpose of This Simulation

This is a **research thesis simulation** comparing two GPU cluster schedulers:
- **FFT (Fluid Fairness and Throughput)**: The state-of-the-art centralized ILP-based scheduler from an existing research paper.
- **SMART**: A proposed hybrid scheduler with a fast O(1) heuristic dispatcher backed by an asynchronous ILP brain.

The simulation does NOT run real neural network training. Instead, it mathematically simulates GPU training throughput, job progress, and scheduling decisions using Docker containers communicating over a real Redis instance. The key insight is that all **scheduling decisions, handshakes, and communication latencies are REAL** — only the training itself is simulated mathematically.

---

## 2. Physical Hardware Setup

### Two Windows Machines Connected by a Cat 8 Ethernet Cable

```
╔════════════════════════════════════════════════╗         CAT 8 CABLE          ╔════════════════════════════════════════════════╗
║            PC 1 (Controller / Master)          ║  ◄═══════════════════════►   ║          PC 2 (Compute Cluster)                ║
║                                                ║     Direct peer-to-peer      ║                                                ║
║  Static IP: 192.168.100.1                      ║     No router/switch         ║  Static IP: 192.168.100.2                      ║
║  Subnet:    255.255.255.0                      ║     < 1ms latency            ║  Subnet:    255.255.255.0                      ║
║                                                ║                              ║                                                ║
║  Runs:                                         ║                              ║  Runs:                                         ║
║   • Redis Server (port 6379)                   ║                              ║   • agent-t4   container (12 GPU slots)        ║
║   • Dispatcher container (SMART mode)          ║                              ║   • agent-v100 container (12 GPU slots)        ║
║   • Brain container (SMART mode)               ║                              ║   • agent-a10  container (12 GPU slots)        ║
║   • FFT Scheduler container (FFT mode)         ║                              ║                                                ║
║   • Orchestrator container (test runner)       ║                              ║  Total: 36 simulated GPUs                      ║
║                                                ║                              ║  (0 GPUs on PC 1 — pure controller)            ║
║  Also runs: run_all_36.py (automation script)  ║                              ║                                                ║
╚════════════════════════════════════════════════╝                              ╚════════════════════════════════════════════════╝
```

### Network Configuration

| Detail | PC 1 | PC 2 |
|--------|------|------|
| Role | Controller (no GPUs) | Compute cluster (36 GPUs) |
| Static IP | `192.168.100.1` | `192.168.100.2` |
| Subnet Mask | `255.255.255.0` | `255.255.255.0` |
| Default Gateway | (blank) | `192.168.100.1` (or blank) |
| Cable | Cat 8 Ethernet, direct peer-to-peer | Cat 8 Ethernet, direct peer-to-peer |
| Latency | < 1ms round-trip | < 1ms round-trip |

**Important**: No router or switch is used. The Cat 8 cable connects the two machines directly. Both machines must have their Ethernet adapter configured with static IPs in the `192.168.100.0/24` subnet.

### Firewall Requirement
On PC 1, Windows Firewall must allow **inbound TCP port 6379** so PC 2's containers can reach Redis:
```powershell
New-NetFirewallRule -DisplayName "Allow-Redis-6379" -Direction Inbound -LocalPort 6379 -Protocol TCP -Action Allow
```

---

## 3. Software Prerequisites

Both machines need:
- **Docker Desktop** for Windows (with WSL2 backend)
- **Python 3.12+** (on PC 1 only, for running `run_all_36.py` and `compare_all.py`)
- **Git** (optional, for version control)

Python packages required (installed inside Docker containers automatically):
- `redis` — Redis client
- `numpy` — Numerical operations
- `scipy` — ILP solver (HiGHS backend via `scipy.optimize.milp`)

---

## 4. Project File Structure

```
Option B/
├── .env                        # Environment variables (EMU_N_PER_TYPE=12, EMU_TIME_SCALE=100)
├── Dockerfile                  # Single Docker image for all components
├── docker-compose.yml          # Single-machine mode (all containers on one machine, 8 GPUs/type)
├── docker-compose.pc1.yml      # Two-machine mode: PC 1 services (Redis, dispatcher, brain, FFT, orchestrator)
├── docker-compose.pc2.yml      # Two-machine mode: PC 2 services (3 node agents, 12 GPUs/type each)
│
├── config.py                   # All tunable parameters, time scaling, Redis connection
├── common.py                   # Redis helpers, job CRUD, RPC protocol, telemetry
├── workload.py                 # GPU types, model zoo, throughput math, trace generation
│
├── trace_replayer.py           # Component 1: Replays job arrivals in real time
├── dispatcher.py               # Component 2: O(1) heuristic GPU selector (SMART mode only)
├── brain.py                    # Component 6: Asynchronous ILP brain (SMART mode only)
├── fft_scheduler_proc.py       # FFT baseline: Centralized round-gated ILP scheduler
├── node_agent.py               # Component 7: GPU slot manager + worker threads (one per GPU type)
├── ilp_core.py                 # FFT ILP math: cost function + solver interface
├── fast_solver.py              # In-process LP/MILP solver (replaces CBC subprocess)
│
├── orchestrator.py             # Test runner: waits for agents, replays trace, collects metrics
├── analyze_results.py          # Metric computation (JCT, FTF, starvation, latency)
├── run_all_36.py               # Master automation: runs all 36 experiments automatically
├── compare_all.py              # Aggregates results and prints comparison tables
├── test_connection.py          # Network diagnostic: tests Redis connectivity over Cat 8
│
├── PC2_SETUP_AND_AGENT_GUIDE.md # Setup guide for the PC 2 operator
├── FFT.pdf                     # The FFT research paper (state-of-the-art baseline)
├── README.md                   # Project overview
│
├── runs/                       # Output: 36 JSON result files (current optimized version)
│   ├── smart_bursty_s1.json
│   ├── smart_bursty_s2.json
│   ├── ...                     # 18 SMART + 18 FFT = 36 files total
│   └── fft_steady_s3.json
│
└── runs_v1/                    # Output: 36 JSON result files (original version, backed up)
```

---

## 5. The Docker Architecture

### 5.1 The Dockerfile (Shared by All Containers)

Every component uses the **same Docker image**. The Dockerfile installs Python 3.12, the `redis`, `numpy`, and `scipy` packages, and copies all `.py` files into `/app/`. Each container runs a different Python script as its command.

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir redis numpy scipy
WORKDIR /app
COPY *.py /app/
CMD ["python", "-c", "print('specify a command in docker-compose.yml')"]
```

### 5.2 Docker Compose Profiles

The system uses Docker Compose **profiles** to switch between SMART and FFT modes:

| Profile | What It Starts |
|---------|----------------|
| `smart` | Redis + 3 Node Agents + Dispatcher + Brain |
| `fft` | Redis + 3 Node Agents + FFT Scheduler |
| `tools` | Orchestrator (run once per experiment, then removed) |

To run SMART mode: `docker compose --profile smart up -d`
To run FFT mode: `docker compose --profile fft up -d`

### 5.3 Two-Machine Docker Compose Split

For the two-machine setup, the services are split across two compose files:

**PC 1** uses `docker-compose.pc1.yml`:
- `redis` — The central state store (port 6379, bound to 0.0.0.0)
- `dispatcher` — The O(1) heuristic (SMART mode)
- `brain` — The async ILP solver (SMART mode)
- `fft-scheduler` — The centralized scheduler (FFT mode)
- `orchestrator` — The test driver (tools profile)

**PC 2** uses `docker-compose.pc2.yml`:
- `agent-t4` — Manages 12 T4 GPU slots
- `agent-v100` — Manages 12 V100 GPU slots
- `agent-a10` — Manages 12 A10 GPU slots

PC 2's containers connect to Redis on PC 1 via the environment variable:
```yaml
EMU_REDIS_HOST: ${EMU_REDIS_HOST:-192.168.100.1}
```

### 5.4 Single-Machine Mode

For convenience, `docker-compose.yml` runs everything on one machine with 8 GPUs per type (24 total) instead of 12 per type (36 total). This is what `run_all_36.py` uses by default.

---

## 6. How Each Component Works

### 6.1 Redis (The Global Recorder — Component 5)

Redis is the **single source of truth** for the entire cluster. Every component reads from and writes to Redis. No component directly communicates with another — all communication goes through Redis.

**Redis Schema:**
| Key Pattern | Type | Purpose |
|-------------|------|---------|
| `arrivals` | LIST | Queue of arriving job IDs (pushed by trace_replayer) |
| `jobs:<id>` | HASH | Full job record: model, d, W, theta, progress, status, timestamps |
| `queue:global` | LIST | Slow-path queue (jobs waiting for the Brain's ILP) |
| `chan:queue_event` | LIST | Event trigger: wakes the Brain via BRPOP |
| `agent:<type>:req` | LIST | RPC request queue per GPU type (reserve/evict commands) |
| `resp:<uuid>` | LIST | RPC response channel (ACK or NACK from node agent) |
| `free:<type>` | STRING | Advertised free GPU slots per type (set by node agents) |
| `running:<type>` | SET | Job IDs currently executing on each GPU type |
| `cache:models` | SET | Historical cache of profiled model names |
| `metrics:events` | LIST | JSON telemetry log (every event from every component) |
| `total_jobs` | STRING | Total number of jobs in this run |
| `done_count` | STRING | Number of completed jobs (incremented by workers) |
| `shutdown` | STRING | Set to "1" to signal all components to stop |
| `run_t0` | STRING | Unix timestamp of run start (for time-round conversion) |

Redis has **no persistence** — `--save "" --appendonly no`. Every `docker compose down` wipes all state, ensuring a clean slate for the next run.

### 6.2 Trace Replayer (Component 1 — `trace_replayer.py`)

Generates a synthetic workload trace that statistically matches the Microsoft Philly trace (Poisson arrivals, log-normal processing times, variable worker counts). It replays job arrivals in **real time** — sleeping between arrivals according to the scaled arrival schedule.

Each job has:
- `model`: One of 7 DL models (ResNet50, VGG19, DenseNet, BERT-base, GPT-neo, GPT-2, OPT-6.7B)
- `d_j`: Number of GPUs requested (1–8)
- `W_j`: Number of epochs required (log-normal distributed)
- `theta`: Throughput per GPU type (epochs/round), computed from model × GPU speed
- `state_gb`: Checkpoint size (for migration cost calculation)

**6 Traffic Regimes:**
| Regime | Arrival Pattern | Model Mix |
|--------|----------------|-----------|
| `steady` | Near-uniform intervals | Mostly small models |
| `mixed` | Poisson (exponential inter-arrival) | Balanced mix |
| `bursty` | Clustered into 3+ bursts | More large models |
| `dynamic` | (generated via workload parameters) | Varied |
| `heavy` | (generated via workload parameters) | Varied |
| `random` | (generated via workload parameters) | Varied |

### 6.3 Node Agent (Component 7 — `node_agent.py`)

One agent process per GPU type (T4, V100, A10). Each agent:
- **Owns a slot table**: `self.free` tracks how many GPU slots are available. No other component can modify this directly.
- **Runs an RPC loop**: Listens on `agent:<type>:req` for `reserve` and `evict` commands.
  - `reserve`: Checks if `free >= d_j`. If yes, spawns a worker thread and ACKs. If no, NACKs. This is the **decentralized handshake**.
  - `evict`: Sets a preemption flag on a running worker. The worker checkpoints its progress and frees its slots.
- **Workers are threads**: Each worker sleeps in ticks (`TICK_REAL = 0.05s`), advancing progress at `theta` epochs per scaled round. When progress ≥ W, the job is done.
- **Auto-reset loop**: After receiving `shutdown`, the agent resets its slots and waits for the next Redis connection. This means **PC 2 never needs to be restarted between runs**.

### 6.4 Dispatcher (Component 2 — `dispatcher.py`, SMART mode only)

The O(1) heuristic that processes every arriving job instantly:
1. Pops a job ID from the `arrivals` queue.
2. Ranks all GPU types by training throughput for this job.
3. If any GPU has free slots, tries to place the job immediately (fast-path).
4. If all GPUs are full, pushes the job to `queue:global` for the Brain (slow-path).

### 6.5 Brain (Component 6 — `brain.py`, SMART mode only)

The asynchronous ILP optimizer that runs in the background:
1. Wakes up on a queue event OR every `BRAIN_INTERVAL_ROUNDS`.
2. Gathers all active jobs (queued + running).
3. Solves the FFT ILP to find optimal GPU assignments.
4. Places queued jobs and performs corrective migrations.
5. Runs Mechanism E (starvation prevention) for jobs that have never executed.
6. Drains the queue: if new jobs arrived during the solve, loops again (up to 3 times).

### 6.6 FFT Scheduler (`fft_scheduler_proc.py`, FFT mode only)

The baseline centralized scheduler:
1. Sleeps for exactly one scheduling round (5 minutes, scaled).
2. Drains all arrivals that occurred during the round.
3. Profiles every new job (adds a stall penalty).
4. Solves the FFT ILP over all active jobs.
5. Places/migrates jobs according to the solution.

**Key difference from SMART**: Jobs must WAIT for the round boundary before being scheduled. Decision latency = wait time + solve time ≈ 1500ms. In SMART mode, the dispatcher places jobs in ~1-4ms.

### 6.7 ILP Core (`ilp_core.py`)

The FFT paper's mathematical cost function, shared by both the Brain and the FFT scheduler:
- `phi_j^i(t)`: JCT priority term (paper Eq. 3)
- `rho_j(t)`: Fairness compensation (paper Eq. 5)
- `s_j^i(t)`: Switching penalty for changing GPU type
- Continuity reward for keeping a running job on its current GPU
- Work-conservation base reward (strong incentive to schedule any job)

### 6.8 Fast Solver (`fast_solver.py`)

Replaces the original CBC subprocess solver with an in-process SciPy HiGHS solver:
1. LP relaxation fast path: If the LP relaxation is already integral, return it immediately (proven optimal).
2. MILP fallback: If fractional, solve the true integer program with HiGHS branch-and-bound.

### 6.9 Orchestrator (`orchestrator.py`)

The test driver that runs inside a Docker container:
1. Waits for all node agents to register their slot tables in Redis.
2. Calls `trace_replayer.replay()` to start job arrivals.
3. Polls `done_count` until all jobs finish (or timeout).
4. Collects telemetry from `metrics:events` and computes final metrics.
5. Saves results to `runs/<mode>_<regime>_s<seed>.json`.
6. Sets `shutdown = 1` to stop all components.

---

## 7. Time Scaling

Real GPU training takes hours/days. The simulation compresses time by a factor of `TIME_SCALE = 100`:

| Physical Time | Scaled Time (100x) |
|---------------|---------------------|
| 1 scheduling round (5 min = 300s) | 3 seconds |
| 1 profiling window (60s) | 0.6 seconds |
| 1 OPT-6.7B migration (107GB at 1.25 GB/s = 85.6s) | 0.856 seconds |

All job progress, stalls, and scheduling rounds are computed in these scaled times. Measured decision latencies (ms) are **real wall-clock time** — NOT scaled.

---

## 8. The RPC Handshake (How Components Communicate)

The dispatcher/brain never directly modifies a node agent's slot table. Instead, they use a Redis-based RPC protocol:

```
Dispatcher/Brain                    Redis                         Node Agent
     │                                │                                │
     ├── LPUSH agent:V100:req ───────►│                                │
     │   {op: "reserve",              │                                │
     │    job: 42,                    │                                │
     │    resp: "resp:abc123"}        │                                │
     │                                │◄── BRPOP agent:V100:req ──────┤
     │                                │                                │
     │                                │    (Agent checks: free >= d?)  │
     │                                │                                │
     │                                │◄── LPUSH resp:abc123 ─────────┤
     │                                │    {ok: true}                  │
     │◄── BRPOP resp:abc123 ─────────┤                                │
     │                                │                                │
```

This means every placement decision crosses a **real network boundary** through Redis. In the two-machine setup, the RPC crosses the Cat 8 cable, adding realistic network latency.

---

## 9. The 36-Experiment Suite

### 9.1 What Is Being Tested

36 experiment runs = 2 modes × 6 regimes × 3 seeds:

| Dimension | Values |
|-----------|--------|
| Mode | `smart`, `fft` |
| Regime | `bursty`, `dynamic`, `heavy`, `mixed`, `random`, `steady` |
| Seed | `1`, `2`, `3` (different random traces) |

Each run simulates **60 jobs** being submitted to the 36-GPU cluster (or 24-GPU in single-machine mode).

### 9.2 How `run_all_36.py` Automates Everything

The master script on PC 1 automates the entire suite:

```
For each mode (smart, fft):
  For each regime (bursty, dynamic, heavy, mixed, random, steady):
    For each seed (1, 2, 3):
      1. Check if runs/<mode>_<regime>_s<seed>.json exists → skip if already done
      2. docker compose --profile <mode> up -d        → start Redis + scheduler containers
      3. sleep 3 seconds                               → wait for Redis health check
      4. docker compose --profile tools run --rm       → run the orchestrator (60 jobs, timeout 1800s)
         orchestrator 60 <regime> <seed> <mode> 1800
      5. docker compose --profile <mode> down           → tear down everything (clean Redis state)
      6. sleep 3 seconds                               → cooldown before next run
```

After all 36 runs, it automatically calls `python compare_all.py` to generate the comparison table.

### 9.3 How It Works in Two-Machine Mode

**On PC 2** (run ONCE at the start):
```powershell
cd "Option B"
docker compose -f docker-compose.pc2.yml up -d --build
docker compose -f docker-compose.pc2.yml logs -f     # optional: watch logs
```

The node agents on PC 2 start up and continuously wait for Redis to appear. When PC 1 starts a run, they connect, register their slots, process jobs, and when shutdown comes, they reset and wait for the next run.

**On PC 1** (run the suite):
```powershell
cd "Option B"
python run_all_36.py
```

The script uses `docker-compose.pc1.yml` in two-machine mode (or `docker-compose.yml` in single-machine mode).

### 9.4 What `run_all_36.py` Uses

In the current setup (single-machine, where the most recent 36 runs were executed), `run_all_36.py` uses `docker-compose.yml` which:
- Runs ALL containers on PC 1 (Redis + agents + scheduler)
- Uses `EMU_N_PER_TYPE=8` (24 GPUs total instead of 36)

To switch to two-machine mode, change `COMPOSE_FILE = "docker-compose.pc1.yml"` in `run_all_36.py` and set `EMU_N_PER_TYPE=12`.

---

## 10. Output Format

Each run produces a JSON file in `runs/`:

```json
{
  "n_jobs": 60,
  "n_finished": 60,
  "jct_mean_rounds": 25.197,
  "ftf_mean": 1.119,
  "starvation_mean_rounds": 0.188,
  "decision_latency_ms_mean": 1.508,
  "decision_latency_rounds_mean": 0.0005,
  "solve_wall_ms_mean": 6.780,
  "fast_path_frac": 0.994,
  "handshake_rejects": 18,
  "preemptions": 13,
  "migrations": 13,
  "profile_stalls": 0
}
```

**Metric Definitions:**
| Metric | Unit | Meaning |
|--------|------|---------|
| `jct_mean_rounds` | Scheduling rounds | Average time from job arrival to job completion |
| `ftf_mean` | Ratio (1.0 = perfect) | Finish-Time Fairness: actual JCT / ideal JCT (higher = less fair) |
| `starvation_mean_rounds` | Scheduling rounds | Average time from arrival to first execution on any GPU |
| `decision_latency_ms_mean` | Milliseconds (REAL) | Wall-clock time from arrival to scheduling decision |
| `fast_path_frac` | Fraction (0.0–1.0) | Percentage of jobs placed by the O(1) dispatcher (SMART only; FFT = 0.0) |
| `handshake_rejects` | Count | Number of NACK responses from node agents |
| `preemptions` | Count | Number of times a running job was evicted |
| `migrations` | Count | Number of times a job was moved between GPU types |
| `profile_stalls` | Count | Number of jobs that paid the profiling stall penalty |

---

## 11. How to Reproduce the Experiment

### Single-Machine Mode (Quickest)

```powershell
# 1. Navigate to project
cd "e:\Research testing V08 using Antigravity\Option B"

# 2. Build Docker images
docker compose build

# 3. Run all 36 experiments
python run_all_36.py

# 4. View results
python compare_all.py
```

### Two-Machine Mode (Research-Grade)

**PC 2 (run once):**
```powershell
# 1. Copy project files to PC 2
# 2. Configure static IP: 192.168.100.2
# 3. Build and start agents
cd "Option B"
docker compose -f docker-compose.pc2.yml up -d --build
```

**PC 1 (run suite):**
```powershell
# 1. Configure static IP: 192.168.100.1
# 2. Open firewall port 6379
New-NetFirewallRule -DisplayName "Allow-Redis-6379" -Direction Inbound -LocalPort 6379 -Protocol TCP -Action Allow

# 3. Verify connection
python test_connection.py 192.168.100.1

# 4. Edit run_all_36.py: change COMPOSE_FILE = "docker-compose.pc1.yml"
# 5. Run
python run_all_36.py
```

---

## 12. Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `EMU_REDIS_HOST` | `127.0.0.1` (config.py) / `redis` (compose) / `192.168.100.1` (PC2) | Redis server hostname |
| `EMU_REDIS_PORT` | `6379` | Redis server port |
| `EMU_TIME_SCALE` | `100` | Time compression factor (100x = 5-min round → 3 seconds) |
| `EMU_N_PER_TYPE` | `8` (single-machine) / `12` (two-machine) | Number of GPU slots per type |
| `PYTHONUNBUFFERED` | `1` | Forces Python to print output immediately (for Docker logs) |

---

## 13. Data Flow Diagram (One Complete Job Lifecycle)

```
1. trace_replayer.py generates a job and pushes it to Redis:
   LPUSH arrivals <job_id>
   HSET jobs:<id> {model, d, W, theta, state_gb, arrival_ts, progress=0, status="arrived"}

2a. SMART MODE — Dispatcher pops the job:
   BRPOP arrivals → gets job_id
   Reads job from Redis: HGETALL jobs:<id>
   Ranks GPU types by theta[g] → picks best
   Checks free slots: GET free:<type>
   If free >= d: sends RPC to node agent → ACK → job placed (fast-path)
   If not: LPUSH queue:global <job_id> → job waits for Brain (slow-path)

2b. FFT MODE — FFT Scheduler sleeps for one round, then:
   RPOP arrivals → drains all arrivals
   Solves ILP over all active jobs
   Places/migrates via RPCs to node agents

3. Node Agent receives RPC:
   BRPOP agent:<type>:req → gets {op: "reserve", job: <id>}
   Checks self.free >= d → ACK or NACK
   If ACK: spawns worker thread, decrements free slots

4. Worker thread runs:
   Sleeps in 0.05s ticks, advancing progress += theta * dt_rounds
   If preempt flag set: checkpoints progress, frees slots, requeues job
   If progress >= W: marks job "done", increments done_count

5. Orchestrator watches:
   Polls done_count until == total_jobs
   Collects metrics from metrics:events
   Saves to runs/<mode>_<regime>_s<seed>.json
   Sets shutdown = 1
```

---

## 14. Key Design Decisions

1. **Why Redis?** Redis acts as the "Global Recorder" (Component 5 in the architecture). It provides atomic operations, pub/sub-like patterns via BRPOP, and sub-millisecond latency. It also naturally separates components — no component needs to know another's address.

2. **Why Docker?** Docker containers provide real network namespaces. Every RPC crosses a network boundary, making latency measurements realistic. Docker also ensures reproducibility across machines.

3. **Why Cat 8?** Cat 8 provides up to 40 Gbps bandwidth and < 1ms latency. This simulates a high-speed datacenter interconnect between nodes, matching the FFT paper's 10 Gbps testbed assumption.

4. **Why 100x time scaling?** At 100x, a 5-minute scheduling round becomes 3 seconds. This allows a full 36-experiment suite to complete in ~6-7 hours instead of ~250 hours. The scaling factor cannot go below ~50x because real Redis/solver latencies (tens of ms) would distort the simulated physics.

5. **Why 3 seeds per regime?** Three random seeds provide statistical confidence. Results are averaged across seeds in `compare_all.py` to reduce noise from random trace variation.
