"""
calibrate_load.py — pick the run parameters BEFORE burning two-PC cluster time.

THE TRAP THIS AVOIDS
--------------------
A dynamic-arrival experiment only measures adaptability if the cluster is
loaded but not drowning:

  * too light  -> every job is placed instantly by both schedulers, the surge
                  leaves no trace, and the two schedulers look identical;
  * too heavy  -> the surge saturates capacity, every metric collapses into
                  raw queueing delay, and you are measuring cluster size rather
                  than scheduling policy. (The backlog never drains inside the
                  horizon, and the settling time exceeds the run.)

Target: rho ~ 0.5-0.6 in the quiet phases, rho ~ 1.5-2.0 during the surge
(transient overload that the cluster CAN work off), and rho < 0.85 run-wide so
the backlog provably drains before the horizon ends.

Note that in the stock replayer horizon = n_jobs * HORIZON_PER_JOB, so
rho is INDEPENDENT of n_jobs: scaling job count scales the horizon with it.
The only real knob is HORIZON_PER_JOB (how far apart arrivals are spread).
That is why this script solves for HORIZON_PER_JOB, not for n_jobs.

    python calibrate_load.py
    python calibrate_load.py dyn_step 1 --coeff 2.6
"""
from __future__ import annotations

import argparse
import statistics

from arrival_process import DYNAMIC_REGIMES, build_plan, generate_dynamic_trace
from workload import GPU_TYPES, profile_job

N_PER_TYPE = 12
CAPACITY = N_PER_TYPE * len(GPU_TYPES)   # 36 GPU slots


def job_work(j) -> float:
    """GPU-rounds of work, in expectation over where the job actually lands.

    Using the FASTEST feasible GPU would be a lower bound that no loaded
    cluster achieves: with 12 slots of each type, a job queued behind others
    spills onto slower hardware. We therefore weight throughput by slot share
    across the job's FEASIBLE types, which is what the cluster delivers on
    average. Ignoring this understates rho by roughly 40% and is exactly how a
    "calibrated" experiment ends up saturated.
    """
    profile_job(j)
    feasible = [t for t in j.theta.values() if t > 0]
    if not feasible:
        return 0.0
    theta_eff = sum(feasible) / len(feasible)   # equal slots per type
    return (j.W_j / theta_eff) * j.d_j


def analyse(regime: str, seed: int, n_jobs: int, coeff: float) -> dict:
    horizon = max(60.0, n_jobs * coeff)
    trace = generate_dynamic_trace(n_jobs, regime, seed, horizon)
    plan = build_plan(regime, seed, horizon)

    work = {j.job_id: job_work(j) for j in trace}
    total = sum(work.values())

    by_phase: dict[str, list] = {}
    for j in trace:
        by_phase.setdefault(plan.phase_at(j.arrival).label, []).append(j)

    phase_rho = {}
    for label in by_phase:
        span = sum(p.t1 - p.t0 for p in plan.phases
                   if p.label == label and p.t1 <= horizon + 1)
        w = sum(work[j.job_id] for j in by_phase[label])
        phase_rho[label] = {
            "n": len(by_phase[label]),
            "span_rounds": round(span, 1),
            "rho": round(w / max(1e-9, CAPACITY * span), 2),
        }

    return {
        "regime": regime, "seed": seed, "n_jobs": n_jobs,
        "coeff": coeff, "horizon": round(horizon, 1),
        "rho_overall": round(total / (CAPACITY * horizon), 2),
        "mean_work_gpurounds": round(statistics.mean(work.values()), 1),
        "median_job_rounds": round(
            statistics.median(work[j.job_id] / j.d_j for j in trace), 1),
        "phases": phase_rho,
        "wallclock_min_at_100x": round(horizon * 300 / 100 / 60, 1),
    }


BASELINE_LABELS = ("quiet", "low")


def _baseline_rho(r: dict) -> float:
    """rho of the calm phase — the cohort every shock number is compared against."""
    vals = [d["rho"] for lab, d in r["phases"].items() if lab in BASELINE_LABELS]
    return max(vals) if vals else r["rho_overall"]


def suggest(regime: str, seed: int, n_jobs: int, target: float = 0.78) -> float:
    """Solve for the HORIZON_PER_JOB coefficient hitting a target run-wide rho.

    Run-wide rho is the binding constraint, because it is what decides whether
    the backlog drains inside the run. The calm-phase rho then falls out of the
    regime's contrast constants; if it lands below ~0.30 the fix is to LOWER
    the contrast constants in arrival_process.py (STEP_RATE_RATIO and friends),
    NOT to stretch the horizon further — a longer horizon lowers both numbers
    together and just buys idle time.
    """
    lo, hi = 0.3, 60.0
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        if analyse(regime, seed, n_jobs, mid)["rho_overall"] > target:
            lo = mid
        else:
            hi = mid
    return round(0.5 * (lo + hi), 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("regime", nargs="?", default="all")
    ap.add_argument("seed", nargs="?", type=int, default=1)
    ap.add_argument("--seeds", default=None,
                    help="comma list; solves the WORST-CASE coeff over all "
                         "seeds so no seed saturates (use '1,2,3')")
    ap.add_argument("--jobs", type=int, default=120)
    ap.add_argument("--coeff", type=float, default=None,
                    help="HORIZON_PER_JOB; stock replayer uses 1.4")
    ap.add_argument("--target", type=float, default=0.78,
                    help="target run-wide rho (keep <= 0.85 so backlog drains)")
    a = ap.parse_args()

    regimes = DYNAMIC_REGIMES if a.regime == "all" else [a.regime]
    seeds = ([int(x) for x in a.seeds.split(",")] if a.seeds else [a.seed])
    for reg in regimes:
        # Worst case across seeds: a coefficient that is safe for the heaviest
        # seed is safe for all of them, and one coefficient per regime keeps
        # the three seeds directly averageable.
        # The HEAVIEST seed sets the coefficient: a horizon long enough for it
        # is long enough for the others, and one coefficient per regime keeps
        # the three seeds directly averageable.
        coeff = a.coeff or max(suggest(reg, s, a.jobs, a.target) for s in seeds)
        r = analyse(reg, seeds[0], a.jobs, coeff)
        print(f"\n=== {reg}  seed={a.seed}  n_jobs={r['n_jobs']} "
              f"HORIZON_PER_JOB={coeff} ===")
        print(f"  horizon         {r['horizon']} rounds "
              f"(~{r['wallclock_min_at_100x']} min wall clock at 100x)")
        print(f"  rho overall     {r['rho_overall']}   "
              f"(must be < 0.85 so the backlog provably drains)")
        print(f"  rho calm phase  {_baseline_rho(r):.2f}   "
              f"(target {a.target}; this is the baseline cohort)")
        print(f"  mean job work   {r['mean_work_gpurounds']} GPU-rounds, "
              f"median duration {r['median_job_rounds']} rounds")
        if len(seeds) > 1:
            worst = max(analyse(reg, s, a.jobs, coeff)["rho_overall"]
                        for s in seeds)
            print(f"  worst-seed rho  {worst}   (over seeds {seeds})")
        for label, d in sorted(r["phases"].items(),
                               key=lambda kv: -kv[1]["rho"]):
            flag = ""
            if d["rho"] > 3.6 or (d["rho"] > 2.3 and d["span_rounds"] > 40):
                flag = "  <-- saturating for too long, will not drain"
            elif d["rho"] < 0.20:
                flag = "  <-- too idle to discriminate"
            print(f"    {label:9} n={d['n']:4}  span={d['span_rounds']:7} "
                  f"rho={d['rho']:5}{flag}")


if __name__ == "__main__":
    main()
