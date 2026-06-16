"""
workload.py
-----------
Synthetic DL training-job trace generator.

Replicates the *statistical* properties of the Microsoft Philly trace
(Poisson-like arrivals, log-normal processing times, variable worker counts)
rather than executing real neural networks. Each job carries per-GPU-type
throughput derived from a small model zoo, so the schedulers can compute
completion times mathematically.

Nothing here trains anything. A "job" is a parameter tuple.
"""

from __future__ import annotations
import math
import random
from dataclasses import dataclass, field
from typing import Dict, List


# ----------------------------------------------------------------------
# GPU types. Throughput multipliers are relative speeds (epochs/round)
# for a reference model. Higher = faster device.
# Loosely modelled on the T4 / V100 / A10 ordering used in the FFT paper.
# ----------------------------------------------------------------------
GPU_TYPES: Dict[str, Dict[str, float]] = {
    "T4":   {"speed": 1.0, "memory_gb": 16},   # low end
    "V100": {"speed": 2.0, "memory_gb": 32},   # mid
    "A10":  {"speed": 2.7, "memory_gb": 48},   # high end (per FFT speedups 1.6-2.71x)
}

# Capability score C_n per GPU type (used by the dispatcher's S_node).
GPU_CAPABILITY: Dict[str, float] = {
    "T4": 1.0,
    "V100": 2.0,
    "A10": 2.7,
}


# ----------------------------------------------------------------------
# Model zoo. base_epoch_cost = work units per epoch on a *reference*
# (speed = 1.0) GPU. mem_gb = memory footprint, used to forbid placing
# large models on small-memory GPUs. state_gb = checkpoint/migration size.
# ----------------------------------------------------------------------
MODEL_ZOO: Dict[str, Dict[str, float]] = {
    "ResNet50":  {"base_epoch_cost": 1.0,  "mem_gb": 8,  "state_gb": 0.3},
    "VGG19":     {"base_epoch_cost": 1.3,  "mem_gb": 11, "state_gb": 0.5},
    "DenseNet":  {"base_epoch_cost": 1.1,  "mem_gb": 9,  "state_gb": 0.4},
    "BERT-base": {"base_epoch_cost": 2.0,  "mem_gb": 14, "state_gb": 1.2},
    "GPT-neo":   {"base_epoch_cost": 4.0,  "mem_gb": 22, "state_gb": 50.0},
    "GPT-2":     {"base_epoch_cost": 5.0,  "mem_gb": 28, "state_gb": 60.0},
    "OPT-6.7B":  {"base_epoch_cost": 8.0,  "mem_gb": 30, "state_gb": 107.0},
}

MODEL_NAMES = list(MODEL_ZOO.keys())


@dataclass
class Job:
    job_id: int
    arrival: float            # arrival time (in rounds, continuous)
    model: str
    d_j: int                  # requested workers (GPUs)
    W_j: int                  # required epochs

    # --- filled in by profiling / scheduling ---
    theta: Dict[str, float] = field(default_factory=dict)  # throughput per GPU type (epochs/round)
    state_gb: float = 0.0
    mem_gb: float = 0.0

    # --- runtime bookkeeping (mutated by the engine) ---
    epochs_done: float = 0.0
    admit_time: float | None = None       # when it first got a GPU slot
    first_exec_time: float | None = None  # first time actually executed
    finish_time: float | None = None
    current_gpu: str | None = None        # GPU type it is currently on
    migrations: int = 0

    def remaining_epochs(self) -> float:
        return max(0.0, self.W_j - self.epochs_done)

    def is_done(self) -> bool:
        return self.epochs_done >= self.W_j

    def throughput_on(self, gpu_type: str) -> float:
        return self.theta.get(gpu_type, 0.0)


def compute_throughput(model: str, gpu_type: str) -> float:
    """Epochs completed per scheduling round for `model` on `gpu_type`.

    Scaled so a typical job occupies its GPUs for many rounds (as real DL
    training does), which creates genuine queueing/contention under load.
    """
    base = MODEL_ZOO[model]["base_epoch_cost"]
    speed = GPU_TYPES[gpu_type]["speed"]
    # epochs/round = device speed / per-epoch cost. Small values => long jobs.
    return round(speed / base * 0.5, 4)


