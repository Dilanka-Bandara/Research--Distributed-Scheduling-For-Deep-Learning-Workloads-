"""
dispatcher.py — component 2, the O(1) Heuristic Scoring Dispatcher (smart mode).

This is where your research gap becomes a stopwatch measurement: for every
arrival we record the REAL wall-clock time from popping the job to the routing
decision. It is O(1) — a couple of Redis reads plus arithmetic — versus the
FFT process's round-gated solve. Fast placements go through the agent's
reserve RPC (real handshake); a NACK falls back to the Slow Path.
"""
from __future__ import annotations
import time
from common import R, now, emit, load_job, update_job, rpc, safe_brpop
from config import (ALPHA, BETA, GAMMA, DELTA, THRESHOLD, SLOW_RESERVE,
                    RHO_AGE_RATE, JCT_AWARE_FAST, PROFILE_SECONDS,
                    ROUND_SECONDS, N_PER_TYPE, real_to_rounds)
from workload import GPU_TYPES, GPU_CAPABILITY

D_MAX, W_MAX = 8.0, 40.0
CAP_MAX = max(GPU_CAPABILITY.values())

def s_job(job, wait_rounds):
    return (ALPHA * job["d"] / D_MAX
            + BETA * min(1.0, job["W"] / W_MAX)
            + RHO_AGE_RATE * (wait_rounds / 50.0))

def s_node(g, free, cap_total):
    return (GAMMA * GPU_CAPABILITY[g] / CAP_MAX
            + DELTA * free / max(1, cap_total)) / (GAMMA + DELTA)

def run():
    r = R()
    total_cap = {g: N_PER_TYPE for g in GPU_TYPES}
    cluster_total = sum(total_cap.values())
    fast_used = 0
    emit(r, "dispatcher_up")
    while r.get("shutdown") != "1":
        got = safe_brpop(r, "arrivals", 1)
        if not got: continue
        jid = int(got[1])
        t_pop = now()
        job = load_job(r, jid)

        # ---------- O(1) scoring & routing (the measured decision) ----------
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
        fast_cap = int((1.0 - SLOW_RESERVE) * cluster_total)
        go_fast = bool(cands) and fast_used + job["d"] <= fast_cap
        if go_fast and JCT_AWARE_FAST:
            target = max(cands)[1]          # fastest-finishing type (Eq. 3)
        elif go_fast:
            target = best_g
        decide_ts = now()
        update_job(r, jid, decide_ts=decide_ts)
        emit(r, "decision", job=jid, latency=decide_ts - t_pop,
             route="fast" if go_fast else "slow")

        cache_hit = r.sismember("cache:models", job["model"])
        r.sadd("cache:models", job["model"])
        update_job(r, jid, cache_hit=bool(cache_hit))

        if go_fast:
            # real handshake: agent verifies its own slot table
            ack = rpc(r, target, "reserve", {"job": jid, "stall_rounds": 0.0})
            if ack.get("ok"):
                fast_used += job["d"]
                update_job(r, jid, route="fast")
                emit(r, "placed", job=jid, gpu=target, how="fast")
                continue
            # NACK -> fall through to slow path
        stall = 0.0 if cache_hit else PROFILE_SECONDS / ROUND_SECONDS
        if stall: emit(r, "profile_stall", job=jid, rounds=stall)
        update_job(r, jid, route="slow", pending_stall=stall)
        r.lpush("queue:global", jid)
        r.lpush("chan:queue_event", "1")     # event trigger -> wake the brain

if __name__ == "__main__":
    run()
