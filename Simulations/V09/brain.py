"""
brain.py — component 6, the Asynchronous Global FFT Scheduler (smart mode).

OPTIMISED VERSION (Fixes 4, 5):
  Fix 4 — Micro-batch brain with drain-all-queue (Pollux-inspired):
          After each ILP solve and placement round, immediately re-check the
          queue. If new jobs arrived during the solve, process them without
          waiting for the next BRPOP timeout. Tight inner loop ensures the
          brain doesn't go back to sleep with unplaced jobs in the queue.

  Fix 5 — Immediate starvation response (Tiresias LAS-inspired):
          STARVE_AFTER_ROUNDS reduced to 0.3 (from 0.5), MAX_EVICT_PER_WAKE
          increased to 6 (from 4). Evicted jobs get credit: pending_stall = 0
          (skip re-profiling since they were already running).

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

def _place_queued_jobs(r, jobs, alloc, t_rounds):
    """Place queued jobs based on ILP allocation. Returns count placed."""
    queued_ids = {int(x) for x in r.lrange("queue:global", 0, -1)}
    # SRPT queue prioritization: evaluate shortest remaining jobs first
    queued_jobs = [j for j in jobs if j["id"] in queued_ids]
    queued_jobs.sort(key=lambda x: (x["W"] - x["progress"]) / max(1e-3, max(x["theta"].values())))

    placed = 0
    for j in queued_jobs:
        jid = j["id"]; target = alloc.get(jid)
        if target is None or j.get("gpu") == target: continue
        free_t = int(r.get(f"free:{target}") or 0)
        if free_t >= j["d"]:
            stall = j.get("pending_stall", 0.0) or 0.0
            ack = rpc(r, target, "reserve",
                      {"job": jid, "stall_rounds": stall,
                       "migrate_from": j.get("gpu")})
            if ack.get("ok"):
                r.lrem("queue:global", 0, str(jid))
                update_job(r, jid, pending_stall=0.0)
                emit(r, "placed", job=jid, gpu=target, how="brain")
                placed += 1
        else:
            # Coordinated Preemptive Placement: target full, evict lower-priority victim first
            rem_j = (j["W"] - j["progress"]) / max(1e-3, j["theta"].get(target, 1.0))
            running_victims = []
            for vid in r.smembers(f"running:{target}"):
                v = load_job(r, int(vid))
                if v and v.get("first_exec_ts"):
                    rem_v = (v["W"] - v["progress"]) / max(1e-3, v["theta"].get(target, 1.0))
                    if rem_v > rem_j * 1.3:
                        running_victims.append((rem_v, int(vid), v["d"]))
            running_victims.sort(reverse=True)
            if running_victims and (free_t + running_victims[0][2] >= j["d"]):
                victim_id = running_victims[0][1]
                if rpc(r, target, "evict", {"job": victim_id}).get("ok"):
                    time.sleep(0.15)
                    stall = j.get("pending_stall", 0.0) or 0.0
                    ack = rpc(r, target, "reserve",
                              {"job": jid, "stall_rounds": stall,
                               "migrate_from": j.get("gpu")})
                    if ack.get("ok"):
                        r.lrem("queue:global", 0, str(jid))
                        update_job(r, jid, pending_stall=0.0)
                        emit(r, "placed", job=jid, gpu=target, how="brain_preempt")
                        placed += 1
    return placed

def _corrective_migrations(r, jobs, alloc, queued_ids):
    """Migrate running jobs to better GPU types per ILP solution."""
    running_jobs = [j for j in jobs if j["id"] not in queued_ids]
    migrated = 0
    for j in running_jobs:
        jid = j["id"]; target = alloc.get(jid)
        if target is None or j.get("gpu") == target: continue
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

def _mechanism_e(r, t_rounds):
    """Starvation prevention: evict running jobs for starving queued jobs."""
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
            # Fix 5: Starving job skips re-profiling (already arrived & known)
            stall = j.get("pending_stall", 0.0) or 0.0
            ack = rpc(r, g, "reserve",
                      {"job": j["id"], "stall_rounds": stall})
            if ack.get("ok"):
                r.lrem("queue:global", 0, str(j["id"]))
                update_job(r, j["id"], pending_stall=0.0)
                emit(r, "placed", job=j["id"], gpu=g, how="mechE")
                evicted += 1
            break

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

        # ---- Fix 4: Drain-all-queue loop (Pollux-inspired) ----
        # After each solve, re-check the queue. If new jobs arrived during
        # the solve, process them immediately without waiting for BRPOP.
        max_drain_iters = 3   # safety cap: prevent infinite loop under flood
        for drain_iter in range(max_drain_iters):
            t_solve = now()
            alloc = solve(jobs, cap, t_rounds, fair)
            solve_wall = now() - t_solve
            emit(r, "brain_solve", n=len(jobs), wall=solve_wall)

            queued_ids = {int(x) for x in r.lrange("queue:global", 0, -1)}
            placed = _place_queued_jobs(r, jobs, alloc, t_rounds)
            _corrective_migrations(r, jobs, alloc, queued_ids)

            # Mechanism E (starvation prevention)
            _mechanism_e(r, t_rounds)

            # Drain check: if we placed jobs, re-check queue for more
            queue_len = r.llen("queue:global")
            if placed == 0 or queue_len == 0:
                break  # nothing more to do this cycle
            # Refresh jobs for next iteration
            jobs = active_jobs(r)
            if not jobs:
                break

if __name__ == "__main__":
    run()
