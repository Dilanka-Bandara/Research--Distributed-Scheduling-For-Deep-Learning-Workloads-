"""
FFT baseline scheduler (Mo et al., ICS '25), faithful re-implementation.

Per round t, FFT solves an Integer Linear Program over the binary allocation
matrix x_j^i(t) in {0,1}  (job j on device type i this round), minimizing the
three-term cost from Eq. (6):

    min  sum_j sum_i  [ phi_j^i(t) + s_j^i(t) - rho_j(t)*theta_j^i ] * x_j^i(t)

where
    phi_j^i(t) = (t - a_j)*theta_j^i / W_j  +  d_j*W_j / theta_j^i   (JCT term, Eq.3)
    s_j^i(t)   = migration penalty if type i differs from job's current type
    rho_j(t)   = fairness compensation factor (Eq.5), grows while a job starves

subject to:
    sum_i x_j^i <= 1            (a job runs on one type)
    sum_j d_j*x_j^i <= Y_i      (no type oversubscribed)
    work-conserving (encouraged via the objective; capacity is the hard limit)

THE CENTRALIZATION COST: this ILP is rebuilt and solved over *all* admitted-but-
unfinished jobs every round. As the active set grows during a burst, the variable
count = M * |A(t)| grows and solve time climbs -- this is the bottleneck your
architecture removes from the admission path.
"""

import time
import numpy as np
import cvxpy as cp

from workload import GPU_TYPES, GPU_COUNT, M, migration_rounds


class FFTScheduler:
    name = "FFT (centralized ILP)"

    def __init__(self):
        self.Y = np.array([GPU_COUNT[g] for g in GPU_TYPES])   # workers per type
        self.solve_times = []        # seconds spent in the solver each round (the bottleneck)
        self.admission_lat = []      # rounds between arrival and first scheduling decision

    def _solve_round(self, active, t):
        """Build and solve the ILP for the current active job set."""
        N = len(active)
        if N == 0:
            return None

        x = cp.Variable((N, M), boolean=True)

        # cost coefficients c[j,i]
        C = np.zeros((N, M))
        for j_idx, job in enumerate(active):
            theta = job.theta
            phi = (t - job.arrival) * theta / job.epochs + (job.workers * job.epochs) / theta
            # switching penalty: pay migration cost if assigned type != current type
            s = np.zeros(M)
            mig = migration_rounds(job.state_gb)
            for i in range(M):
                if job.current_type is not None and i != job.current_type:
                    s[i] = mig
            rho = getattr(job, "rho", 0.0)
            C[j_idx] = phi + s - rho * theta

        constraints = []
        # each job on at most one type
        constraints.append(cp.sum(x, axis=1) <= 1)
        # per-type capacity: sum_j d_j * x_j^i <= Y_i
        d = np.array([job.workers for job in active])
        for i in range(M):
            constraints.append(d @ x[:, i] <= self.Y[i])

        # Work-conserving constraint (FFT Eq.7): keep GPUs busy when jobs are available.
        # K(t) = min(total GPUs, total demand). Force total allocated workers >= K(t).
        total_gpus = int(self.Y.sum())
        total_demand = int(d.sum())
        K = min(total_gpus, total_demand)
        total_allocated = cp.sum(cp.multiply(np.tile(d.reshape(-1, 1), (1, M)), x))
        constraints.append(total_allocated >= K)

        prob = cp.Problem(cp.Minimize(cp.sum(cp.multiply(C, x))), constraints)

        t0 = time.perf_counter()
        try:
            prob.solve(solver=cp.HIGHS, verbose=False)
        except Exception:
            prob.solve(verbose=False)
        self.solve_times.append(time.perf_counter() - t0)

        if x.value is None:
            return None
        return np.round(x.value).astype(int)

    def step(self, active, t):
        """
        Advance one round. `active` = list of admitted, unfinished jobs.
        In centralized FFT, a job is 'admitted' the round it arrives, but its first
        scheduling DECISION waits for this round's global solve to finish.
        Returns the allocation matrix (N x M).
        """
        newly_arrived = [j for j in active if j.admit_round is None]

        alloc = self._solve_round(active, t)

        # Admission latency for centralized FFT = time the new job waits behind the
        # GLOBAL solve before it can receive any decision. This grows with |A(t)|,
        # which is exactly the centralization bottleneck under bursts.
        if newly_arrived and self.solve_times:
            last_solve = self.solve_times[-1]
            for job in newly_arrived:
                job.admit_round = t
                self.admission_lat.append(last_solve)

        return alloc

    def update_fairness(self, active, alloc, t):
        """rho_j(t+1) update (Eq.5) -- grows when starved, shrinks when scheduled."""
        if alloc is None:
            return
        for j_idx, job in enumerate(active):
            tau = max(job.ideal_jct_rounds() * len(active), 1.0)   # fair-share completion est
            mu = (t - job.arrival) / max(tau, 1.0)                  # dynamic fairness coeff
            served = float(alloc[j_idx] @ job.theta) if alloc is not None else 0.0
            fair_rate = job.epochs / tau
            rho_prev = getattr(job, "rho", 0.0)
            job.rho = max(0.0, rho_prev + mu * (fair_rate - served))
