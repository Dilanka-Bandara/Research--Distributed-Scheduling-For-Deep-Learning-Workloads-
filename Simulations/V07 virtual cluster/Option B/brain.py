"""
brain.py — component 6, the Asynchronous Global FFT Scheduler (smart mode).

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

        t_solve = now()
        alloc = solve(jobs, cap, t_rounds, fair)
        solve_wall = now() - t_solve
        emit(r, "brain_solve", n=len(jobs), wall=solve_wall)

        queued_ids = {int(x) for x in r.lrange("queue:global", 0, -1)}
        migrated = 0
        for j in jobs:
            jid = j["id"]; target = alloc.get(jid)
            if target is None or j.get("gpu") == target: continue
            if jid in queued_ids:
                stall = j.get("pending_stall", 0.0) or 0.0
                ack = rpc(r, target, "reserve",
                          {"job": jid, "stall_rounds": stall,
                           "migrate_from": j.get("gpu")})
                if ack.get("ok"):
                    r.lrem("queue:global", 0, str(jid))
                    update_job(r, jid, pending_stall=0.0)
                    emit(r, "placed", job=jid, gpu=target, how="brain")
                # NACK already logged by the agent as handshake_reject
            else:
                # corrective migration: CHECK TARGET CAPACITY FIRST (matches the
                # simulation's semantics — never evict into a full target), and
                # cap migrations per wake to prevent churn storms under racing.
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

        # ---------------- Mechanism E ----------------
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
                ack = rpc(r, g, "reserve",
                          {"job": j["id"], "stall_rounds": j.get("pending_stall", 0.0) or 0.0})
                if ack.get("ok"):
                    r.lrem("queue:global", 0, str(j["id"]))
                    emit(r, "placed", job=j["id"], gpu=g, how="mechE")
                    evicted += 1
                break

if __name__ == "__main__":
    run()
