# Two-Machine Run Guide — V09 Dynamic-Arrival Experiment

Every step is marked **[PC 1]**, **[PC 2]** or **[BOTH]**. Follow them in order.

> **Correction to earlier guides.** Earlier guides told you to run
> `docker compose up -d --build` on PC 2. That command uses
> `docker-compose.yml`, the all-in-one single-machine stack, which brings up
> its own Redis on PC 2. On PC 2, **always** use
> `-f docker-compose.pc2.yml`, as shown below.

---

## 0. What runs where

| | PC 1 — Controller `192.168.100.1` | PC 2 — Compute `192.168.100.2` |
|---|---|---|
| Containers | Redis, dispatcher + brain (SMART) **or** FFT scheduler, probe, orchestrator | 3 node agents: T4, V100, A10 × 12 slots |
| Compose file | `docker-compose.pc1.yml` + `docker-compose.pc1.dynamic.yml` | `docker-compose.pc2.yml` |
| Lifecycle | Started and stopped **for every run** by `run_dynamic.py` | Started **once**; reconnects automatically between runs |
| You type commands here | Almost always | Only for setup and watching logs |
| Clock | The Redis clock **is** the cluster clock | Agents measure their offset against PC 1 and correct for it |

---

## 1. One-time setup

Skip anything you've already done, but check the **power settings** — they
matter more now.

### 1.1 Network [BOTH]

| | PC 1 | PC 2 |
|---|---|---|
| IP address | `192.168.100.1` | `192.168.100.2` |
| Subnet mask | `255.255.255.0` | `255.255.255.0` |
| Gateway | leave empty | leave empty |

```powershell
# on PC 2 — must be under 1 ms
ping 192.168.100.1
```

### 1.2 Firewall [PC 1]

PowerShell as Administrator:

```powershell
New-NetFirewallRule -DisplayName "Allow-Redis-6379" -Direction Inbound `
    -LocalPort 6379 -Protocol TCP -Action Allow
```

PC 2 only makes outbound connections, so it needs no firewall rule.

### 1.3 Shared folder [PC 2]

`sync_to_pc2.ps1` copies code to PC 2 over a Windows share. Share PC 2's
project folder (right-click → Properties → Sharing) and give PC 1's user
read/write access. From PC 1, this should open:

```powershell
Test-Path "\\192.168.100.2\<ShareName>\Option B"
```

### 1.4 Power and updates [BOTH] — important

A Tier 1 session lasts about 3 hours. Sleep does two kinds of damage: it kills
runs, and it makes the WSL2 VM clock (which Docker containers use) jump.

- Settings → System → Power: **Sleep = Never** while plugged in, on **both** PCs.
- Keep laptops plugged in.
- Settings → Windows Update → **Pause updates** for the session.
- Docker Desktop → Settings → General: **Start Docker Desktop when you sign in**.
- Don't do heavy work on PC 1 during runs. CPU contention distorts
  scheduling latencies.

---

## 2. Install V09

### 2.1 Back up V08 [PC 1]

```powershell
cd "E:\Research--Distributed-Scheduling-For-Deep-Learning-Workloads-\Simulations"
Copy-Item -Recurse "Option B" "Option B - V08 backup"
cd "Option B"
```

### 2.2 Copy the V09 files in [PC 1]

Copy **every file** from the `v09` folder into `Option B`, overwriting
existing ones:

```
Dockerfile               common.py            node_agent.py
brain.py                 dispatcher.py        fft_scheduler_proc.py
orchestrator.py          dynamic_metrics.py   arrival_process.py
workload.py              trace_replayer.py    probe.py
run_dynamic.py           compare_dynamic_all.py
calibrate_load.py        dryrun_dynamic.py    check_run.py
preflight.py             preflight.ps1        docker-compose.pc1.dynamic.yml
```

**Keep your own** `config.py`, `ilp_core.py`, `fast_solver.py`,
`analyze_results.py`, `docker-compose.pc1.yml`, `docker-compose.pc2.yml` and
`.env`. V09 doesn't replace them.

The `evidence/` folder is for your records and thesis only. Don't copy it into
`Option B`.

### 2.3 Label the machines [PC 1 — edit BOTH compose files here]

Make both edits **on PC 1**. The sync in step 3 uses `robocopy /MIR`, which
would overwrite any edit you made directly on PC 2.

In `docker-compose.pc1.yml`, add one line to the `x-common-env` block:

```yaml
x-common-env: &env
  EMU_REDIS_HOST: redis
  EMU_REDIS_PORT: 6379
  EMU_TIME_SCALE: ${EMU_TIME_SCALE:-100}
  EMU_N_PER_TYPE: ${EMU_N_PER_TYPE:-12}
  EMU_NODE_LABEL: "PC1"          # <-- add
  PYTHONUNBUFFERED: "1"
