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
