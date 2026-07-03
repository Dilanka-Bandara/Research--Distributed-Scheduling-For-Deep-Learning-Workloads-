"""
fft_baseline.py  (FAST variant)
-------------------------------
Faithful implementation of the FFT scheduler (Mo et al., ICS '25), identical in
formulation to the reference simulation. The ONLY difference is the solver:
the per-round integer program is solved in-process by `fast_solver` (SciPy
HiGHS, with an LP-relaxation fast path) instead of shelling out to the CBC
binary. Profiling showed the CBC subprocess spawn/wait was ~87% of total
runtime; removing it is where the speedup comes from. The objective, the
constraints, the fairness update rho_j(t), the switching penalty, the
work-conservation reward, the membership-based solve caching, and the MEASURED
solve-cost model are all unchanged, so results match the reference simulation.

Per-round program (identical to the reference; Eq. references are to the paper):
    minimise  sum_{j,i} [ -(base + fairness + continuity) + 0.1*(phi+switch) ] x[j,i]
    s.t.      sum_i x[j,i] <= 1                  (one GPU type per job, Eq.1 left)
              sum_j d_j x[j,i] <= cap_i          (per-type capacity, Eq.1 right)
              x[j,i] in {0,1}; x=0 if infeasible

where
    phi_j^i(t) = (t-a_j) theta/W_j + d_j W_j/theta   (JCT term, Eq.3)
    switch     = migration penalty if changing GPU type (Eq. s_j^i)
    fairness   = rho_j(t) * theta                     (Eq.5 compensation)
    base       = strong work-conservation reward (keeps GPUs busy, Eq.7 intent)

Admission remains SYNCHRONOUS: a job is only placed when a round solve runs.
That is the centralised bottleneck the new architecture attacks; we keep it.
"""

from __future__ import annotations
from typing import Dict, List
import json
import os

from workload import Job, GPU_TYPES
from fast_solver import solve_assignment


# Measured solve-cost model fitted from real CBC timings (unchanged).
_FIT_PATH = os.path.join(os.path.dirname(__file__), "solve_cost_fit.json")
try:
    with open(_FIT_PATH) as _f:
        _FIT = json.load(_f)
    _SOLVE_BASE_MS = _FIT["base_ms"]
    _SOLVE_SLOPE_MS = _FIT["slope_ms_per_job"]
except Exception:
    _SOLVE_BASE_MS, _SOLVE_SLOPE_MS = 27.73, 0.0930


def measured_solve_time_rounds(n_active: int, round_seconds: float = 300.0) -> float:
    """Measured per-round solve time as a FRACTION of a scheduling round.

    base + slope*n_active gives milliseconds (from real CBC measurements). This
    models the centralised-admission cost FFT pays every synchronous round. It
    is a *modelled* cost charged to the simulated clock; it is intentionally
    independent of how fast THIS process actually solves the ILP, so swapping
    CBC for the in-process solver does not change the reported latency numbers.
    """
    ms = _SOLVE_BASE_MS + _SOLVE_SLOPE_MS * max(0, n_active)
    return (ms / 1000.0) / round_seconds


