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
