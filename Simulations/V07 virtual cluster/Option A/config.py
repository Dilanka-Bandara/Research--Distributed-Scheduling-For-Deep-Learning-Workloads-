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

# --- architecture parameters (latest defaults, same as scheduler_simulation_v2) ---
ALPHA, BETA, GAMMA, DELTA = 1.0, 0.5, 1.0, 1.0
THRESHOLD = 0.15
SLOW_RESERVE = 0.25
RHO_AGE_RATE = 0.3
STARVE_AFTER_ROUNDS = 1.0      # Mechanism E trigger (tuned; re-sweep at your scale)
MAX_EVICT_PER_WAKE = 4
JCT_AWARE_FAST = True
BRAIN_INTERVAL_ROUNDS = 2.0    # hybrid trigger: interval OR queue event

N_PER_TYPE = int(os.environ.get("EMU_N_PER_TYPE", "8"))

def scaled(seconds_physical: float) -> float:
    """Physical seconds -> real (wall-clock) seconds under TIME_SCALE."""
    return seconds_physical / TIME_SCALE

def rounds_to_real(rounds: float) -> float:
    return scaled(rounds * ROUND_SECONDS)

def real_to_rounds(real_dt: float) -> float:
    return real_dt * TIME_SCALE / ROUND_SECONDS
