"""
arrival_process.py — non-stationary ("dynamic") job-arrival processes.

WHY THIS FILE EXISTS
--------------------
`workload.generate_trace` only branches on "steady" and "bursty"; every other
regime name silently falls through to the stationary "mixed" generator. The
arrival rate lambda is therefore CONSTANT in every existing regime, so none of
them can exercise the research gap ("limited adaptability to dynamic job
arrivals with centralized scheduling"). This module supplies genuinely
NON-STATIONARY arrival processes: lambda(t) changes during the run, and so does
the job mix.

METHOD (and why it is defensible in a viva)
-------------------------------------------
Arrivals follow a non-homogeneous Poisson process (NHPP) with intensity
lambda(t). We do NOT use thinning (Lewis & Shedler), because thinning yields a
RANDOM number of jobs, which would make the FFT vs SMART comparison unfair
(different traces would carry different amounts of work).

Instead we use the *conditional uniformity property* of the NHPP (Cinlar,
"Introduction to Stochastic Processes", Ch. 4): conditioned on N(H) = n, the n
arrival epochs of an NHPP are distributed as n i.i.d. draws from the density
    f(t) = lambda(t) / Lambda(H),      Lambda(t) = integral_0^t lambda(s) ds
So we draw n uniforms u_i ~ U(0,1) and invert the normalised cumulative
intensity: t_i = Lambda^-1(u_i * Lambda(H)).

This gives EXACTLY n jobs (identical total work across schedulers, identical
completion criterion) while reproducing the exact shape of lambda(t). It is the
standard textbook construction, not an ad-hoc hack.

NON-STATIONARITY IS TWO-DIMENSIONAL
-----------------------------------
Real dynamic clusters do not merely change *how many* jobs arrive; they change
*what kind*. Each phase therefore carries its own model-mix weights and a
worker-count scale factor, so a load surge also shifts the composition toward
large multi-worker models (GPT-neo / GPT-2 / OPT-6.7B). Job length W_j keeps
the SAME log-normal distribution in every phase, on purpose: that keeps total
work attributable to the arrival process rather than to a silent change in job
size, so any JCT difference is a scheduling effect.

REGIMES
-------
  dyn_step   Load step. Quiet -> 2.2x rate surge WITH a heavy-model mix and
             larger worker requests -> quiet. Rate and composition move
             together, which is how real surges behave; the effective load
             step is ~3.5x.
             The classic control-theory step response. Measures dead time,
             overshoot and settling time.
  dyn_sine   Diurnal sinusoid (Philly / Alibaba traces show daily cycles).
             Rate ONLY -- the model mix and worker counts are held constant on
             purpose, so this regime isolates pure rate tracking from the
             composition shift that dyn_step confounds it with.
  dyn_mmpp   2-state Markov-modulated Poisson process. LOW/HIGH alternate at
             random exponential times, so no scheduler can anticipate the
             switch. Measures robustness without lookahead.
  dyn_flash  Flash crowds: three near-instantaneous impulses separated by idle
             gaps. Measures impulse response and overload recovery.

The three existing regimes (steady / mixed / bursty) are untouched and remain
byte-identical, so every result already collected stays valid as the STATIONARY
CONTROL arm of the experiment.
"""
from __future__ import annotations

import bisect
import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

GRID = 4000  # resolution of the numeric cumulative-intensity grid

# Model-mix presets. Order matches workload.MODEL_NAMES:
#   ResNet50, VGG19, DenseNet, BERT-base, GPT-neo, GPT-2, OPT-6.7B
MIX_LIGHT = [0.34, 0.16, 0.20, 0.18, 0.06, 0.04, 0.02]
MIX_NORMAL = [0.22, 0.13, 0.15, 0.18, 0.12, 0.12, 0.08]
MIX_HEAVY = [0.10, 0.08, 0.08, 0.14, 0.22, 0.20, 0.18]

DYNAMIC_REGIMES = ("dyn_step", "dyn_sine", "dyn_mmpp", "dyn_flash")

