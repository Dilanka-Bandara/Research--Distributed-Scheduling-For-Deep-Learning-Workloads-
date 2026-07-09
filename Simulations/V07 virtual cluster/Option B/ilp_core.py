"""
ilp_core.py — the FFT paper's per-round optimisation, shared by both the
centralised FFT scheduler process and the smart architecture's background brain
(your design reuses FFT's exact mathematics as the brain, spec Sec. 7).

Costs per (job, type), identical to scheduler_simulation_v2/fft_baseline.py:
  phi_j^i(t)  = (t-a_j)*theta/W_j + d_j*W_j/theta        (JCT term, paper Eq. 3)
  s_j^i(t)    = switch penalty if changing type           (Sec. 4.2.3)
  rho_j(t)    = fairness compensation, paper Eq. 5 with exact mu_j(t)
  continuity  = small keep-running reward (switching-control intent)
  base=100    = strong work-conservation reward (Eq. 7 as reward; hard
                constraint is infeasible in tight regimes — documented lesson)
Solved EXACTLY in-process by fast_solver (LP fast path + HiGHS MILP).
`t` is in ROUNDS of scaled time since run start.
"""
from __future__ import annotations
from typing import Dict, List
from fast_solver import solve_assignment
from workload import GPU_TYPES

SWITCH_PENALTY = 0.5

class FairnessState:
    def __init__(self, mu: float = 1.0):
        self.rho: Dict[int, float] = {}
        self.mu = mu

    def update(self, jobs: List[dict], t_rounds: float):
        n = max(1, len(jobs))
        for j in jobs:
            jid = j["id"]
            self.rho.setdefault(jid, 0.0)
            theta = j["theta"]
            best = max(theta.values()) if theta else 1.0
            tau = j["W"] / max(1e-6, best / n)          # duration on 1/N share
            fair_rate = j["W"] / max(1e-6, tau)
            done_rate = theta.get(j.get("gpu") or "", 0.0)
            age_r = max(0.0, t_rounds - j["arrival_rounds"])
            mu_t = self.mu * age_r / max(1e-6, tau)     # exact mu_j(t), Eq. 5
            self.rho[jid] = max(0.0, self.rho[jid] + mu_t * (fair_rate - done_rate))

def build_costs(jobs: List[dict], t_rounds: float, fair: FairnessState):
    costs, demands, ids = {}, {}, []
    for j in jobs:
        jid = j["id"]; ids.append(jid); demands[jid] = j["d"]
        for g in GPU_TYPES:
            th = j["theta"].get(g, 0.0)
            if th <= 0: continue
            age_r = max(0.0, t_rounds - j["arrival_rounds"])
            phi = age_r * th / max(1e-6, j["W"]) + j["d"] * j["W"] / th
            sw = 0.0 if (j.get("gpu") in (None, g)) else SWITCH_PENALTY * (j["state_gb"] / 10.0)
            cont = 0.5 * th if (j.get("gpu") == g and 0 < j["progress"] < j["W"]) else 0.0
            fairr = fair.rho.get(jid, 0.0) * th
            costs[(jid, g)] = -(100.0 + fairr + cont) + 0.1 * (phi + sw)
    return ids, demands, costs

def solve(jobs: List[dict], capacity: Dict[str, int], t_rounds: float,
          fair: FairnessState) -> Dict[int, str]:
    if not jobs: return {}
    fair.update(jobs, t_rounds)
    ids, demands, costs = build_costs(jobs, t_rounds, fair)
    return solve_assignment(ids, demands, list(GPU_TYPES), capacity, costs)
