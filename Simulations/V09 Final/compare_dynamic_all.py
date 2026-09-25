"""
compare_dynamic_all.py — seed-averaged thesis table for the dynamic phase.

Reads runs/{mode}_{regime}_s{seed}.json and reports, per regime, the
seed-averaged adaptability metrics for FFT and SMART side by side.

STATISTICS, HONESTLY
--------------------
Three seeds is three samples. A t-based 95% confidence interval on n=3 has a
critical value of 4.303, so the interval is wide and a p-value from it would be
close to meaningless. This script therefore reports:

  * mean over seeds, with the t(2) 95% half-width, so the spread is visible;
  * the PAIRED per-seed comparison — FFT and SMART see the identical trace for
    a given (regime, seed), so the difference is paired and far more
    informative than the unpaired means;
  * a WINS column: how many of the three paired seeds favour SMART.

With n=3 the defensible claim is "SMART was better on all three paired seeds,
by a mean margin of X rounds", not "p < 0.05". If a metric splits 2-1, say so
and call it inconclusive. If you want a real significance claim, the cost is
more seeds, not a different test — 5 seeds per cell adds roughly 2 hours.

CONFIDENCE MARKERS follow the project standard: a cell is quotable only if all
three seeds completed with every job finished.

Usage:
  python compare_dynamic_all.py
  python compare_dynamic_all.py --regime dyn_flash --verbose
  python compare_dynamic_all.py --csv results_dynamic.csv
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import statistics
from typing import Dict, List, Optional

T95_N3 = 4.303   # two-sided t critical value, 2 degrees of freedom

# (key, direction, label). direction "lower" means lower is better.
METRICS = [
    # --- the research gap, measured directly --------------------------------
    ("dyn_shock_ttfe_rounds", "lower", "surge-job wait to start (abs)"),
    ("dyn_shock_ttuw_rounds", "lower", "surge-job wait to useful work"),
    ("dyn_reaction_rounds", "lower", "reaction to surge"),
    ("dyn_backlog_peak", "lower", "peak backlog (jobs)"),
    ("dyn_backlog_auc_shock_jobrounds", "lower", "backlog area during surge"),
    ("dyn_stranded_gpu_rounds", "lower", "idle usable GPU-rds w/ queue"),
    ("dyn_ttfe_p99_rounds", "lower", "wait p99 (whole run)"),
    # --- load sensitivity: does a surge make things WORSE than calm? --------
    ("dyn_excess_ttfe_rounds", "lower", "excess wait, surge vs calm"),
    ("dyn_recovery_excess_ttfe_rounds", "lower", "excess wait, post-surge"),
    ("dyn_excess_jct_rounds", "lower", "excess JCT, surge vs calm"),
    # --- context: the costs, so the trade-off stays visible -----------------
    ("jct_mean_rounds", "lower", "  [context] run-wide mean JCT"),
    ("preemptions", "lower", "  [context] preemptions"),
    ("profile_stalls", "lower", "  [context] profiling stalls"),
    ("sched_max_gap_rounds", "lower", "  [health] longest solve gap"),
]

RUN_RE = re.compile(r"(?P<mode>smart|fft)_(?P<regime>dyn_[a-z]+)_s(?P<seed>\d+)\.json$")


def load_runs(root: str = "runs") -> Dict[str, Dict[str, Dict[int, dict]]]:
    out: Dict[str, Dict[str, Dict[int, dict]]] = {}
    for path in sorted(glob.glob(os.path.join(root, "*.json"))):
        m = RUN_RE.search(os.path.basename(path))
        if not m:
            continue
        try:
            data = json.load(open(path))
        except Exception:
            continue
        out.setdefault(m["regime"], {}).setdefault(m["mode"], {})[int(m["seed"])] = data
    return out


def _vals(by_seed: Dict[int, dict], key: str, seeds: List[int]) -> List[float]:
    vs = []
    for s in seeds:
        v = by_seed.get(s, {}).get(key)
        if isinstance(v, (int, float)):
            vs.append(float(v))
    return vs


def _ci(vs: List[float]) -> Optional[float]:
    if len(vs) < 2:
        return None
    return T95_N3 * statistics.stdev(vs) / (len(vs) ** 0.5)


def report_regime(regime: str, runs: Dict[str, Dict[int, dict]], verbose: bool):
    fft, smart = runs.get("fft", {}), runs.get("smart", {})
    seeds = sorted(set(fft) & set(smart))
    if not seeds:
        print(f"\n### {regime}: no paired runs found — nothing to compare.")
        return []

    n_jobs = (fft[seeds[0]].get("config") or {}).get("n_jobs")
    def valid(d: dict) -> bool:
        return (d.get("n_finished", 0) >= (d.get("n_jobs") or 0) > 0
                and not d.get("sched_gaps_over_2_rounds")
                and not d.get("rpc_bad_target")
                and (d.get("provenance") or {}).get("code_match") is not False)
    complete = all(valid(r[s]) for r in (fft, smart) for s in seeds)
    marker = "CONFIRMED" if (complete and len(seeds) >= 3) else "NOT QUOTABLE"

    print(f"\n{'=' * 92}")
    print(f"### {regime}   paired seeds: {seeds}   n_jobs={n_jobs}   [{marker}]")
    if marker == "NOT QUOTABLE":
        print("    (needs 3 paired seeds, every job finished, no scheduler stall,"
              " PC1/PC2 code match)")
    print(f"{'=' * 92}")
    print(f"{'metric':38}{'FFT':>16}{'SMART':>16}{'SMART/FFT':>11}{'wins':>8}")

    rows = []
    for key, direction, label in METRICS:
        f_v = _vals(fft, key, seeds)
        s_v = _vals(smart, key, seeds)
        if not f_v or not s_v:
            continue
        f_m, s_m = statistics.mean(f_v), statistics.mean(s_v)
        f_ci, s_ci = _ci(f_v), _ci(s_v)

        # Paired: same trace, same seed, so compare seed by seed.
        pairs = [(fft[s].get(key), smart[s].get(key)) for s in seeds]
        pairs = [(a, b) for a, b in pairs
                 if isinstance(a, (int, float)) and isinstance(b, (int, float))]
        wins = sum(1 for a, b in pairs
                   if (b < a if direction == "lower" else b > a))

        ratio = s_m / f_m if abs(f_m) > 1e-9 else float("nan")
        f_s = f"{f_m:8.3f}+-{f_ci:5.2f}" if f_ci is not None else f"{f_m:14.3f}"
        s_s = f"{s_m:8.3f}+-{s_ci:5.2f}" if s_ci is not None else f"{s_m:14.3f}"
        verdict = f"{wins}/{len(pairs)}"
        if wins == len(pairs) and len(pairs) >= 3:
            verdict += "*"        # consistent across every paired seed
        print(f"{label:38}{f_s:>16}{s_s:>16}{ratio:10.2f}x{verdict:>8}")

        rows.append({"regime": regime, "metric": key, "label": label,
                     "fft_mean": f_m, "fft_ci95": f_ci,
                     "smart_mean": s_m, "smart_ci95": s_ci,
                     "ratio": ratio, "wins": wins, "n_pairs": len(pairs),
                     "marker": marker})

        if verbose:
            print(f"{'':38}per-seed FFT   " +
                  " ".join(f"{v:8.3f}" for v in f_v))
            print(f"{'':38}per-seed SMART " +
                  " ".join(f"{v:8.3f}" for v in s_v))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="runs")
    ap.add_argument("--regime", default=None)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--csv", default=None)
    a = ap.parse_args()

    runs = load_runs(a.root)
    if not runs:
        print(f"no dynamic runs found under {a.root}/ "
              f"(expecting e.g. runs/smart_dyn_step_s1.json)")
        return

    regimes = [a.regime] if a.regime else sorted(runs)
    all_rows = []
    for reg in regimes:
        all_rows += report_regime(reg, runs.get(reg, {}), a.verbose)

    print("\n" + "-" * 92)
    print("ratio < 1.00 favours SMART on every metric above (all are "
          "lower-is-better).")
    print("wins = paired seeds favouring SMART; * = consistent on all three.")
    print("Excess metrics are shock-cohort minus calm-cohort WITHIN a "
          "scheduler, so they are\nnot affected by either scheduler's absolute "
          "JCT advantage. The two [context] rows are\nnot adaptability "
          "measures; they are there so the trade-off stays visible.")

    if a.csv and all_rows:
        with open(a.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(all_rows[0]))
            w.writeheader()
            w.writerows(all_rows)
        print(f"\nwrote {a.csv}")


if __name__ == "__main__":
    main()
