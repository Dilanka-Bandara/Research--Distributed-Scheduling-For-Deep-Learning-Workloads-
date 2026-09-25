"""
run_dynamic.py — Phase 2 runner: the dynamic job-arrival experiment.

Same two-machine cluster, same containers, same FFT ILP core. The ONLY thing
that changes between Phase 1 (static) and Phase 2 (dynamic) is the arrival
process, which is the point: any difference in the results is attributable to
how the schedulers respond to a changing arrival rate, not to a code change.

PC 2 never needs restarting — node_agent.py's auto-reset loop already handles
state between runs. Bring PC 2 up once and leave it.

TIERS
  Tier 1 (dyn_step + dyn_flash, 3 seeds, 2 schedulers) = 12 runs, ~2.5 h.
         This alone answers the research gap. Run it first.
  Tier 2 (adds dyn_sine + dyn_mmpp)                    = 12 more runs, ~2.5 h.

Each regime needs its own HORIZON_PER_JOB so that offered load lands in the
discriminating band; calibrate_load.py solves for these and the values below
are its worst-case-over-seeds output for n_jobs=100 on a 36-GPU cluster. If you
change n_jobs, N_PER_TYPE or the model zoo, re-run the calibrator FIRST — an
uncalibrated dynamic run is wasted cluster time, because a saturated surge
makes both schedulers look identical.

Usage:
  python run_dynamic.py                      # Tier 1, all seeds, both modes
  python run_dynamic.py --tier 2             # Tier 2 only
  python run_dynamic.py --tier all
  python run_dynamic.py --mode smart         # one scheduler
  python run_dynamic.py --regime dyn_flash --seeds 1
  python run_dynamic.py --dry-run            # print the plan, run nothing
  python run_dynamic.py --single --regime dyn_flash --seeds 1   # PC 1 only
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

# Default: the two-machine controller stack. `--single` swaps in the
# all-in-one compose file so the whole pipeline can be validated on PC 1 alone.
COMPOSE_PC1 = "docker-compose.pc1.yml"
COMPOSE_SINGLE = "docker-compose.yml"
OVERLAY = "docker-compose.pc1.dynamic.yml"

TIER1 = ["dyn_step", "dyn_flash"]
TIER2 = ["dyn_sine", "dyn_mmpp"]

# Worst-case-over-seeds output of calibrate_load.py at n_jobs=100, 36 GPUs.
# Re-derive with: python calibrate_load.py all --jobs 100 --seeds 1,2,3
HORIZON_PER_JOB = {
    "dyn_step": 1.84,
    "dyn_flash": 1.84,
    "dyn_sine": 2.19,
    "dyn_mmpp": 2.19,
}

ALL_MODES = ["smart", "fft"]
ALL_SEEDS = [1, 2, 3]


def sh(cmd, check=False, env=None):
    return subprocess.run(cmd, shell=True, check=check,
                          env={**os.environ, **(env or {})})


def compose_flags(base: str, overlay: str | None) -> str:
    """`-f base [-f overlay]`. Base must come first; later files override."""
    flags = f"-f {base}"
    if overlay and os.path.exists(overlay):
        flags += f" -f {overlay}"
    return flags


def is_done(mode, regime, seed, jobs) -> bool:
    """A run counts as done only if every job finished AND the adaptability
    metrics were actually extracted. A timed-out run that saved partial
    metrics must be re-run, not silently skipped."""
    path = f"runs/{mode}_{regime}_s{seed}.json"
    if not os.path.exists(path):
        return False
    try:
        d = json.load(open(path))
    except Exception:
        return False
    if d.get("n_finished", 0) < jobs:
        return False
    if "dyn_excess_ttfe_rounds" not in d or "dyn_error" in d:
        return False
    # V09: a run whose scheduler froze, or whose PCs ran different code, is
    # not a result — re-run it rather than let it into the table.
    if d.get("sched_gaps_over_2_rounds", 0) or d.get("rpc_bad_target", 0):
        return False
    if (d.get("provenance") or {}).get("code_match") is False:
        return False
    return True


def estimate_minutes(regime, jobs, time_scale) -> float:
    horizon = max(60.0, jobs * HORIZON_PER_JOB[regime])
    arrival_min = horizon * 300.0 / time_scale / 60.0
    return arrival_min * 1.6 + 1.0     # + drain tail + container churn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["1", "2", "all"], default="1")
    ap.add_argument("--mode", choices=["all", "smart", "fft"], default="all")
    ap.add_argument("--regime", default=None)
    ap.add_argument("--seeds", default="1,2,3")
    ap.add_argument("--jobs", type=int, default=100)
    ap.add_argument("--timeout", type=int, default=2400)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--single", action="store_true",
                    help="run everything on THIS machine (docker-compose.yml, "
                         "agents included) instead of the two-PC stack. For "
                         "validation only — results are not comparable with "
                         "the two-machine static suite.")
    ap.add_argument("--compose", default=None,
                    help="override the compose file explicitly")
    ap.add_argument("--no-probe", action="store_true",
                    help="skip the dynamic overlay (no stranded-capacity data)")
    a = ap.parse_args()

    base = a.compose or (COMPOSE_SINGLE if a.single else COMPOSE_PC1)
    if not os.path.exists(base):
        sys.exit(f"compose file not found: {base}")
    cf = compose_flags(base, None if a.no_probe else OVERLAY)

    if a.regime:
        regimes = [a.regime]
    elif a.tier == "1":
        regimes = TIER1
    elif a.tier == "2":
        regimes = TIER2
    else:
        regimes = TIER1 + TIER2

    unknown = [r for r in regimes if r not in HORIZON_PER_JOB]
    if unknown:
        sys.exit(f"no calibration for {unknown} — run calibrate_load.py first")

    modes = ALL_MODES if a.mode == "all" else [a.mode]
    seeds = [int(s) for s in a.seeds.split(",")]
    time_scale = float(os.environ.get("EMU_TIME_SCALE", "100"))

    plan = [(m, r, s) for m in modes for r in regimes for s in seeds]
    est = sum(estimate_minutes(r, a.jobs, time_scale) for _, r, _ in plan)

    print("=" * 78)
    print(f"  DYNAMIC ARRIVAL SUITE — {len(plan)} runs, ~{est/60:.1f} h estimated")
    print(f"  regimes {regimes}   modes {modes}   seeds {seeds}")
    print(f"  jobs/run {a.jobs}   TIME_SCALE {time_scale:.0f}   "
          f"N_PER_TYPE {os.environ.get('EMU_N_PER_TYPE', '(unset!)')}")
    print(f"  compose: {cf}"
          + ("   [SINGLE MACHINE — validation only]" if a.single else ""))
    print("  horizon_per_job " + ", ".join(
        f"{r}={HORIZON_PER_JOB[r]}" for r in regimes))
    print("=" * 78)

    if os.environ.get("EMU_N_PER_TYPE") != "12":
        print("  WARNING: EMU_N_PER_TYPE is not 12. The calibration above "
              "assumes a 36-GPU cluster.\n")

    if a.dry_run:
        for i, (m, r, s) in enumerate(plan, 1):
            state = "done" if (not a.force and is_done(m, r, s, a.jobs)) else "TO RUN"
            print(f"  [{i:2}/{len(plan)}] {m:5} {r:10} seed {s}   {state}")
        return

    done = skipped = failed = 0
    current_mode = None
    try:
        for i, (mode, regime, seed) in enumerate(plan, 1):
            tag = f"[{i}/{len(plan)}] {mode.upper()} | {regime} | seed {seed}"
            if not a.force and is_done(mode, regime, seed, a.jobs):
                print(f"\n>> {tag} -> already complete, skipping")
                skipped += 1
                continue

            print(f"\n{'=' * 78}\n   {tag}\n{'=' * 78}")
            current_mode = mode

            # HORIZON_PER_JOB must reach the orchestrator container, and it is
            # per-regime, so it is injected here rather than baked into .env.
            env = {"EMU_HORIZON_PER_JOB": str(HORIZON_PER_JOB[regime])}

            sh(f"docker compose {cf} --profile {mode} up -d", env=env)
            time.sleep(3)

            res = sh(
                f"docker compose {cf} --profile tools "
                f"run --rm -e EMU_HORIZON_PER_JOB={HORIZON_PER_JOB[regime]} "
                f"orchestrator {a.jobs} {regime} {seed} {mode} {a.timeout}",
                env=env,
            )
            sh(f"docker compose {cf} --profile {mode} down", env=env)

            if res.returncode == 0 and is_done(mode, regime, seed, a.jobs):
                done += 1
                print(f"[+] {tag} OK")
            else:
                failed += 1
                print(f"[-] {tag} FAILED or incomplete (rc={res.returncode})")
            time.sleep(3)

    except KeyboardInterrupt:
        print("\n[!] interrupted — cleaning up containers")
        if current_mode:
            sh(f"docker compose {cf} --profile {current_mode} down")
        sys.exit(1)

    print(f"\n{'=' * 78}")
    print(f"  FINISHED   ok={done}  skipped={skipped}  failed={failed}")
    print("=" * 78)
    sh("python compare_dynamic_all.py")


if __name__ == "__main__":
    main()
