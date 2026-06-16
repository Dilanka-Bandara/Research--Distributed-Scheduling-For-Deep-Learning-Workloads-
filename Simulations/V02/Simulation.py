import numpy as np
from typing import List, Dict, Optional, Tuple, Set
from dataclasses import dataclass, field
import random
import copy

# =====================================================================
# 1. CLUSTER & MODEL CONFIGURATION
# =====================================================================
NUM_GPU_TYPES = 3
GPU_CAPACITY = {0: 120, 1: 120, 2: 120}  # 0: T4, 1: V100, 2: A10
GPU_C_N = {0: 1.0, 1: 1.5, 2: 2.0}    # Compute capability

STATE_SIZE_GB = np.array([0.15, 0.1, 0.2, 0.4, 2.5, 4.0, 12.0, 107.0])
TRANSFER_RATE_GB_PER_MIN = 1.25
ROUND_DURATION = 5

def migration_cost_rounds(model_type: int) -> float:
    return (STATE_SIZE_GB[model_type] / TRANSFER_RATE_GB_PER_MIN) / ROUND_DURATION

WORKERS_REQUIRED = [1, 2, 2, 4, 4, 8, 8, 16]       # d_j
EPOCHS_REQUIRED = [50.0, 50.0, 40.0, 40.0, 20.0, 20.0, 10.0, 10.0]  # W_j

THROUGHPUT = np.array([
    [1.00, 1.50, 2.00], [0.80, 1.20, 1.60], [0.60, 0.90, 1.20], [0.40, 0.60, 0.80],
    [0.20, 0.30, 0.40], [0.10, 0.15, 0.20], [0.05, 0.08, 0.10], [0.02, 0.04, 0.06]
])

# =====================================================================
# 2. JOB DATA STRUCTURE
# =====================================================================
@dataclass
class Job:
    job_id: int
    arrival_round: int
    model_type: int
    d_j: int = field(init=False)
    W_j: float = field(init=False)
    theta: np.ndarray = field(init=False)
    
    completed_epochs: float = 0.0
    rho: float = 0.0
    tau: float = 0.0
    last_gpu_type: Optional[int] = None
    first_scheduled: Optional[int] = None
    finish_round: Optional[int] = None

    def __post_init__(self):
        self.d_j = WORKERS_REQUIRED[self.model_type]
        self.W_j = EPOCHS_REQUIRED[self.model_type]
        self.theta = THROUGHPUT[self.model_type]

    @property
    def remaining_epochs(self) -> float:
        return max(0.0, self.W_j - self.completed_epochs)

    def migration_cost(self, target_gpu: int) -> float:
        if self.last_gpu_type is None or self.last_gpu_type == target_gpu: return 0.0
        return migration_cost_rounds(self.model_type)