def can_fit(model: str, gpu_type: str) -> bool:
    """Memory feasibility: big models cannot run on small-memory GPUs."""
    return MODEL_ZOO[model]["mem_gb"] <= GPU_TYPES[gpu_type]["memory_gb"]


def profile_job(job: Job) -> None:
    """Fill in throughput / memory / state for a job (the 'micro-profiler')."""
    job.mem_gb = MODEL_ZOO[job.model]["mem_gb"]
    job.state_gb = MODEL_ZOO[job.model]["state_gb"]
    job.theta = {}
    for g in GPU_TYPES:
        job.theta[g] = compute_throughput(job.model, g) if can_fit(job.model, g) else 0.0


# ----------------------------------------------------------------------
# Trace generation
# ----------------------------------------------------------------------
def generate_trace(
    n_jobs: int = 200,
    regime: str = "mixed",
    seed: int = 0,
    horizon: float = 400.0,
) -> List[Job]:
    """
    Generate a list of Jobs sorted by arrival time.

    regime:
      - "steady": near-uniform arrivals, smaller jobs dominate.
      - "mixed":  Poisson arrivals, balanced model mix.
      - "bursty": arrivals clustered into bursts (stress-tests admission latency).
    """
    rng = random.Random(seed)
    jobs: List[Job] = []

    # Model-mix weights per regime.
    if regime == "steady":
        weights = [0.30, 0.15, 0.20, 0.20, 0.07, 0.05, 0.03]
        mean_interarrival = horizon / n_jobs
    elif regime == "bursty":
        weights = [0.20, 0.10, 0.10, 0.15, 0.18, 0.15, 0.12]  # more big jobs
        mean_interarrival = horizon / n_jobs
    else:  # mixed
        weights = [0.22, 0.13, 0.15, 0.18, 0.12, 0.12, 0.08]
        mean_interarrival = horizon / n_jobs

    # --- arrival times ---
    if regime == "bursty":
        # Cluster arrivals into a handful of bursts.
        n_bursts = max(3, n_jobs // 25)
        burst_centers = sorted(rng.uniform(0, horizon) for _ in range(n_bursts))
        arrivals = []
        for _ in range(n_jobs):
            c = rng.choice(burst_centers)
            a = max(0.0, rng.gauss(c, horizon * 0.01))  # tight cluster around centre
            arrivals.append(a)
        arrivals.sort()
    else:
        # Poisson process (exponential inter-arrival), steady ~ less variance.
        arrivals = []
        t = 0.0
        for _ in range(n_jobs):
            if regime == "steady":
                gap = rng.uniform(0.5 * mean_interarrival, 1.5 * mean_interarrival)
            else:
                gap = rng.expovariate(1.0 / mean_interarrival)
            t += gap
            arrivals.append(t)

    for i, a in enumerate(arrivals):
        model = rng.choices(MODEL_NAMES, weights=weights, k=1)[0]
        # Worker count: log-normal-ish, capped at 8 and at least 1.
        d_j = min(8, max(1, int(round(rng.lognormvariate(0.4, 0.6)))))
        # Epochs: log-normal processing time.
        W_j = max(2, int(round(rng.lognormvariate(2.2, 0.7))))
        job = Job(job_id=i, arrival=round(a, 4), model=model, d_j=d_j, W_j=W_j)
        jobs.append(job)

    jobs.sort(key=lambda j: j.arrival)
    for new_id, j in enumerate(jobs):
        j.job_id = new_id
    return jobs


def make_cluster(n_per_type: int = 12) -> Dict[str, int]:
    """
    Cluster inventory: how many GPU workers exist per type.
    Default 12 each (36 GPUs total), matching the proposal's 36-GPU lab cluster.
    """
    return {g: n_per_type for g in GPU_TYPES}


if __name__ == "__main__":
    for reg in ("steady", "mixed", "bursty"):
        trace = generate_trace(n_jobs=50, regime=reg, seed=1)
        profile_job(trace[0])
        print(f"[{reg}] {len(trace)} jobs, first arrival {trace[0].arrival}, "
              f"last {trace[-1].arrival:.1f}, sample model {trace[0].model}, "
              f"theta {trace[0].theta}")