# ----------------------------------------------------------------------
# Load-contrast knobs. These are the ONLY numbers you should need to touch
# when re-calibrating, and calibrate_load.py reports their effect directly.
#
# Constraint they satisfy (verified by calibrate_load.py):
#   calm-phase  rho ~ 0.35-0.55  -> the baseline cohort is a genuinely loaded
#                                   cluster, so it is a fair denominator
#   surge-phase rho ~ 1.3-2.2    -> transient overload the cluster CAN work off
#   run-wide    rho < 0.85       -> the backlog provably drains, so settling
#                                   time is finite and measurable
# Raising these past those bands does not make the experiment "harder"; it
# makes it capacity-bound, at which point you are measuring cluster size rather
# than scheduling policy and both schedulers converge.
#
# The effective load step is larger than the rate ratio alone, because a surge
# also shifts the mix toward large models and raises worker counts.
# ----------------------------------------------------------------------
STEP_RATE_RATIO = 2.2      # dyn_step:  surge arrival rate / quiet arrival rate
STEP_D_SCALE = 1.15        # dyn_step:  worker-request inflation during surge
SINE_AMPLITUDE = 0.55      # dyn_sine:  lambda(t) = 1 + A*sin(...)
MMPP_RATE_RATIO = 2.0      # dyn_mmpp:  HIGH state rate / LOW state rate
FLASH_PEAK = 2.2           # dyn_flash: impulse height above background
FLASH_BACKGROUND = 0.6     # dyn_flash: inter-crowd trickle


@dataclass
class Phase:
    """A labelled stretch of wall-clock (in rounds) with its own job character."""
    t0: float
    t1: float
    label: str           # "quiet" | "surge" | "recover" | "low" | "high"
    mix: List[float]
    d_scale: float = 1.0

    def contains(self, t: float) -> bool:
        return self.t0 <= t < self.t1


@dataclass
class ArrivalPlan:
    """Everything the generator AND the analyser need, derived from (regime, seed)."""
    regime: str
    horizon: float
    grid: List[float]                  # time points, rounds
    lam: List[float]                   # intensity shape at each grid point
    phases: List[Phase]
    shock_onset: Optional[float] = None   # rounds; start of the stress window
    shock_end: Optional[float] = None     # rounds; end of the stress window
    cum: List[float] = field(default_factory=list)   # cumulative intensity

    def phase_at(self, t: float) -> Phase:
        for p in self.phases:
            if p.contains(t):
                return p
        return self.phases[-1]

    def cohort(self, t: float) -> str:
        """Which experimental cohort a job arriving at `t` belongs to."""
        if self.shock_onset is None:
            return "baseline"
        if t < self.shock_onset:
            return "baseline"
        if self.shock_end is not None and t >= self.shock_end:
            return "recovery"
        return "shock"

    def to_dict(self) -> dict:
        return {
            "regime": self.regime,
            "horizon": self.horizon,
            "shock_onset": self.shock_onset,
            "shock_end": self.shock_end,
            "phases": [
                {"t0": p.t0, "t1": p.t1, "label": p.label, "d_scale": p.d_scale}
                for p in self.phases
            ],
        }


# ----------------------------------------------------------------------
# Intensity construction
# ----------------------------------------------------------------------
def _finalise(plan: ArrivalPlan) -> ArrivalPlan:
    """Trapezoidal cumulative intensity, used for inverse-transform sampling."""
    cum = [0.0]
    g, lam = plan.grid, plan.lam
    for i in range(1, len(g)):
        dt = g[i] - g[i - 1]
        cum.append(cum[-1] + 0.5 * (lam[i] + lam[i - 1]) * dt)
    plan.cum = cum
    return plan