```

In `docker-compose.pc2.yml`, the same place:

```yaml
x-common-env: &env
  EMU_REDIS_HOST: "192.168.100.1"
  EMU_REDIS_PORT: 6379
  EMU_TIME_SCALE: ${EMU_TIME_SCALE:-100}
  EMU_N_PER_TYPE: ${EMU_N_PER_TYPE:-12}
  EMU_NODE_LABEL: "PC2"          # <-- add
  PYTHONUNBUFFERED: "1"
```

This makes result files show `PC2:T4:<code>` instead of a container ID, which
is proof that the agents really ran on the second machine.

### 2.4 Check the `.env` files [BOTH]

Each PC has its own `.env`. It isn't synced, by design.

```
# PC 1 .env                    # PC 2 .env
EMU_N_PER_TYPE=12              EMU_N_PER_TYPE=12
EMU_TIME_SCALE=100             EMU_TIME_SCALE=100
EMU_REDIS_HOST=redis           EMU_REDIS_HOST=192.168.100.1
EMU_REDIS_PORT=6379            EMU_REDIS_PORT=6379
```

### 2.5 Host Python [PC 1]

`run_dynamic.py`, `check_run.py`, `compare_dynamic_all.py` and
`calibrate_load.py` run directly on PC 1's Windows Python, not in Docker. They
use only the standard library.

```powershell
python --version        # 3.10 or newer
```

---

## 3. Sync the code to PC 2 [PC 1]

```powershell
.\sync_to_pc2.ps1 -Destination "\\192.168.100.2\<ShareName>\Option B"
```

The script mirrors every file, including the Dockerfile and both compose files,
and skips `.env` and `runs/`.

Now confirm both PCs hold identical code. Run the command below **on each PC**
in the `Option B` folder. The two outputs must be the same 12 characters.

```powershell
python -c "import glob,hashlib,os;h=hashlib.md5();[(h.update(os.path.basename(f).encode()),h.update(open(f,'rb').read())) for f in sorted(glob.glob('*.py'))];print(h.hexdigest()[:12])"
```

This is the same fingerprint the agents report, so a mismatch here means
`check_run.py` would reject every run.

---

## 4. Build and start

The Dockerfile changed (library versions are now pinned) and all the code
changed, so both images must be rebuilt **without cache**.

### 4.1 Start the agents [PC 2]

```powershell
cd "<path>\Option B"
docker compose -f docker-compose.pc2.yml down
docker compose -f docker-compose.pc2.yml build --no-cache
docker compose -f docker-compose.pc2.yml up -d
docker compose -f docker-compose.pc2.yml logs -f
```

Until PC 1's Redis is up, each agent prints `Waiting for Redis ...`. That's
normal. **Leave this log window open** for the whole session.

### 4.2 Build the controller [PC 1]

Every PC 1 service belongs to a profile, so a plain `build` builds nothing.
Name all the profiles:

```powershell
docker compose -f docker-compose.pc1.yml -f docker-compose.pc1.dynamic.yml `
    --profile smart --profile fft --profile tools build --no-cache
```

