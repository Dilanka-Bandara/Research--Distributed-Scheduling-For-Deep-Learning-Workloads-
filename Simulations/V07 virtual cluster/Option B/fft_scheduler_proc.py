"""
fft_scheduler_proc.py — the FFT baseline as a real process (fft mode).

Centralised and synchronous, per the paper (Sec. 4.1): allocations are decided
only at round boundaries. An arriving job WAITS for the next round's ILP solve
— its measured admission latency = (solve completion) - (arrival), which is
exactly the centralised bottleneck your research gap names. Every new job pays
the on-the-fly profiling stall (Sec. 5). Migrations decided by the solve incur
the real (scaled) state-transfer stall via the agents.
"""
from __future__ import annotations
import time
from common import R, now, emit, load_job, update_job, rpc
from config import (ROUND_SECONDS, PROFILE_SECONDS, N_PER_TYPE,
                    rounds_to_real, real_to_rounds)
from workload import GPU_TYPES
from ilp_core import FairnessState, solve

def run():
    r = R()
    fair = FairnessState(mu=1.0)
    cap = {g: N_PER_TYPE for g in GPU_TYPES}
    t0 = float(r.get("run_t0") or now())
    known: dict[int, bool] = {}
    emit(r, "fft_up")
    while r.get("shutdown") != "1":
        time.sleep(rounds_to_real(1.0))              # the round gate
        # drain arrivals that occurred during the round
        while True:
            got = r.rpop("arrivals")
            if got is None: break
            jid = int(got)
            known[jid] = False
            update_job(r, jid, pending_stall=PROFILE_SECONDS / ROUND_SECONDS,
                       route="fft")
            emit(r, "profile_stall", job=jid, rounds=PROFILE_SECONDS / ROUND_SECONDS)
        # active set
        jobs = []
        for jid in list(known):
            j = load_job(r, jid)
            if j and j["status"] != "done": jobs.append(j)
            elif j and j["status"] == "done": known.pop(jid, None)
        if not jobs: continue

        t_rounds = real_to_rounds(now() - t0)
        t_solve = now()
        alloc = solve(jobs, cap, t_rounds, fair)
        solve_end = now()
        emit(r, "fft_solve", n=len(jobs), wall=solve_end - t_solve)

        for j in jobs:
            jid = j["id"]
            if not known.get(jid):
                known[jid] = True
                update_job(r, jid, decide_ts=solve_end)
                emit(r, "decision", job=jid,
                     latency=solve_end - j["arrival_ts"], route="fft")
            target = alloc.get(jid)
            if target is None or j.get("gpu") == target: continue
            if j.get("gpu") is None:
                ack = rpc(r, target, "reserve",
                          {"job": jid, "stall_rounds": j.get("pending_stall", 0.0) or 0.0})
                if ack.get("ok"):
                    update_job(r, jid, pending_stall=0.0)
                    emit(r, "placed", job=jid, gpu=target, how="fft")
            else:
                src = j["gpu"]
                if int(r.get(f"free:{target}") or 0) < j["d"]:
                    continue
                if rpc(r, src, "evict", {"job": jid}).get("ok"):
                    time.sleep(0.15)
                    ack = rpc(r, target, "reserve",
                              {"job": jid, "migrate_from": src})
                    if ack.get("ok"):
                        r.lrem("queue:global", 0, str(jid))
                        emit(r, "placed", job=jid, gpu=target, how="fft_migrate")

if __name__ == "__main__":
    run()