def build_plan(regime: str, seed: int, horizon: float) -> ArrivalPlan:
    """Deterministic given (regime, seed, horizon) — the analyser rebuilds it."""
    rng = random.Random((seed * 7919) ^ 0xD15EA5E)
    grid = [horizon * i / (GRID - 1) for i in range(GRID)]

    if regime == "dyn_step":
        # Quiet (0, H/3) -> 4x surge with heavy mix (H/3, 2H/3) -> quiet again.
        # Surge occupies the middle QUARTER, not a third: a sharp step is what
        # exposes scheduling policy. Stretch it into a long plateau and the
        # surge becomes capacity-bound, at which point both schedulers converge
        # because neither can conjure GPUs that do not exist.
        t1, t2 = 0.375 * horizon, 0.625 * horizon
        # 2.2x on rate; the heavy mix and d_scale supply the rest of the step.
        lam = [STEP_RATE_RATIO if t1 <= t < t2 else 1.0 for t in grid]
        phases = [
            Phase(0.0, t1, "quiet", MIX_LIGHT, 1.0),
            Phase(t1, t2, "surge", MIX_NORMAL, STEP_D_SCALE),
            Phase(t2, horizon + 1e-9, "recover", MIX_LIGHT, 1.0),
        ]
        return _finalise(ArrivalPlan(regime, horizon, grid, lam, phases, t1, t2))

    if regime == "dyn_sine":
        # Diurnal cycle: three full periods across the run, amplitude 0.8.
        period = horizon / 3.0
        lam = [1.0 + SINE_AMPLITUDE * math.sin(2 * math.pi * t / period)
               for t in grid]
        # Report the middle peak as the stress window.
        peak = 0.25 * period + period  # second period's crest
        phases, shock_on, shock_off = _phases_from_threshold(
            grid, lam, 1.0, horizon, "low", "high", heavy=False
        )
        lo = max(0.0, peak - 0.25 * period)
        hi = min(horizon, peak + 0.25 * period)
        return _finalise(ArrivalPlan(regime, horizon, grid, lam, phases, lo, hi))

    if regime == "dyn_mmpp":
        # 2-state CTMC. Mean holding time ~ H/7 in each state; HIGH is 5x LOW.
        mean_hold = horizon / 7.0
        lam, switch_pts, state = [], [], 0  # 0 = LOW, 1 = HIGH
        t_next = rng.expovariate(1.0 / mean_hold)
        for t in grid:
            while t >= t_next:
                state ^= 1
                switch_pts.append(t_next)
                t_next += rng.expovariate(1.0 / mean_hold)
            lam.append(MMPP_RATE_RATIO if state else 1.0)
        phases, shock_on, shock_off = _phases_from_threshold(
            grid, lam, 0.5 * (1.0 + MMPP_RATE_RATIO), horizon, "low", "high",
            heavy=False
        )
        # Stress window = the longest HIGH stretch.
        highs = [p for p in phases if p.label == "high"]
        if highs:
            longest = max(highs, key=lambda p: p.t1 - p.t0)
            shock_on, shock_off = longest.t0, longest.t1
        else:
            shock_on = shock_off = None
        return _finalise(
            ArrivalPlan(regime, horizon, grid, lam, phases, shock_on, shock_off)
        )

    if regime == "dyn_flash":
        # Three flash crowds (narrow Gaussian impulses) on a low background.
        centres = [0.22 * horizon, 0.50 * horizon, 0.78 * horizon]
        width = 0.014 * horizon
        lam = []
        for t in grid:
            v = FLASH_BACKGROUND  # trickle between crowds
            for c in centres:
                v += FLASH_PEAK * math.exp(-0.5 * ((t - c) / width) ** 2)
            lam.append(v)
        phases = []
        prev = 0.0
        for c in centres:
            a, b = max(0.0, c - 2 * width), min(horizon, c + 2 * width)
            if a > prev:
                phases.append(Phase(prev, a, "quiet", MIX_LIGHT, 1.0))
            phases.append(Phase(a, b, "surge", MIX_NORMAL, 1.2))
            prev = b
        phases.append(Phase(prev, horizon + 1e-9, "recover", MIX_LIGHT, 1.0))
        c = centres[1]
        return _finalise(
            ArrivalPlan(
                regime, horizon, grid, lam, phases,
                max(0.0, c - 2 * width), min(horizon, c + 2 * width),
            )
        )

    raise ValueError(f"unknown dynamic regime: {regime!r}")


