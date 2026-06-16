"""
smart_scheduler.py
------------------
The upgraded Enhanced Decentralised Score-Based Scheduler.

Decouples admission from global optimisation:

  * Fairness-aware scoring dispatcher (O(1)):
        S_job  = alpha*d_j + beta*W_j + rho_age   (rho_age = NEW ageing term)
        S_node = gamma*C_n + delta*A_n
        dS     = |S_node - S_job|
  * Dual-path routing on a TUNABLE threshold (the Pareto knob).
  * Fast Path: instant local placement, capped at (1 - SLOW_RESERVE) of cluster.
  * Slow Path: cache check -> (micro-profile) -> global queue -> background FFT.
  * Hybrid event-driven FFT solver in the background (reuses FFTScheduler).
  * Corrective migration: background solver may re-place fast-path jobs.

All four upgrade dials are exposed as constructor parameters so they can be swept.
"""

from __future__ import annotations
from typing import Dict, List, Tuple
from collections import deque

from workload import Job, GPU_TYPES, GPU_CAPABILITY, profile_job
from fft_baseline import FFTScheduler


class SmartScheduler:
    def __init__(
        self,
        cluster: Dict[str, int],
        # --- dispatcher score weights (the alpha/beta/gamma/delta to sweep) ---
        alpha: float = 1.0,   # weight on requested workers d_j
        beta: float = 0.5,    # weight on epochs W_j
        gamma: float = 1.0,   # weight on node capability C_n
        delta: float = 1.0,   # weight on node availability A_n
        # --- the four upgrade dials ---
        threshold: float = 0.15,       # ΔS routing threshold (Pareto knob)
        slow_reserve: float = 0.25,    # fraction reserved for Slow Path
        rho_age_rate: float = 0.3,     # NEW: fairness ageing growth rate
        corrective: bool = True,       # NEW: allow background re-placement
        # --- background solver config ---
        round_len: float = 1.0,
        dispatch_cost: float = 0.00002,  # simulated O(1) admission cost (s/job)
    ):
        self.cluster = dict(cluster)
        self.total_capacity = sum(cluster.values())
        self.alpha, self.beta = alpha, beta
        self.gamma, self.delta = gamma, delta
        self.threshold = threshold
        self.slow_reserve = slow_reserve
        self.rho_age_rate = rho_age_rate
        self.corrective = corrective
        self.dispatch_cost = dispatch_cost

        # Background FFT brain (reused, identical maths to the baseline).
        self.brain = FFTScheduler(cluster, round_len=round_len)

        # Live allocation state: gpu_type -> used worker count.
        self.used: Dict[str, int] = {g: 0 for g in GPU_TYPES}
        # Fast-path usage counter, for the reserve cap.
        self.fast_used: int = 0
        self.global_queue: deque[Job] = deque()
        self.cache: Dict[str, Dict[str, float]] = {}  # model -> theta (historical cache)

        # bookkeeping
        self.last_dispatch_cost = 0.0
        self.last_solve_wall = 0.0

    # ------------------------------------------------------------------
    # Dispatcher scoring
    #
    # Both scores are normalised to a comparable ~[0,1] range so that the
    # ΔS threshold is meaningful. S_job rises with job heaviness; S_node
    # rises with node capability AND current free capacity. A heavy job
    # therefore matches a capable, free node (small ΔS) and takes the Fast
    # Path; a heavy job arriving at a busy cluster mismatches and is routed
    # to the Slow Path for global optimisation.
    # ------------------------------------------------------------------
    _D_MAX = 8.0     # max workers, for normalisation
    _W_MAX = 40.0    # typical max epochs, for normalisation
    _CAP_MAX = 2.7   # max GPU capability (A10)

    def _s_job(self, job: Job, t: float) -> float:
        wait = 0.0 if job.arrival is None else max(0.0, t - job.arrival)
        d_norm = job.d_j / self._D_MAX
        w_norm = min(1.0, job.W_j / self._W_MAX)
        rho_age = self.rho_age_rate * (wait / 50.0)   # NEW fairness ageing term
        return self.alpha * d_norm + self.beta * w_norm + rho_age

    def _s_node(self, gpu: str) -> float:
        cap = GPU_CAPABILITY[gpu] / self._CAP_MAX
        free = self.cluster[gpu] - self.used[gpu]
        avail = free / max(1, self.cluster[gpu])      # A_n in [0,1]
        # Normalised so a capable, free node scores ~ (gamma+delta) scale.
        return (self.gamma * cap + self.delta * avail) / (self.gamma + self.delta)

    def _best_node(self, job: Job, t: float) -> Tuple[str, float]:
        """Return (gpu_type, dS) of the closest-matching node with room."""
        s_job = self._s_job(job, t)
        best_gpu, best_ds = None, float("inf")
        for gpu in GPU_TYPES:
            if job.throughput_on(gpu) <= 0:
                continue  # won't fit on this type
            if self.cluster[gpu] - self.used[gpu] < job.d_j:
                continue  # no room
            ds = abs(self._s_node(gpu) - s_job)
            if ds < best_ds:
                best_gpu, best_ds = gpu, ds
        return best_gpu, best_ds

    # ------------------------------------------------------------------
    # Admission (called by the engine when a job arrives)
    # ------------------------------------------------------------------
    def admit(self, job: Job, t: float) -> str:
        """
        Route an arriving job. Returns "fast", "slow", or "queue".
        Fast Path places immediately. Slow Path profiles and queues.
        """
        self.last_dispatch_cost = self.dispatch_cost  # O(1) cost, independent of N

        # Historical cache check (Slow Path will need theta; Fast needs it too).
        if job.model in self.cache:
            job.theta = dict(self.cache[job.model])
            job.mem_gb = profile_lookup_mem(job)
            job.state_gb = profile_lookup_state(job)
        else:
            profile_job(job)                 # micro-profile (first time only)
            self.cache[job.model] = dict(job.theta)

        fast_cap = int((1.0 - self.slow_reserve) * self.total_capacity)
        best_gpu, ds = self._best_node(job, t)

        # Fast Path: good match, room available, and under the reserve cap.
        if (
            best_gpu is not None
            and ds <= self.threshold
            and self.fast_used + job.d_j <= fast_cap
        ):
            self._place(job, best_gpu, t, fast=True)
            return "fast"

        # Slow Path: park in the global queue (event trigger wakes the brain).
        self.global_queue.append(job)
        return "slow"

    def _place(self, job: Job, gpu: str, t: float, fast: bool) -> None:
        if job.current_gpu == gpu:
            pass  # already here, no migration
        else:
            if job.current_gpu is not None:
                self.used[job.current_gpu] -= job.d_j
                job.migrations += 1
            job.current_gpu = gpu
        self.used[gpu] += job.d_j
        if job.admit_time is None:
            job.admit_time = t
        if fast:
            self.fast_used += job.d_j

    def _release(self, job: Job) -> None:
        if job.current_gpu is not None:
            self.used[job.current_gpu] -= job.d_j
            job.current_gpu = None

    # ------------------------------------------------------------------
    # Background brain: event/interval triggered solve over queued jobs
    # (+ corrective re-placement of fast-path jobs when enabled).
    # ------------------------------------------------------------------
    def run_brain(self, active_fast: List[Job], t: float) -> None:
        """
        Clears the global queue via the FFT ILP, and optionally re-places
        fast-path jobs that the global optimum would place better.
        """
        queued = [j for j in self.global_queue if not j.is_done()]
        candidates = list(queued)
        if self.corrective:
            candidates += [j for j in active_fast if not j.is_done()]

        if not candidates:
            self.last_solve_wall = 0.0
            return

        # Temporarily free queued jobs' (zero) usage; fast jobs keep their slot
        # until reassigned. Solve over the candidate set.
        alloc = self.brain.schedule_round(candidates, t)
        self.last_solve_wall = self.brain.last_solve_wall

        for job in candidates:
            target = alloc.get(job.job_id)
            if target is None:
                continue
            # capacity check before committing (the decentralised handshake)
            free = self.cluster[target] - self.used[target]
            if job.current_gpu == target:
                continue
            need = job.d_j - (0 if job.current_gpu is None else 0)
            if free >= job.d_j or job.current_gpu == target:
                # handshake OK -> commit placement/migration
                was_fast_slot = job in active_fast
                self._place(job, target, t, fast=False)
                if job in self.global_queue:
                    self.global_queue.remove(job)
            # else: handshake fails, job stays where it is / stays queued


# Helper lookups so cache hits still recover memory/state without reprofiling.
def profile_lookup_mem(job: Job) -> float:
    from workload import MODEL_ZOO
    return MODEL_ZOO[job.model]["mem_gb"]


def profile_lookup_state(job: Job) -> float:
    from workload import MODEL_ZOO
    return MODEL_ZOO[job.model]["state_gb"]
