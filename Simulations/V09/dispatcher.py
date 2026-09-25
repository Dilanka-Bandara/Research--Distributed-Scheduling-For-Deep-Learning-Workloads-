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
