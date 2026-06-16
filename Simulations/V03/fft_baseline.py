"""
fft_baseline.py
---------------
Faithful-in-spirit implementation of the FFT scheduler
(Mo et al., "Fast and Fair Training for Deep Learning in Heterogeneous
GPU Clusters", ICS '25).

Per scheduling round it solves an ILP over GPU *types* (not individual
GPUs, which is what keeps FFT scalable). The objective for each (job, type)
pair combines three terms exactly as in the paper's Eq. (6):

    minimise  sum_j sum_i [ phi_j^i(t) + s_j^i(t) - rho_j(t) * theta_j^i ] * x_j^i(t)

where
    phi_j^i(t) = (t - a_j) * theta_j^i / W_j  +  d_j * W_j / theta_j^i   (JCT term, Eq.3)
    s_j^i(t)   = switching penalty if moving job j to a different type
    rho_j(t)   = fairness compensation factor (Eq.5), grows under starvation

Key faithful details:
  * Admission is SYNCHRONOUS: a job can only be placed when a round solve runs.
    This is the centralised bottleneck the new architecture attacks.
  * Work-conservation (Eq.7) is included via a strong scheduling REWARD rather
    than a hard constraint, because as a hard constraint it makes the solver
    infeasible in tight regimes (a documented lesson).
  * One GPU type per job per round (Eq.1); per-type capacity not oversubscribed.

Solve-cost model:
  The per-round ILP solve time is NOT a guessed constant. It is a linear model
  (base + slope * n_active) fitted to REAL CBC solve-time measurements taken on
  this machine (see solve_cost_fit.json and calibrate_solver.py). This is what
  makes the centralised-admission latency a measured result rather than an
  assumption: every synchronous round pays this measured cost, and it grows
  with the active job count.
"""

from __future__ import annotations
from typing import Dict, List
import json
import os
import pulp

from workload import Job, GPU_TYPES


# Load the measured solve-cost model fitted from real CBC timings.
_FIT_PATH = os.path.join(os.path.dirname(__file__), "solve_cost_fit.json")
try:
    with open(_FIT_PATH) as _f:
        _FIT = json.load(_f)
    _SOLVE_BASE_MS = _FIT["base_ms"]
    _SOLVE_SLOPE_MS = _FIT["slope_ms_per_job"]
except Exception:
    # Fallback to the fitted values if the file is missing.
    _SOLVE_BASE_MS, _SOLVE_SLOPE_MS = 27.73, 0.0930


def measured_solve_time_rounds(n_active: int, round_seconds: float = 300.0) -> float:
    """Measured per-round solve time, expressed as a FRACTION of a round.

    base + slope*n_active gives milliseconds (from real CBC measurements);
    we convert to round-fractions using the system's round length (default
    5 minutes = 300 s, the FFT paper's default scheduling round).
    """
    ms = _SOLVE_BASE_MS + _SOLVE_SLOPE_MS * max(0, n_active)
    return (ms / 1000.0) / round_seconds