> **Rule for the rest of the project:** whenever you change a `.py` file,
> repeat steps 3, 4.1 (the `build` and `up` lines) and 4.2. The code is baked
> into the images, so containers keep running the old code until you rebuild.

---

## 5. Preflight [PC 1]

```powershell
.\preflight.ps1
```

It starts Redis briefly, waits for PC 2's agents to connect, and checks
everything. You need **all** of these lines:

```
[+] Redis reachable on PC 1
[+] Agent T4 registered 12 slots.
[+] Agent V100 registered 12 slots.
[+] Agent A10 registered 12 slots.
[+] Cross-machine RPC RTT: x.xx ms
    PC2:...   offset   +NNN.N ms   rtt ...
[+] V09 corrects these offsets automatically. Largest: NNN.N ms
[+] PC 2 agents run the same code as PC 1 (xxxxxxxxxxxx)
ALL CHECKS PASSED.
```

**Write down the offset value.** It is how far apart the two machines' clocks
are, which is how wrong V08's two-machine timestamps were. It belongs in the
thesis. V09 corrects it automatically, so there's nothing to fix.

In PC 2's log window you should see each agent print
`Ready (epoch N) ... clock offset ... ms` and, when preflight stops Redis,
`Redis disconnected ... Waiting for next stack`. That is correct behaviour.

---

## 6. Smoke test — one short paired run [PC 1]

This proves the full two-machine pipeline works before you commit three hours.

```powershell
$env:EMU_N_PER_TYPE="12"
$env:EMU_TIME_SCALE="100"
python run_dynamic.py --regime dyn_flash --seeds 1 --jobs 40
```

This takes about 10–15 minutes for both schedulers. Then:

```powershell
python check_run.py --expect-pc2
```

Every line must say `ok`, and each file must end in `PASS`:

```
runs/smart_dyn_flash_s1.json   [smart | dyn_flash | seed 1 | 40 jobs]   -> PASS
  ok    all jobs finished (40/40)
  ok    scheduler never stalled (gaps>2 rounds: 0, longest 0.36 rounds)
  ok    no RPCs to a missing agent (0)
  ok    no RPC timeouts (0)
  ok    adaptability metrics computed
  ok    PC 1 and agents run identical code (code_match=True)
  ok    three agents registered (3)
  ok    36-GPU cluster (N_PER_TYPE=12)
  ok    TIME_SCALE=100 (100)
  ok    agents ran on PC 2 ([...'PC2:T4:...'])
  info  clock offsets vs Redis clock (ms): [0, NNN]
```

The SMART scheduler's longest gap should be around 0.3–0.4 rounds, and FFT's
around 1.0 (its round length).

**If anything fails, stop.** Send me the failing JSON together with its
`runs\raw\<same name>.events.json.gz`.

**If everything passes,** delete the smoke-test files. They're 40-job runs and
don't belong in the 100-job results:

```powershell
Remove-Item runs\*_dyn_flash_s1.json, runs\raw\*_dyn_flash_s1.*
```

---

## 7. Tier 1 — the real runs [PC 1]

```powershell
$env:EMU_N_PER_TYPE="12"
$env:EMU_TIME_SCALE="100"
python run_dynamic.py --dry-run          # read the plan and time estimate
python run_dynamic.py --tier 1           # 12 runs: dyn_step + dyn_flash,
                                         # 3 seeds, FFT and SMART. ~3 hours.
```

`$env:` settings only last for the current PowerShell window. Set them again
in every new window.

**While it runs:**

| Where | What you should see |
|---|---|
| PC 1 window | `[k/12] SMART \| dyn_step \| seed 1` → orchestrator output → `[+] ... OK` |
| PC 2 log window | At each run start: `Ready (epoch N)`. At each end: `Run over. Workers retired`. |
| Either | **No** `WARNING: scheduler stalled` and **no** `CODE MISMATCH` |

