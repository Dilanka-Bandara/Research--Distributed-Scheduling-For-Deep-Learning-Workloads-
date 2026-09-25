"""
dynamic_metrics.py — metrics that actually measure ADAPTABILITY.

WHY THE EXISTING METRICS ARE NOT ENOUGH
---------------------------------------
`analyze_results.metrics` reports run-wide means: mean JCT, mean FTF, mean
starvation, mean decision latency. Those are STEADY-STATE aggregates. Under a
load shock they average the calm stretches together with the stressed one, so a
scheduler that handles a surge badly for twenty rounds and well for the other
sixty still posts a respectable mean. The research gap is about what happens
DURING the change, so the measurement has to be windowed and cohort-conditioned.

THE FRAMING: STEP RESPONSE
--------------------------
A load step is a step input; the cluster is the plant; the scheduler is the
controller. A round-gated centralized scheduler has a structural DEAD TIME of up
to one full round before it can even observe a new arrival, and its correction
is applied in discrete round-sized kicks. Classical step-response descriptors
therefore transfer directly and give an examiner familiar vocabulary:

  dead time / reaction   how long before the first surge job runs at all
  overshoot              peak backlog above the pre-shock level
  settling time          how long after the surge ends before backlog recovers
  stranded capacity      GPU-rounds left idle while jobs were queued
                         (a pure work-conservation violation — the clearest
                          single artefact of round-gating)

THE HEADLINE NUMBER: ADAPTATION PENALTY
---------------------------------------
    excess(metric) = metric(shock cohort) - metric(baseline cohort)     [rounds]

measured WITHIN one scheduler, then compared across schedulers. This is the
number the research gap needs: it says how much WORSE a job does purely for
having arrived during the change, with each scheduler judged against its own
calm-period behaviour. Any constant advantage a scheduler holds in absolute JCT
cancels, so the comparison survives the honest admission that SMART does not
beat FFT on absolute JCT.

ADDITIVE, NOT A RATIO — and this matters. The ratio form shock/baseline blows
up whenever the baseline is near zero, which is exactly the regime an
event-driven dispatcher lives in: SMART's calm-period time-to-first-execution
is ~0.003 rounds, so a perfectly good 0.5-round shock response reports as a
"160x degradation" while FFT's 0.5-round calm baseline makes the same 0.5-round
shock response look like 1.0x. The ratio would punish the faster scheduler for
being fast. The ratio is still reported, floored, as a secondary figure, but
the additive excess in rounds is the one to put in the thesis.

All times are reported in ROUNDS of scaled time, per the project's standard.
"""
from __future__ import annotations

import statistics
from typing import Dict, List, Optional

from arrival_process import DYNAMIC_REGIMES, build_plan
from config import real_to_rounds