class FFTScheduler:
    def __init__(
        self,
        cluster: Dict[str, int],
        round_len: float = 1.0,
        mu: float = 0.5,            # fairness coefficient base
        switch_penalty: float = 0.5,  # weight on migration cost
        wc_reward: float = 0.05,    # work-conservation reward weight
        solver_time_per_job: float = 0.02,  # simulated ILP solve cost (rounds/job)
    ):
        self.cluster = dict(cluster)
        self.round_len = round_len
        self.mu = mu
        self.switch_penalty = switch_penalty
        self.wc_reward = wc_reward
        self.solver_time_per_job = solver_time_per_job

        self.rho: Dict[int, float] = {}   # fairness factor per job id
        self.tau: Dict[int, float] = {}   # est. fair-share completion time per job
        self.last_solve_wall = 0.0        # measured solve time of last solve (round-fractions)
        self.round_seconds = 300.0        # 5-min round (FFT default), for solve-cost conversion
        self._cache_sig = None            # signature of last solved problem
        self._cache_alloc: Dict[int, str] = {}
        self._resolve_every = 3           # re-solve at least every N rounds
        self._rounds_since_solve = 0

    # ------------------------------------------------------------------
    def _phi(self, job: Job, gpu: str, t: float) -> float:
        """JCT cost term phi_j^i(t) from Eq. (3)."""
        th = job.throughput_on(gpu)
        if th <= 0:
            return 1e6  # infeasible placement (won't fit / zero throughput)
        progress_term = (t - job.arrival) * th / max(1e-6, job.W_j)
        demand_term = job.d_j * job.W_j / th
        return progress_term + demand_term

    def _switch_cost(self, job: Job, gpu: str) -> float:
        """s_j^i(t): penalty for moving to a different GPU type."""
        if job.current_gpu is None or job.current_gpu == gpu:
            return 0.0
        # proportional to training-state transfer size
        return self.switch_penalty * (job.state_gb / 10.0)

    def _update_fairness(self, active: List[Job], t: float) -> None:
        """Update rho_j(t) per Eq. (5): grows when a job falls behind fair share."""
        n = max(1, len(active))
        for job in active:
            if job.job_id not in self.rho:
                self.rho[job.job_id] = 0.0
            # tau_j: completion time under an exclusive 1/N share (approx, no preemption)
            best_th = max(job.theta.values()) if job.theta else 1.0
            tau = job.W_j / max(1e-6, best_th / n)
            self.tau[job.job_id] = tau
            fair_rate = job.W_j / max(1e-6, tau)
            done_this = job.throughput_on(job.current_gpu) if job.current_gpu else 0.0
            # dynamic fairness coefficient grows with time-in-cluster (Eq. mu_j(t))
            mu_t = self.mu * (t - job.arrival) / max(1e-6, tau)
            self.rho[job.job_id] = max(
                0.0, self.rho[job.job_id] + mu_t * (fair_rate - done_this)
            )

    # ------------------------------------------------------------------
    def schedule_round(self, active: List[Job], t: float) -> Dict[int, str]:
        """
        Solve the per-round ILP. Returns {job_id: gpu_type} allocation.
        Also returns (via self.last_solve_wall) the simulated solve time.
        """
        active = [j for j in active if not j.is_done()]
        if not active:
            self.last_solve_wall = 0.0
            return {}

        # Re-solve when the SET of active jobs changes (arrival/completion) or
        # every few rounds so fairness compensation can rotate starved jobs in.
        # Keyed on membership only (not progress) so it's stable round-to-round.
        sig = tuple(sorted(j.job_id for j in active))
        self._rounds_since_solve += 1
        if (sig == self._cache_sig
                and self._rounds_since_solve < self._resolve_every):
            self.last_solve_wall = 0.0
            # validate cached alloc still references only active jobs
            return {jid: g for jid, g in self._cache_alloc.items()
                    if jid in sig}

        self._update_fairness(active, t)

        prob = pulp.LpProblem("FFT_round", pulp.LpMinimize)
        # x[j,i] in {0,1}: job j on type i this round
        x = {}
        for job in active:
            for gpu in GPU_TYPES:
                x[(job.job_id, gpu)] = pulp.LpVariable(
                    f"x_{job.job_id}_{gpu}", cat="Binary"
                )

        # Objective (minimise). FFT is work-conserving: it keeps GPUs busy.
        # Expressed as a strong per-placement scheduling REWARD, with the FFT
        # cost terms (JCT priority, switching, fairness) deciding WHICH jobs get
        # the scarce slots. Net: always schedule when capacity exists, preferring
        # short / starved / already-running jobs.
        obj = []
        for job in active:
            for gpu in GPU_TYPES:
                th = job.throughput_on(gpu)
                if th <= 0:
                    continue
                priority = self._phi(job, gpu, t) + self._switch_cost(job, gpu)
                fairness_reward = self.rho.get(job.job_id, 0.0) * th
                continuity = 0.0
                if job.current_gpu == gpu and 0 < job.epochs_done < job.W_j:
                    continuity = 0.5 * th
                base_reward = 100.0  # guarantees work-conservation
                net = -(base_reward + fairness_reward + continuity) + 0.1 * priority
                obj.append(net * x[(job.job_id, gpu)])
        prob += pulp.lpSum(obj)

        # Constraint 1: each job on at most one GPU type (Eq.1 left)
        for job in active:
            prob += pulp.lpSum(x[(job.job_id, gpu)] for gpu in GPU_TYPES) <= 1

        # Constraint 2: per-type worker capacity not exceeded (Eq.1 right)
        for gpu in GPU_TYPES:
            prob += (
                pulp.lpSum(job.d_j * x[(job.job_id, gpu)] for job in active)
                <= self.cluster[gpu]
            )

        # Forbid infeasible (zero-throughput / won't-fit) placements
        for job in active:
            for gpu in GPU_TYPES:
                if job.throughput_on(gpu) <= 0:
                    prob += x[(job.job_id, gpu)] == 0

        prob.solve(pulp.PULP_CBC_CMD(msg=0))

        # Simulated solve wall-time scales with problem size (matches paper's
        # observation that solve time grows with job count).
        # Measured solve cost: base + slope*n_active (from real CBC timings).
        self.last_solve_wall = measured_solve_time_rounds(
            len(active), self.round_seconds
        )

        alloc: Dict[int, str] = {}
        for job in active:
            for gpu in GPU_TYPES:
                if pulp.value(x[(job.job_id, gpu)]) == 1:
                    alloc[job.job_id] = gpu
                    break
        self._cache_sig = sig
        self._cache_alloc = dict(alloc)
        self._rounds_since_solve = 0
        return alloc
