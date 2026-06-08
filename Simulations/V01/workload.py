"""
Workload and cluster models shared by BOTH schedulers.

Design intent (viva-defensible):
  - Models are parameter tuples (NO real training), matching FFT's simulator approach.
  - Job arrivals/durations/worker-counts follow distributions in the spirit of the
    Microsoft Philly trace (statistical parameterization, not real workloads).
  - Throughput theta[i] = epochs completed per round on GPU type i. Completion time
    of a job on type i (no preemption) = W_j / theta_j^i  -- exactly the FFT definition.
"""

import numpy as np

# ---------------------------------------------------------------------------
# Cluster definition: heterogeneous GPU types (per your Figure 1: A100/V100/T4)
# Each type has a relative compute capability C (used by your Smart Dispatcher)
# and a per-type worker count. Higher index = higher-end device.
# ---------------------------------------------------------------------------
GPU_TYPES = ["T4", "V100", "A100"]          # low -> high end
GPU_CAPABILITY = {"T4": 1.0, "V100": 1.8, "A100": 2.7}   # speedup, in FFT's 1.6-2.71x range
GPU_COUNT = {"T4": 16, "V100": 16, "A100": 16}            # workers per type

M = len(GPU_TYPES)                           # number of device types

# ---------------------------------------------------------------------------
# Representative DL models as parameter tuples (cf. FFT Table 2).
# base_epoch_time_on_T4 = minutes to complete ONE epoch on the slowest GPU.
# state_gb = training-state size, drives migration overhead (large LLMs ~100GB+).
# ---------------------------------------------------------------------------
MODEL_ZOO = {
    "ResNet50":  dict(base_epoch_min=0.8, state_gb=0.3,  typical_workers=[1, 2, 4]),
    "VGG19":     dict(base_epoch_min=1.0, state_gb=0.5,  typical_workers=[1, 2, 4]),
    "DenseNet":  dict(base_epoch_min=0.9, state_gb=0.4,  typical_workers=[1, 2]),
    "BERT":      dict(base_epoch_min=1.5, state_gb=1.3,  typical_workers=[1, 4]),
    "GPT-neo":   dict(base_epoch_min=3.5, state_gb=50.0, typical_workers=[4]),
    "GPT-2":     dict(base_epoch_min=3.0, state_gb=30.0, typical_workers=[4, 8]),
    "OPT-6.7B":  dict(base_epoch_min=5.0, state_gb=107.0, typical_workers=[4, 8]),
}
MODEL_NAMES = list(MODEL_ZOO.keys())


class Job:
    """A single DL training job. Throughput is computed mathematically, not measured."""
    def __init__(self, jid, arrival, model, workers, epochs):
        self.jid = jid
        self.arrival = arrival              # round index at which job arrives
        self.model = model
        self.workers = workers              # d_j : requested GPU workers
        self.epochs = epochs                # W_j : required epochs

        spec = MODEL_ZOO[model]
        self.state_gb = spec["state_gb"]
        # theta_j^i : epochs/round on each GPU type. One "round" of wall-clock = ROUND_MIN.
        # epochs/round on T4 = ROUND_MIN / base_epoch_min ; scaled by capability for others.
        base = spec["base_epoch_min"]
        self.theta = np.array([
            (ROUND_MIN / base) * GPU_CAPABILITY[g] for g in GPU_TYPES
        ])  # length M

        # bookkeeping filled in during simulation
        self.admit_round = None             # when dispatcher accepted it (latency source)
        self.start_round = None             # first round it actually executed
        self.finish_round = None
        self.progress = 0.0                 # epochs completed so far
        self.current_type = None            # GPU type index it last ran on (for switch cost)
        self.path = None                    # "fast" or "slow" (your arch only)

    @property
    def remaining(self):
        return max(0.0, self.epochs - self.progress)

    def ideal_jct_rounds(self):
        """T_id: completion time alone on its BEST gpu type (for Finish-Time-Fairness)."""
        best_theta = self.theta.max()
        return self.epochs / best_theta


# Wall-clock minutes represented by one scheduling round (FFT default = 5 min).
ROUND_MIN = 5.0
# Migration overhead model: minutes to move state, as fraction of a round.
# transfer ~ state_gb / bandwidth. 10 Gbps LAN ~= 1.25 GB/s -> seconds, but we add
# checkpoint save/load + NCCL re-init overhead. We express it as ROUNDS lost.
def migration_rounds(state_gb):
    transfer_min = state_gb / 1.25 / 60.0          # GB / (GB/s) -> s -> min
    overhead_min = transfer_min + 0.5              # +checkpoint/NCCL fixed cost
    return overhead_min / ROUND_MIN                 # fraction of a round wasted


def generate_trace(n_jobs, seed, mode="mixed"):
    """
    Build a reproducible job trace. Both schedulers receive the SAME trace.

    mode:
      "steady" : near-uniform Poisson arrivals (where centralized FFT is fine)
      "bursty" : long quiet gaps punctuated by large simultaneous bursts
                 (where centralized admission becomes the bottleneck -- your claim)
      "mixed"  : steady baseline with a few injected bursts
    """
    rng = np.random.default_rng(seed)
    jobs = []
    t = 0.0
    for jid in range(n_jobs):
        # ---- arrival process ----
        if mode == "steady":
            gap = rng.exponential(1.2)
        elif mode == "bursty":
            # 70% of jobs arrive in tight clusters; 30% spread out
            if rng.random() < 0.7:
                gap = rng.exponential(0.05)   # nearly simultaneous -> burst
            else:
                gap = rng.exponential(4.0)    # long quiet gap between bursts
        else:  # mixed
            gap = rng.exponential(1.0)
            if jid % 50 == 0 and jid > 0:
                gap = 0.0                     # inject a synchronized burst
        t += gap
        arrival = int(t)

        # ---- job shape (Philly-style statistical sampling) ----
        model = rng.choice(MODEL_NAMES)
        workers = int(rng.choice(MODEL_ZOO[model]["typical_workers"]))
        # epochs: heavy-tailed (most jobs short, a few very long) -> log-normal
        epochs = float(np.clip(rng.lognormal(mean=2.2, sigma=0.9), 3, 200))

        jobs.append(Job(jid, arrival, model, workers, round(epochs, 1)))

    jobs.sort(key=lambda j: j.arrival)
    return jobs
