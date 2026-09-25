"""trace_replayer.py — component 1. Replays the workload trace in REAL time.

Reuses the exact Philly-statistics generator from scheduler_simulation_v2, so
the emulation and the discrete-event simulation can consume the SAME trace
(same seed/regime/jobs) — that is what enables the emulation-vs-simulation
validation figure (the FFT paper validated its simulator against its physical
testbed the same way, <=4.9% JCT deviation).

DYNAMIC-PHASE CHANGES (two, both backwards-compatible):

1. HORIZON_PER_JOB is now configurable. It was hard-coded at 1.4, which made
   the offered load rho INDEPENDENT of n_jobs (horizon scaled with job count,
   so the two cancelled) and left no way to calibrate utilisation at all.
   The default is still 1.4, so every existing regime replays byte-identically.

2. The arrival plan for a dynamic regime is published to Redis under
   `trace_plan`, so the analyser can window the run into phases without having
   to re-derive them. It is written BEFORE the first arrival so a crashed run
   still records what it was trying to do.
"""
from __future__ import annotations

import json
import os
import sys
import time

from common import R, now, save_job, emit
from config import rounds_to_real
from workload import generate_trace, profile_job

# Stock behaviour is 1.4; calibrate_load.py solves for the right value per
# regime. Set EMU_HORIZON_PER_JOB on PC 1 only — PC 2's agents never see the
# trace and do not need it.
HORIZON_PER_JOB = float(os.environ.get("EMU_HORIZON_PER_JOB", "1.4"))


def trace_horizon(n_jobs: int) -> float:
    """Single source of truth — the analyser calls this too, so the phase
    boundaries it computes are guaranteed to match the ones replayed."""
    return max(60.0, n_jobs * HORIZON_PER_JOB)


def replay(n_jobs: int, regime: str, seed: int):
    r = R()
    horizon = trace_horizon(n_jobs)
    trace = generate_trace(n_jobs=n_jobs, regime=regime, seed=seed,
                           horizon=horizon)
    for j in trace:
        profile_job(j)             # theta known to the SYSTEM's profiler model

    # Publish the arrival plan for dynamic regimes (no-op for the stationary
    # ones, which have no phases).
    try:
        from arrival_process import DYNAMIC_REGIMES, build_plan
        if regime in DYNAMIC_REGIMES:
            r.set("trace_plan", json.dumps(build_plan(regime, seed, horizon).to_dict()))
    except ImportError:
        pass

    r.set("total_jobs", len(trace))
    r.set("trace_horizon", horizon)
    t0 = now(); r.set("run_t0", t0)
    emit(r, "run_start", n_jobs=len(trace), regime=regime, seed=seed,
         horizon=horizon)
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
