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

from workload import (Job, GPU_TYPES, GPU_CAPABILITY, profile_job,
                      migration_stall_rounds, profiling_stall_rounds)
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
        # Mechanism E: starvation-triggered preemption. Restores the FFT
        # paper's Theorem-4.2 property (every job executes within bounded
        # time of arrival) which non-preemptible Fast-Path locks broke.
        # Only fires for jobs that have NEVER executed and have waited
        # >= starve_after rounds, so it is burst-adaptive and cannot livelock
        # (victims have already executed, hence can never trigger it back).
        preempt_starved: bool = True,
        starve_after: float = 1.0,     # rounds waited (never run) before firing; 1.0 won the sweep at 36 GPUs — re-sweep at your scale
        max_evict_per_round: int = 4,  # thrash guard
        # Opt 1: among nodes within the dS threshold, place on the HIGHEST-
        # throughput one instead of the closest-dS one. SRSF-consistent with
        # FFT Eq. 3 (jobs finish fastest where theta is highest); admission
        # stays O(1) and the routing decision (fast vs slow) is unchanged.
        jct_aware_fast: bool = True,
        # Opt 2: opportunistic (benefit-gated) corrective migration, per the
        # paper's Sec. 2.2: only migrate an already-running job if the time
        # saved (remaining/theta_old - remaining/theta_new) exceeds the
        # state-transfer stall by benefit_margin.
        benefit_gated: bool = True,
        benefit_margin: float = 1.0,
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
        self.preempt_starved = preempt_starved
        self.starve_after = starve_after
        self.max_evict_per_round = max_evict_per_round
        self.preemptions = 0
        self.jct_aware_fast = jct_aware_fast
        self.benefit_gated = benefit_gated
        self.benefit_margin = benefit_margin
        self.migrations_skipped = 0   # blocked by the opportunistic gate
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
        self.handshake_rejects = 0
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

    def _best_node(self, job: Job, t: float):
        """Return (gpu_type, dS, fast_candidates) — fast_candidates lists every
        feasible node within the dS threshold as (throughput, gpu)."""
        s_job = self._s_job(job, t)
        best_gpu, best_ds = None, float("inf")
        fast_candidates = []
        for gpu in GPU_TYPES:
            if job.throughput_on(gpu) <= 0:
                continue  # won't fit on this type
            if self.cluster[gpu] - self.used[gpu] < job.d_j:
                continue  # no room
            ds = abs(self._s_node(gpu) - s_job)
            if ds < best_ds:
                best_gpu, best_ds = gpu, ds
            if ds <= self.threshold:
                fast_candidates.append((job.throughput_on(gpu), gpu))
        return best_gpu, best_ds, fast_candidates

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
        cache_hit = job.model in self.cache
        if cache_hit:
            job.theta = dict(self.cache[job.model])
            job.mem_gb = profile_lookup_mem(job)
            job.state_gb = profile_lookup_state(job)
        else:
            profile_job(job)                 # micro-profile (first time only)
            self.cache[job.model] = dict(job.theta)
        job.cache_hit = cache_hit

        fast_cap = int((1.0 - self.slow_reserve) * self.total_capacity)
        best_gpu, ds, fast_candidates = self._best_node(job, t)

        # Fast Path: good match, room available, and under the reserve cap.
        # Spec: Fast-Path jobs are profiled silently on-the-fly -> no stall.
        if (
            fast_candidates
            and self.fast_used + job.d_j <= fast_cap
        ):
            if self.jct_aware_fast:
                # Opt 1: among within-threshold nodes, finish fastest
                # (SRSF-consistent with FFT Eq. 3), not closest-dS.
                target = max(fast_candidates)[1]
            else:
                target = best_gpu
            self._place(job, target, t, fast=True)
            return "fast"

        # Slow Path: on a cache MISS the job pays the strict 60 s micro-profiling
        # window (spec Sec. 3.3) before it can train; a HIT skips it entirely.
        if not cache_hit:
            p = profiling_stall_rounds()
            job.stall += p
            job.profile_time_lost += p
        self.global_queue.append(job)
        return "slow"

    def _place(self, job: Job, gpu: str, t: float, fast: bool) -> None:
        if job.current_gpu == gpu:
            pass  # already here, no migration
        else:
            if job.current_gpu is not None:
                self.used[job.current_gpu] -= job.d_j
                job.migrations += 1
                # Real migration cost: the job stalls for the state-transfer
                # time (paper Sec. 2.2: large training states over the LAN).
                m = migration_stall_rounds(job.state_gb)
                job.stall += m
                job.migration_time_lost += m
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
            # capacity check before committing (the decentralised handshake:
            # target verifies hardware availability before accepting migration)
            free = self.cluster[target] - self.used[target]
            if job.current_gpu == target:
                continue
            if free >= job.d_j:
                # Opt 2: opportunistic-migration gate (paper Sec. 2.2). For a
                # job ALREADY RUNNING, migrate only if the time saved on the
                # new type repays the state-transfer stall. Jobs coming from
                # the queue (current_gpu is None) are placements, not
                # migrations, and always pass.
                if (self.benefit_gated and job.current_gpu is not None):
                    th_old = job.throughput_on(job.current_gpu)
                    th_new = job.throughput_on(target)
                    rem = job.remaining_epochs()
                    saved = (rem / max(1e-9, th_old)) - (rem / max(1e-9, th_new))
                    stall = migration_stall_rounds(job.state_gb)
                    if saved <= stall * self.benefit_margin:
                        self.migrations_skipped += 1
                        continue
                # handshake OK -> commit placement/migration
                self._place(job, target, t, fast=False)
                if job in self.global_queue:
                    self.global_queue.remove(job)
            else:
                # handshake REJECTED: job stays where it is / stays queued.
                self.handshake_rejects += 1

        # ---- Mechanism E: starvation-triggered preemption ----
        # For queued jobs that have NEVER executed and have waited too long,
        # evict running fast-path jobs to make room. Mirrors the FFT paper's
        # fairness guarantee (Thm 4.2: each job runs within bounded time of
        # arrival), which FFT achieves because everything is preemptible each
        # round. Victims keep their progress and return to the global queue
        # for re-placement (same-type re-placement later carries no state-
        # transfer stall, matching FFT's own treatment of within-type churn).
        if self.preempt_starved:
            evicted = 0
            for job in list(self.global_queue):
                if evicted >= self.max_evict_per_round:
                    break
                if job.is_done() or job.first_exec_time is not None:
                    continue
                if (t - job.arrival) < self.starve_after:
                    continue
                feas = [(job.throughput_on(g), g) for g in self.cluster
                        if job.throughput_on(g) > 0]
                if not feas:
                    continue
                feas.sort(reverse=True)
                for _, g in feas:
                    victims = [v for v in active_fast
                               if v.current_gpu == g and not v.is_done()
                               and v.first_exec_time is not None]
                    # evict longest-remaining victims first (SRSF-consistent)
                    victims.sort(key=lambda v: -(v.W_j - v.epochs_done))
                    freed = self.cluster[g] - self.used[g]
                    picked = []
                    for v in victims:
                        if freed >= job.d_j:
                            break
                        picked.append(v); freed += v.d_j
                    if freed >= job.d_j:
                        for v in picked:
                            self._release(v)
                            v.route = 'slow'
                            if v not in self.global_queue:
                                self.global_queue.append(v)
                            self.preemptions += 1
                        self._place(job, g, t, fast=False)
                        if job in self.global_queue:
                            self.global_queue.remove(job)
                        evicted += 1
                        break
                # if no type could be freed, the job keeps waiting


# Helper lookups so cache hits still recover memory/state without reprofiling.
def profile_lookup_mem(job: Job) -> float:
    from workload import MODEL_ZOO
    return MODEL_ZOO[job.model]["mem_gb"]


def profile_lookup_state(job: Job) -> float:
    from workload import MODEL_ZOO
    return MODEL_ZOO[job.model]["state_gb"]