class FFTScheduler:
    def __init__(
        self,
        cluster: Dict[str, int],
        round_len: float = 1.0,
        mu: float = 1.0,              # 1.0 = paper-exact mu_j(t); knob for sensitivity
        switch_penalty: float = 0.5,  # weight on migration cost
        wc_reward: float = 0.05,      # (kept for signature compatibility)
        solver_time_per_job: float = 0.02,  # (kept for signature compatibility)
    ):
        self.cluster = dict(cluster)
        self.round_len = round_len
        self.mu = mu
        self.switch_penalty = switch_penalty
        self.wc_reward = wc_reward
        self.solver_time_per_job = solver_time_per_job

        self.rho: Dict[int, float] = {}
        self.tau: Dict[int, float] = {}
        self.last_solve_wall = 0.0
        self.round_seconds = 300.0
        self._cache_sig = None
        self._cache_alloc: Dict[int, str] = {}
        self._resolve_every = 3
        self._rounds_since_solve = 0

        self._gpu_types = list(GPU_TYPES)

    # ------------------------------------------------------------------
    def _phi(self, job: Job, gpu: str, t: float) -> float:
        """JCT cost term phi_j^i(t) from Eq. (3)."""
        th = job.throughput_on(gpu)
        if th <= 0:
            return 1e6
        progress_term = (t - job.arrival) * th / max(1e-6, job.W_j)
        demand_term = job.d_j * job.W_j / th
        return progress_term + demand_term

    def _switch_cost(self, job: Job, gpu: str) -> float:
        """s_j^i(t): penalty for moving to a different GPU type."""
        if job.current_gpu is None or job.current_gpu == gpu:
            return 0.0
        return self.switch_penalty * (job.state_gb / 10.0)

    def _update_fairness(self, active: List[Job], t: float) -> None:
        """Update rho_j(t) per Eq. (5), with the paper's exact dynamic
        coefficient mu_j(t) = (t - a_j) / (tau_j - a_j). Our `tau` variable is
        already the duration (tau_j - a_j): the completion time of job j on a
        1/|A(t)| share of resources, re-estimated as jobs arrive/complete.
        `self.mu` is a multiplier kept for sensitivity studies; 1.0 = paper-exact.
        """
        n = max(1, len(active))
        for job in active:
            if job.job_id not in self.rho:
                self.rho[job.job_id] = 0.0
            best_th = max(job.theta.values()) if job.theta else 1.0
            tau = job.W_j / max(1e-6, best_th / n)   # duration under 1/N share
            self.tau[job.job_id] = tau
            fair_rate = job.W_j / max(1e-6, tau)     # W_j / (tau_j - a_j)
            done_this = job.throughput_on(job.current_gpu) if job.current_gpu else 0.0
            mu_t = self.mu * (t - job.arrival) / max(1e-6, tau)  # Eq. mu_j(t)
            self.rho[job.job_id] = max(
                0.0, self.rho[job.job_id] + mu_t * (fair_rate - done_this)
            )

    # ------------------------------------------------------------------
    def schedule_round(self, active: List[Job], t: float) -> Dict[int, str]:
        """Solve the per-round ILP. Returns {job_id: gpu_type}."""
        active = [j for j in active if not j.is_done()]
        if not active:
            self.last_solve_wall = 0.0
            return {}

        # Membership-based solve caching (unchanged from the reference).
        sig = tuple(sorted(j.job_id for j in active))
        self._rounds_since_solve += 1
        if (sig == self._cache_sig
                and self._rounds_since_solve < self._resolve_every):
            self.last_solve_wall = 0.0
            return {jid: g for jid, g in self._cache_alloc.items() if jid in sig}

        self._update_fairness(active, t)

        # Build the cost dict (identical objective to the reference).
        costs: Dict[tuple, float] = {}
        job_demands: Dict[int, int] = {}
        job_ids: List[int] = []
        for job in active:
            job_ids.append(job.job_id)
            job_demands[job.job_id] = job.d_j
            for gpu in self._gpu_types:
                th = job.throughput_on(gpu)
                if th <= 0:
                    continue  # infeasible placement -> no variable (x=0)
                priority = self._phi(job, gpu, t) + self._switch_cost(job, gpu)
                fairness_reward = self.rho.get(job.job_id, 0.0) * th
                continuity = 0.0
                if job.current_gpu == gpu and 0 < job.epochs_done < job.W_j:
                    continuity = 0.5 * th
                base_reward = 100.0
                costs[(job.job_id, gpu)] = (
                    -(base_reward + fairness_reward + continuity) + 0.1 * priority
                )

        # Solve EXACTLY, in-process (same optimum as CBC).
        alloc = solve_assignment(
            job_ids, job_demands, self._gpu_types, self.cluster, costs
        )

        # Charge the MEASURED solve cost to the simulated clock (unchanged).
        self.last_solve_wall = measured_solve_time_rounds(
            len(active), self.round_seconds
        )

        self._cache_sig = sig
        self._cache_alloc = dict(alloc)
        self._rounds_since_solve = 0
        return alloc
