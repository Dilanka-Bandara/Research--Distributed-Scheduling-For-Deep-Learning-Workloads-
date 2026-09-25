"""
dryrun_dynamic.py — validate the dynamic-arrival pipeline WITHOUT the cluster.

Runs a deliberately crude analytic model of the two schedulers over a real
dynamic trace, synthesises the same telemetry the emulation emits, and pushes it
through `dynamic_metrics`. Purpose:

  1. prove the metric code runs end-to-end and produces sane numbers,
  2. confirm the metrics can DISTINGUISH round-gated from event-driven
     admission before you spend hours of two-PC cluster time,
  3. give you an expected DIRECTION for each metric, so a real run that
     contradicts it is a red flag worth investigating rather than a result.

This is NOT a result. Its numbers are not quotable and must never appear in the
thesis: the capacity model here ignores migration, preemption, fairness and the
ILP entirely. It is a wiring test.

    python dryrun_dynamic.py dyn_step 1
"""
from __future__ import annotations

import math
import sys

from arrival_process import build_plan, generate_dynamic_trace
from config import ROUND_SECONDS, TIME_SCALE, rounds_to_real
from dynamic_metrics import compare_dynamic, dynamic_metrics
from workload import GPU_TYPES, profile_job

N_PER_TYPE = 12
PROFILE_ROUNDS = 60.0 / ROUND_SECONDS


def simulate(trace, gated: bool):
    """Toy capacity model. gated=True => decisions only at round boundaries."""
    for j in trace:
        profile_job(j)
    free = {g: N_PER_TYPE for g in GPU_TYPES}
    running = []            # (release_round, gpu, d)
    pending = sorted(trace, key=lambda j: j.arrival)
    queue, rec, i = [], {}, 0
    t = 0.0
    step = 1.0 if gated else 0.02
    horizon_guard = 100000

    while (i < len(pending) or queue or running) and horizon_guard > 0:
        horizon_guard -= 1
        t = round(t + step, 4)
        while i < len(pending) and pending[i].arrival <= t:
            queue.append(pending[i])
            rec[pending[i].job_id] = {"arrival": pending[i].arrival}
            i += 1
        for r in [x for x in running if x[0] <= t]:
            running.remove(r)
            free[r[1]] += r[2]
        # shortest-remaining-first in both models, so ordering is not the variable
        queue.sort(key=lambda j: j.W_j / max(j.theta.values()))
        for j in list(queue):
            # Try every feasible GPU type, fastest first, and take the first
            # with room. (Using only the single fastest type would strand
            # 24 of the 36 slots and silently turn this into a 12-GPU cluster.)
            feasible = sorted((g for g in GPU_TYPES if j.theta.get(g, 0) > 0),
                              key=lambda g: j.theta[g], reverse=True)
            if not feasible:
                queue.remove(j)
                continue
            target = next((g for g in feasible if free[g] >= j.d_j), None)
            if target is None:
                continue
            free[target] -= j.d_j
            queue.remove(j)
            stall = PROFILE_ROUNDS if gated else 0.0   # FFT profiles every job
            dur = j.W_j / j.theta[target] + stall
            running.append((t + dur, target, j.d_j))
            rec[j.job_id].update(first_exec=t + stall, finish=t + dur,
                                 placed=t, free_total=sum(free.values()))
    return rec


def to_telemetry(rec, trace, gated):
    """Render the toy result as the event log + job hashes the analyser expects."""
    t0 = 1_000_000.0
    real = lambda r: t0 + rounds_to_real(r)
    ev, jobs = [], {}
    for j in trace:
        r = rec.get(j.job_id, {})
        ev.append({"type": "arrival", "job": j.job_id, "ts": real(j.arrival)})
        if "placed" in r:
            ev.append({"type": "placed", "job": j.job_id, "ts": real(r["placed"])})
        jobs[j.job_id] = {
            "id": j.job_id, "W": j.W_j, "d": j.d_j, "theta": j.theta,
            "arrival_ts": real(j.arrival), "arrival_rounds": j.arrival,
            "decide_ts": real(math.ceil(j.arrival) if gated else j.arrival + 0.001),
            "first_exec_ts": real(r["first_exec"]) if "first_exec" in r else None,
            "finish_ts": real(r["finish"]) if "finish" in r else None,
        }
    ev.sort(key=lambda e: e["ts"])
    return ev, jobs


def main():
    regime = sys.argv[1] if len(sys.argv) > 1 else "dyn_step"
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    n_jobs = int(sys.argv[3]) if len(sys.argv) > 3 else 100
    horizon = max(60.0, n_jobs * float(sys.argv[4] if len(sys.argv) > 4 else 1.44))

    trace = generate_dynamic_trace(n_jobs, regime, seed, horizon)
    plan = build_plan(regime, seed, horizon)
    print(f"{regime} seed={seed} n={n_jobs} horizon={horizon:.0f} rounds "
          f"shock=[{plan.shock_onset:.1f},{plan.shock_end:.1f}]  "
          f"TIME_SCALE={TIME_SCALE:.0f}\n")

    res = {}
    for label, gated in (("fft", True), ("smart", False)):
        tr = generate_dynamic_trace(n_jobs, regime, seed, horizon)
        rec = simulate(tr, gated)
        ev, jobs = to_telemetry(rec, tr, gated)
        res[label] = dynamic_metrics(ev, jobs, regime, seed, n_jobs, horizon)

    print(compare_dynamic(res["fft"], res["smart"]))
    print("\nper-cohort mean time-to-first-execution (rounds):")
    for label in ("fft", "smart"):
        pc = res[label]["dyn_per_cohort"]
        print(f"  {label:6}", {c: round(pc[c]['ttfe_rounds_mean'], 2)
                               for c in ('baseline', 'shock', 'recovery')},
              f" n={[pc[c]['n'] for c in ('baseline','shock','recovery')]}")


if __name__ == "__main__":
    main()