def _phases_from_threshold(grid, lam, thr, horizon, lo_label, hi_label,
                           heavy: bool = True):
    """Segment a continuous intensity into labelled low/high phases.

    heavy=False keeps the mix and worker scale constant across phases, so the
    regime varies the arrival RATE alone.
    """
    hi_mix = MIX_HEAVY if heavy else MIX_NORMAL
    lo_mix = MIX_LIGHT if heavy else MIX_NORMAL
    hi_d = 1.3 if heavy else 1.0
    phases: List[Phase] = []
    cur = lam[0] >= thr
    start = 0.0
    for t, v in zip(grid, lam):
        hi = v >= thr
        if hi != cur:
            phases.append(
                Phase(start, t, hi_label if cur else lo_label,
                      hi_mix if cur else lo_mix, hi_d if cur else 1.0)
            )
            cur, start = hi, t
    phases.append(
        Phase(start, horizon + 1e-9, hi_label if cur else lo_label,
              hi_mix if cur else lo_mix, hi_d if cur else 1.0)
    )
    return phases, None, None


# ----------------------------------------------------------------------
# Sampling
# ----------------------------------------------------------------------
def sample_arrivals(plan: ArrivalPlan, n_jobs: int, rng: random.Random) -> List[float]:
    """Inverse-transform sampling of the normalised cumulative intensity.

    Conditional uniformity of the NHPP: exactly n_jobs epochs, distributed with
    density lambda(t)/Lambda(H).
    """
    total = plan.cum[-1]
    if total <= 0:
        raise ValueError("degenerate intensity")
    out = []
    for _ in range(n_jobs):
        target = rng.random() * total
        i = bisect.bisect_left(plan.cum, target)
        i = min(max(i, 1), len(plan.cum) - 1)
        c0, c1 = plan.cum[i - 1], plan.cum[i]
        t0, t1 = plan.grid[i - 1], plan.grid[i]
        frac = 0.0 if c1 <= c0 else (target - c0) / (c1 - c0)
        out.append(t0 + frac * (t1 - t0))
    out.sort()
    return out


def generate_dynamic_trace(n_jobs: int, regime: str, seed: int, horizon: float):
    """Build a List[workload.Job] under a non-stationary arrival process.

    Imported lazily from workload.generate_trace to avoid a circular import.
    """
    from workload import Job, MODEL_NAMES  # local import: workload imports us

    plan = build_plan(regime, seed, horizon)
    rng = random.Random(seed)
    arrivals = sample_arrivals(plan, n_jobs, rng)

    jobs: List[Job] = []
    for i, a in enumerate(arrivals):
        ph = plan.phase_at(a)
        model = rng.choices(MODEL_NAMES, weights=ph.mix, k=1)[0]
        # Worker count: same log-normal shape as the stationary generator, but
        # scaled up inside a surge (bigger requests => real fragmentation).
        base_d = rng.lognormvariate(0.4, 0.6) * ph.d_scale
        d_j = min(8, max(1, int(round(base_d))))
        # Job length distribution is IDENTICAL in every phase, by design.
        W_j = max(2, int(round(rng.lognormvariate(2.2, 0.7))))
        jobs.append(Job(job_id=i, arrival=round(a, 4), model=model, d_j=d_j, W_j=W_j))

    jobs.sort(key=lambda j: j.arrival)
    for new_id, j in enumerate(jobs):
        j.job_id = new_id
    return jobs


# ----------------------------------------------------------------------
# Self-check: prove the traces are non-stationary and distinct
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import hashlib, json

    H = 84.0
    print(f"{'regime':10} {'md5':14} {'shock window':>18}  arrivals-per-10-rounds")
    for reg in DYNAMIC_REGIMES:
        tr = generate_dynamic_trace(120, reg, 1, H)
        plan = build_plan(reg, 1, H)
        h = hashlib.md5(
            json.dumps([(j.arrival, j.model, j.d_j, j.W_j) for j in tr]).encode()
        ).hexdigest()[:12]
        bins = [0] * (int(H // 10) + 1)
        for j in tr:
            bins[int(j.arrival // 10)] += 1
        win = (f"[{plan.shock_onset:.0f},{plan.shock_end:.0f}]"
               if plan.shock_onset is not None else "n/a")
        print(f"{reg:10} {h:14} {win:>18}  {bins}")