# ----------------------------------------------------------------------
def _pct(xs: List[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[k]


def _mean(xs: List[float]) -> float:
    return statistics.mean(xs) if xs else 0.0


def _ratio(a: float, b: float) -> float:
    return a / b if b > 1e-9 else 0.0


# ----------------------------------------------------------------------
def backlog_series(ev: List[dict], t0: float):
    """Exact backlog(t) reconstructed from the event log — no probe required.

    backlog(t) = (# jobs that have arrived by t) - (# jobs first placed by t)

    This is the true pending-work curve and it works identically for both
    schedulers, even though FFT parks arrivals in `arrivals` and SMART parks
    them in `queue:global`.
    """
    pts = []
    seen_placed = set()
    for e in ev:
        if e.get("type") == "arrival":
            pts.append((e["ts"], +1))
        elif e.get("type") == "placed":
            jid = e.get("job")
            if jid is not None and jid not in seen_placed:
                seen_placed.add(jid)
                pts.append((e["ts"], -1))
    pts.sort(key=lambda p: p[0])
    series, depth = [], 0
    for ts, delta in pts:
        depth += delta
        series.append((real_to_rounds(ts - t0), depth))
    return series


def _auc(series, lo: Optional[float] = None, hi: Optional[float] = None) -> float:
    """Area under the backlog step function, in job-rounds."""
    if len(series) < 2:
        return 0.0
    total = 0.0
    for (t_a, d_a), (t_b, _) in zip(series, series[1:]):
        a, b = t_a, t_b
        if lo is not None:
            a = max(a, lo)
        if hi is not None:
            b = min(b, hi)
        if b > a:
            total += d_a * (b - a)
    return total


def _depth_at(series, t: float) -> int:
    d = 0
    for ts, depth in series:
        if ts > t:
            break
        d = depth
    return d


# ----------------------------------------------------------------------
def dynamic_metrics(ev: List[dict], jobs: Dict, regime: str, seed: int,
                    n_jobs: int, horizon: float) -> dict:
    """Adaptability metrics. Returns {} for stationary regimes."""
    if regime not in DYNAMIC_REGIMES:
        return {}

    plan = build_plan(regime, seed, horizon)
    arr_ts = [j["arrival_ts"] for j in jobs.values() if j.get("arrival_ts")]
    if not arr_ts:
        return {"dyn_error": "no arrivals recorded"}
    t0 = min(arr_ts)

    # --- cohort-conditioned per-job metrics --------------------------------
    cohorts: Dict[str, Dict[str, List[float]]] = {
        c: {"admit": [], "ttfe": [], "ttuw": [], "jct": []}
        for c in ("baseline", "shock", "recovery")
    }
    for j in jobs.values():
        a_r = j.get("arrival_rounds")
        if a_r is None:
            continue
        c = plan.cohort(a_r)
        bucket = cohorts[c]
        if j.get("decide_ts"):
            bucket["admit"].append(real_to_rounds(j["decide_ts"] - j["arrival_ts"]))
        if j.get("first_exec_ts"):
            bucket["ttfe"].append(real_to_rounds(j["first_exec_ts"] - j["arrival_ts"]))
        # time to USEFUL work: after any profiling / migration stall (V09)
        if j.get("first_progress_ts"):
            bucket["ttuw"].append(real_to_rounds(j["first_progress_ts"] - j["arrival_ts"]))
        if j.get("finish_ts"):
            bucket["jct"].append(real_to_rounds(j["finish_ts"] - j["arrival_ts"]))

    per_cohort = {}
    for c, b in cohorts.items():
        per_cohort[c] = {
            "n": len(b["jct"]),
            "admit_rounds_mean": _mean(b["admit"]),
            "admit_rounds_p95": _pct(b["admit"], 0.95),
            "ttfe_rounds_mean": _mean(b["ttfe"]),
            "ttfe_rounds_p95": _pct(b["ttfe"], 0.95),
            "ttfe_rounds_max": max(b["ttfe"]) if b["ttfe"] else 0.0,
            "ttuw_rounds_mean": _mean(b["ttuw"]),
            "ttuw_rounds_p95": _pct(b["ttuw"], 0.95),
            "jct_rounds_mean": _mean(b["jct"]),
            "jct_rounds_p95": _pct(b["jct"], 0.95),
        }

    base, shock = per_cohort["baseline"], per_cohort["shock"]
    recov = per_cohort["recovery"]

    def excess(key: str) -> float:
        return shock[key] - base[key]

    def recovery_excess(key: str) -> float:
        return (recov[key] - base[key]) if recov["n"] else 0.0

    # --- scheduler watchdog (V09) -------------------------------------------
    # Longest gap between consecutive scheduler solves. For FFT this should sit
    # at ~1 round (its round length); for the brain at ~0.3 (its wake
    # interval). V08's brain froze for 19.5 rounds three times per run and no
    # metric showed it — the symptom surfaced as "starvation" and was
    # misattributed to the architecture. Any run where this exceeds a few
    # rounds is a broken run, not a result.
    solve_ts = sorted(e["ts"] for e in ev
                      if e.get("type") in ("brain_solve", "fft_solve"))
    gaps = [real_to_rounds(b - a) for a, b in zip(solve_ts, solve_ts[1:])]
    health = {
        "sched_solves": len(solve_ts),
        "sched_max_gap_rounds": max(gaps) if gaps else None,
        "sched_gaps_over_2_rounds": sum(1 for g in gaps if g > 2.0),
        "rpc_bad_target": sum(1 for e in ev if e.get("type") == "rpc_bad_target"),
        "rpc_timeouts": sum(1 for e in ev if e.get("type") == "rpc_timeout"),
    }

    # Ratios are floored at a tenth of a round: below that the denominator is
    # measurement noise, not a baseline.
    FLOOR = 0.1

    def ratio_floored(key: str) -> float:
        return shock[key] / max(FLOOR, base[key])

    # --- backlog dynamics ---------------------------------------------------
    series = backlog_series(ev, t0)
    depths = [d for _, d in series]
    on, off = plan.shock_onset, plan.shock_end
    pre_depths = [d for t, d in series if on is not None and t < on]
    pre_level = statistics.median(pre_depths) if pre_depths else 0.0

    # Settling target is an ABSOLUTE backlog level, identical for both
    # schedulers. Using each scheduler's own pre-shock level would set SMART a
    # stricter bar than FFT (SMART idles at backlog 0, FFT at 1-2), which would
    # penalise the faster scheduler for being faster — the same trap as the
    # ratio-form penalty above.
    SETTLE_TARGET = 1.0
    # Longest unbroken stretch with a non-empty backlog. A scheduler that
    # admits instantly but then parks jobs for twenty rounds looks excellent on
    # every mean and terrible here, which is exactly the failure this catches.
    longest_stall, stall_start, cur_start = 0.0, None, None
    for (t_a, d_a), (t_b, _) in zip(series, series[1:]):
        if d_a > 0:
            if cur_start is None:
                cur_start = t_a
            if t_b - cur_start > longest_stall:
                longest_stall, stall_start = t_b - cur_start, cur_start
        else:
            cur_start = None

    settling = None
    if off is not None and series:
        target = SETTLE_TARGET
        ok_from = None
        for t, d in series:
            if t < off:
                continue
            if d <= target:
                if ok_from is None:
                    ok_from = t
            else:
                ok_from = None
        settling = (ok_from - off) if ok_from is not None else None

    # --- reaction: how fast the first surge jobs actually start -------------
    shock_ttfe = sorted(cohorts["shock"]["ttfe"])
    reaction = shock_ttfe[0] if shock_ttfe else 0.0
    reaction_p25 = _pct(shock_ttfe, 0.25)

    # --- stranded capacity (needs probe.py samples) -------------------------
    #
    # Counting every free slot while the queue is non-empty OVERSTATES the
    # waste: if the queued jobs all need A10 (memory) and A10 is full, the free
    # T4 slots were never usable and nothing was actually stranded. The metric
    # now counts a type's free slots only when some job that is currently
    # queued is BOTH feasible on that type and small enough to fit. That is a
    # true work-conservation violation; the naive version is not.
    stranded = None
    stranded_naive = None
    probes = [e for e in ev if e.get("type") == "probe"]
    if len(probes) > 1:
        arrivals = sorted(
            ((j["arrival_ts"], jid) for jid, j in jobs.items()
             if j.get("arrival_ts")), key=lambda x: x[0])
        placed_at = {jid: j.get("first_exec_ts")
                     for jid, j in jobs.items()}
        stranded = stranded_naive = 0.0
        for p_a, p_b in zip(probes, probes[1:]):
            ts = p_a["ts"]
            t_a, t_b = real_to_rounds(ts - t0), real_to_rounds(p_b["ts"] - t0)
            if t_b <= t_a or _depth_at(series, t_a) <= 0:
                continue
            dt = t_b - t_a
            stranded_naive += p_a.get("free_total", 0) * dt
            free = p_a.get("free") or {}
            queued = [jobs[jid] for _, jid in arrivals
                      if jobs[jid]["arrival_ts"] <= ts
                      and (placed_at.get(jid) is None or placed_at[jid] > ts)]
            for gpu, n_free in free.items():
                n_free = int(n_free or 0)
                if n_free <= 0:
                    continue
                usable = any((j.get("theta") or {}).get(gpu, 0) > 0
                             and (j.get("d") or 1) <= n_free for j in queued)
                if usable:
                    stranded += n_free * dt

    out = {
        "dyn_regime": regime,
        "dyn_shock_window_rounds": [on, off],
        "dyn_per_cohort": per_cohort,

        # ---- headline: additive adaptation excess, in rounds ----
        "dyn_excess_ttfe_rounds": excess("ttfe_rounds_mean"),
        "dyn_excess_ttfe_p95_rounds": excess("ttfe_rounds_p95"),
        "dyn_excess_jct_rounds": excess("jct_rounds_mean"),
        "dyn_excess_admit_rounds": excess("admit_rounds_mean"),
        "dyn_excess_ttuw_rounds": excess("ttuw_rounds_mean"),
        # ---- the weakness V08 appeared to have, now measured directly ----
        "dyn_recovery_excess_ttfe_rounds": recovery_excess("ttfe_rounds_mean"),
        "dyn_recovery_excess_jct_rounds": recovery_excess("jct_rounds_mean"),
        **health,
        # ---- secondary, floored ratios (do not headline these) ----
        "dyn_penalty_ratio_ttfe": ratio_floored("ttfe_rounds_mean"),
        "dyn_penalty_ratio_jct": ratio_floored("jct_rounds_mean"),
        # ---- absolute shock-cohort values: the plain cross-scheduler read ----
        "dyn_shock_ttfe_rounds": shock["ttfe_rounds_mean"],
        "dyn_shock_jct_rounds": shock["jct_rounds_mean"],
        "dyn_shock_admit_rounds": shock["admit_rounds_mean"],
        "dyn_shock_ttuw_rounds": shock["ttuw_rounds_mean"],

        # ---- step-response descriptors ----
        "dyn_reaction_rounds": reaction,
        "dyn_reaction_p25_rounds": reaction_p25,
        "dyn_backlog_peak": max(depths) if depths else 0,
        "dyn_backlog_pre_level": pre_level,
        "dyn_backlog_overshoot": (max(depths) - pre_level) if depths else 0.0,
        "dyn_longest_backlog_stall_rounds": longest_stall,
        "dyn_longest_stall_start_round": stall_start,
        "dyn_backlog_auc_jobrounds": _auc(series),
        "dyn_backlog_auc_shock_jobrounds": _auc(series, on, off),
        "dyn_settling_rounds": settling,
        "dyn_settle_target_backlog": 1.0,
        "dyn_stranded_gpu_rounds": stranded,
        "dyn_stranded_gpu_rounds_naive": stranded_naive,

        # ---- tails ----
        "dyn_ttfe_p99_rounds": _pct(
            [v for b in cohorts.values() for v in b["ttfe"]], 0.99),
        "dyn_backlog_series": [[round(t, 3), d] for t, d in series],
    }
    return out


# ----------------------------------------------------------------------
def compare_dynamic(fft: dict, smart: dict) -> str:
    """Side-by-side table for one regime/seed pair."""
    rows = [
        ("dyn_excess_ttfe_rounds", "lower"),
        ("dyn_excess_ttfe_p95_rounds", "lower"),
        ("dyn_excess_jct_rounds", "lower"),
        ("dyn_excess_admit_rounds", "lower"),
        ("dyn_shock_ttfe_rounds", "lower"),
        ("dyn_shock_jct_rounds", "lower"),
        ("dyn_reaction_rounds", "lower"),
        ("dyn_backlog_peak", "lower"),
        ("dyn_backlog_overshoot", "lower"),
        ("dyn_backlog_auc_shock_jobrounds", "lower"),
        ("dyn_settling_rounds", "lower"),
        ("dyn_stranded_gpu_rounds", "lower"),
        ("dyn_ttfe_p99_rounds", "lower"),
    ]
    lines = [f"{'metric':34}{'FFT':>12}{'SMART':>12}{'SMART/FFT':>12}"]
    for k, _ in rows:
        f, s = fft.get(k), smart.get(k)
        if f is None or s is None:
            lines.append(f"{k:34}{'n/a':>12}{'n/a':>12}{'':>12}")
            continue
        lines.append(f"{k:34}{f:12.3f}{s:12.3f}{_ratio(s, f):11.2f}x")
    return "\n".join(lines)