**If it's interrupted** (Ctrl-C, crash, power cut): run the same command
again. It skips valid completed runs and redoes everything else, including any
run the watchdog rejected.

**Tier 2** is optional, a further ~3 hours:

```powershell
python run_dynamic.py --tier 2           # dyn_sine + dyn_mmpp
```

---

## 8. After the runs [PC 1]

```powershell
python check_run.py --expect-pc2                                  # all PASS
python compare_dynamic_all.py --verbose --csv results_dynamic_v09.csv
```

Only cells marked `[CONFIRMED]` in the comparison table are quotable.

Archive the session so it's reproducible:

```powershell
$stamp = Get-Date -Format "yyyyMMdd_HHmm"
Compress-Archive -Path runs, *.py, Dockerfile, *.yml `
    -DestinationPath "..\V09_tier1_$stamp.zip"
```

**Send me:** `results_dynamic_v09.csv`, the `check_run.py` output, and the
`runs` folder (zipped, including `runs\raw`). I'll turn it into the thesis
tables and check the numbers against the single-machine V09 validation.

### Recommended: re-run Phase 1 on V09

Phase 1 ran on V08, whose brain froze. Re-run the three genuinely distinct
static regimes on V09 so both phases use the same code: `steady`, `mixed`,
`bursty` × 3 seeds × 2 schedulers = 18 runs. (`dynamic`, `heavy` and `random`
were copies of `mixed`.) Tell me if you want `run_all_36.py` adapted for this.

---

## 9. Every-session checklist

```
[ ] Both PCs: plugged in, sleep off, updates paused
[ ] Cat 8 connected; ping 192.168.100.1 from PC 2 < 1 ms
[ ] Code changed since last session?  -> sync (3) + rebuild both (4)
[ ] PC 2: docker compose -f docker-compose.pc2.yml up -d ; logs -f open
[ ] PC 1: .\preflight.ps1 -> ALL CHECKS PASSED
[ ] PC 1: $env:EMU_N_PER_TYPE="12"; $env:EMU_TIME_SCALE="100"
[ ] PC 1: python run_dynamic.py --tier 1
[ ] PC 1: python check_run.py --expect-pc2 -> all PASS
```

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| PC 2 stays on `Waiting for Redis` during a run | Firewall, cable, or wrong IP | 1.1, 1.2; run `python test_connection.py 192.168.100.1` on PC 2 |
| Preflight: `Only 0/3 agents connected` | PC 2 agents not running, or started with the wrong compose file | 4.1 — note the `-f docker-compose.pc2.yml` |
| Agents register `8` slots | `EMU_N_PER_TYPE` missing from PC 2's `.env` | Fix `.env` on PC 2, then `down` and `up -d` |
| `CODE MISMATCH` / `code_match=False` | PC 2 has different code, or its image wasn't rebuilt | Sync (3), then rebuild PC 2 with `--no-cache` (4.1) |
| Preflight: `No agent clock records found` | PC 2 is still running V08 | Same as above |
| `WARNING: scheduler stalled` | Scheduler blocked for > 2 rounds — V09 shouldn't do this | Don't quote the run. Send me its JSON + `runs\raw` archive |
| `rpc_timeouts` > 0 | An agent stopped answering: PC 2 overloaded, asleep, or the link dropped | Check the PC 2 log window and power settings; re-run |
| Run hits `TIMEOUT` | Calibration too heavy, or PC 2 stopped mid-run | Check PC 2 logs; rerunning the command resumes |
| `run_dynamic.py` warns `N_PER_TYPE is not 12` | `$env:` not set in this window | Set it (step 7) |
| Code changes don't take effect | Images not rebuilt | 4.1 + 4.2 |
| `preflight.ps1` errors inside `orchestrator.py` | You're using the V08 `preflight.ps1` | Use the V09 one (fixed `--entrypoint`) |