# =====================================================================
# 3. BASELINE FFT SCHEDULER
# =====================================================================
class FFTScheduler:
    def _tau(self, job: Job, t: int, n_active: int) -> float:
        n = max(n_active, 1)
        best = max(job.theta[i] for i in range(NUM_GPU_TYPES) if max(1, GPU_CAPACITY[i]//n) >= job.d_j) if any(max(1, GPU_CAPACITY[i]//n) >= job.d_j for i in range(NUM_GPU_TYPES)) else max(job.theta)
        return t + max(job.remaining_epochs, 1.0) / best

    def _phi(self, job: Job, i: int, t: int) -> float:
        if job.theta[i] <= 0: return 1e7
        return max(0, t - job.arrival_round) * job.theta[i] / job.W_j + job.d_j * job.W_j / job.theta[i]

    def update_rho(self, job: Job, gpu: Optional[int], t: int):
        fair_rate = job.W_j / max(job.tau - job.arrival_round, 1.0)
        actual = job.theta[gpu] if gpu is not None else 0.0
        job.rho = max(0.0, job.rho + (max(0.0, t - job.arrival_round) / max(job.tau - job.arrival_round, 1.0)) * (fair_rate - actual))

    def schedule(self, active_jobs: List[Job], t: int) -> Dict[int, Optional[int]]:
        if not active_jobs: return {}
        for job in active_jobs: job.tau = self._tau(job, t, len(active_jobs))
        
        avail = dict(GPU_CAPACITY)
        C = np.full((len(active_jobs), NUM_GPU_TYPES), 1e7)
        for j, job in enumerate(active_jobs):
            for i in range(NUM_GPU_TYPES):
                C[j, i] = self._phi(job, i, t) + job.migration_cost(i) - (job.rho * job.theta[i])

        order = np.argsort(np.min(C, axis=1))
        allocation = {job.job_id: None for job in active_jobs}

        for j_idx in order:
            job = active_jobs[j_idx]
            best_gpu, best_c = None, float('inf')
            for i in range(NUM_GPU_TYPES):
                if avail[i] >= job.d_j and C[j_idx, i] < best_c:
                    best_c, best_gpu = C[j_idx, i], i
            allocation[job.job_id] = best_gpu
            if best_gpu is not None: avail[best_gpu] -= job.d_j
        return allocation

# =====================================================================
# 4. ENHANCED DECENTRALISED SCHEDULER (New Architecture)
# =====================================================================
class NewArchScheduler:
    # ── TUNABLE PARAMETERS (Adjust these to find pros/cons!) ──
    ALPHA = 0.8    # Weight of GPU demand
    BETA = 0.005   # Weight of Workload (Epochs)
    GAMMA = 1.0    # Weight of Compute capability
    DELTA = 1.0    # Weight of Availability
    TAU_THRESH = 1.1 # Match threshold
    
    def __init__(self):
        self.redis_avail = dict(GPU_CAPACITY)
        self.profiled_models = set()
        self.global_queue = []
        self.profiling_jobs = {}
        self.fft = FFTScheduler()

    def _S_job(self, job: Job, t: int) -> float:
        base = self.ALPHA * job.d_j + self.BETA * job.W_j
        return base * (1.0 + (0.2 * max(0, t - job.arrival_round))) # Fairness injected

    def _S_node(self, gpu: int, avail: int) -> float:
        return self.GAMMA * GPU_C_N[gpu] + self.DELTA * (min(avail, 4) / 4)

    def schedule(self, active_jobs: List[Job], new_arrivals: List[Job], t: int) -> Dict[int, Optional[int]]:
        allocation = {}
        
        # 1. Sync Redis
        self.redis_avail = dict(GPU_CAPACITY)
        for j in active_jobs:
            if j.last_gpu_type is not None and j not in new_arrivals and j not in self.global_queue:
                self.redis_avail[j.last_gpu_type] -= j.d_j

        # 2. Queue-Velocity Passthrough (Turn off heuristic if quiet)
        if len(self.global_queue) <= 3 and len(new_arrivals) < 5:
            self.global_queue.extend(new_arrivals)
            new_arrivals = []
        else:
            # 3. Arrival Batch Sorting (Anti-Fragmentation)
            new_arrivals.sort(key=lambda j: (j.d_j, j.W_j), reverse=True)

        # 4. Smart Dispatcher (Fast Path)
        for job in new_arrivals:
            s_job = self._S_job(job, t)
            best_gpu, best_delta = None, float('inf')
            for i in range(NUM_GPU_TYPES):
                if self.redis_avail[i] >= job.d_j:
                    delta = abs(self._S_node(i, self.redis_avail[i]) - s_job)
                    if delta < best_delta: best_gpu, best_delta = i, delta
            
            # Bounded Deferral for LLMs (Don't trap on T4s if wait < 5)
            if best_gpu == 0 and job.d_j >= 4 and (t - job.arrival_round) < 5:
                best_delta = float('inf') # Force to slow path
            
            if best_gpu is not None and best_delta < self.TAU_THRESH:
                allocation[job.job_id] = best_gpu
                self.redis_avail[best_gpu] -= job.d_j
                self.profiled_models.add(job.model_type)
            else:
                if job.model_type in self.profiled_models:
                    self.global_queue.append(job) # Cache Hit
                else:
                    self.global_queue.append(job) # Simplified profiling for simulation

        # 5. Hybrid Event-Driven FFT (Trigger instantly for queued jobs)
        if self.global_queue:
            fft_alloc = self.fft.schedule(self.global_queue, t)
            for job in list(self.global_queue):
                gpu = fft_alloc.get(job.job_id)
                if gpu is not None and self.redis_avail[gpu] >= job.d_j:
                    allocation[job.job_id] = gpu
                    self.redis_avail[gpu] -= job.d_j
                    self.global_queue.remove(job)

        # 6. Maintain running jobs
        for job in active_jobs:
            if job.job_id not in allocation:
                allocation[job.job_id] = job.last_gpu_type
            self.fft.update_rho(job, allocation.get(job.job_id), t)

        return allocation

# =====================================================================
# 5. SIMULATION ENGINE
# =====================================================================
def generate_trace(name: str) -> List[Job]:
    jobs = []
    if name == "Trace 1 (vision)": model_dist, arr_rate = [0.4, 0.4, 0.2, 0, 0, 0, 0, 0], 2
    elif name == "Trace 2 (mixed)": model_dist, arr_rate = [0.2]*4 + [0.1]*2 + [0]*2, 3
    elif name == "Trace 6 (LLM)": model_dist, arr_rate = [0.1]*4 + [0.15]*4, 2
    elif name == "Burst": model_dist, arr_rate = [0.15]*4 + [0.1]*4, 0 # Burst handled manually
    else: model_dist, arr_rate = [0.25]*4 + [0]*4, 2
    
    jid = 0
    if name == "Burst":
        for _ in range(50): 
            jobs.append(Job(jid, 5, np.random.choice(8, p=model_dist)))
            jid += 1
    else:
        for t in range(0, 100):
            if np.random.rand() < (arr_rate / 10.0):
                jobs.append(Job(jid, t, np.random.choice(8, p=model_dist)))
                jid += 1
    return jobs

def run_simulation(trace_name: str, SchedulerClass):
    jobs = generate_trace(trace_name)
    active_jobs = []
    scheduler = SchedulerClass()
    t = 0
    
    while jobs or active_jobs:
        new_arrivals = [j for j in jobs if j.arrival_round == t]
        jobs = [j for j in jobs if j.arrival_round != t]
        active_jobs.extend(new_arrivals)
        
        if isinstance(scheduler, NewArchScheduler):
            alloc = scheduler.schedule(active_jobs, new_arrivals, t)
        else:
            alloc = scheduler.schedule(active_jobs, t)
            
        for job in active_jobs:
            gpu = alloc.get(job.job_id)
            if gpu is not None:
                if job.first_scheduled is None: job.first_scheduled = t
                job.completed_epochs += THROUGHPUT[job.model_type][gpu]
            job.last_gpu_type = gpu
            
        completed = [j for j in active_jobs if j.completed_epochs >= j.W_j]
        for c in completed:
            c.finish_round = t
            active_jobs.remove(c)
            if isinstance(scheduler, NewArchScheduler) and c in scheduler.global_queue:
                scheduler.global_queue.remove(c)
                
        t += 1
        if t > 500: break # Safety timeout
        
    jcts = [j.finish_round - j.arrival_round for j in completed]
    ftfs = [(j.finish_round - j.arrival_round) / (j.W_j / max(THROUGHPUT[j.model_type])) for j in completed]
    return np.mean(jcts) if jcts else 0, np.mean(ftfs) if ftfs else 0

# =====================================================================
# 6. EXECUTE AND COMPARE
# =====================================================================
if __name__ == "__main__":
    print("Running simulations. Please wait...")
    traces = ["Trace 1 (vision)", "Trace 2 (mixed)", "Trace 6 (LLM)", "Burst"]
    
    print("\n=======================================================================")
    print(f"{'Trace Name':<20} | {'FFT JCT':<8} | {'NA JCT':<8} | {'Improv':<8}")
    print("=======================================================================")
    
    for trace in traces:
        # We seed to make sure both schedulers see the EXACT same jobs
        np.random.seed(42)
        fft_jct, fft_ftf = run_simulation(trace, FFTScheduler)
        
        np.random.seed(42)
        na_jct, na_ftf = run_simulation(trace, NewArchScheduler)
        
        improv = fft_jct / na_jct if na_jct > 0 else 1.0
        marker = "← NA WINS!" if improv > 1.05 else ""
        
        print(f"{trace:<20} | {fft_jct:<8.1f} | {na_jct:<8.1f} | {improv:<5.2f}x  {marker}")
    
    print("=======================================================================\n")
    print("TWEAKING GUIDE:")
    print("1. Open this file and find 'class NewArchScheduler:'.")
    print("2. Change ALPHA (Size weight) or BETA (Workload weight).")
    print("3. Re-run to see how the heuristic impacts Trace 6 vs Burst!")