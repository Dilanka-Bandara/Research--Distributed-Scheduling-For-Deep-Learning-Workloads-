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
