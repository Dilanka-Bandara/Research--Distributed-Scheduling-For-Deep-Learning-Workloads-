"""
dl_cluster_simulator.py
=======================
Research-grade discrete-event simulator comparing:
1. FFT (Fast and Fair Training, ICS '25) - State-of-the-Art Baseline
2. New Architecture (NA) - Enhanced Decentralised Score-Based Scheduler

This simulator tracks Job Completion Time (JCT), Finish-Time Fairness (FTF),
and perfectly replicates the heterogeneous throughput matrices.
"""

import numpy as np
from typing import List, Dict, Optional, Set
from dataclasses import dataclass, field

# =====================================================================
# 1. HARDWARE & MODEL CONFIGURATION (Derived from FFT Paper)
# =====================================================================
NUM_GPU_TYPES = 3
GPU_NAMES = ["T4", "V100", "A10"]
GPU_CAPACITY = {0: 12, 1: 12, 2: 12}  # 12 nodes of each type
GPU_C_N = {0: 1.0, 1: 1.5, 2: 2.0}    # Compute capability multipliers

STATE_SIZE_GB = np.array([0.15, 0.1, 0.2, 0.4, 2.5, 4.0, 12.0, 107.0])
TRANSFER_RATE_GB_PER_MIN = 1.25
ROUND_DURATION = 5 # 1 round = 5 minutes of real time

def migration_cost_rounds(model_type: int) -> float:
    """Calculates migration overhead in units of scheduling rounds."""
    minutes = STATE_SIZE_GB[model_type] / TRANSFER_RATE_GB_PER_MIN
    return minutes / ROUND_DURATION

WORKERS_REQUIRED = [1, 2, 2, 4, 4, 8, 8, 16]       # d_j (GPUs required)
EPOCHS_REQUIRED = [50.0, 50.0, 40.0, 40.0, 20.0, 20.0, 10.0, 10.0]  # W_j (Total work)

# Throughput matrix (theta^i_j): Epochs completed per round per GPU type
THROUGHPUT = np.array([
    [1.00, 1.50, 2.00],  # 0: VGG
    [0.80, 1.20, 1.60],  # 1: ResNet
    [0.60, 0.90, 1.20],  # 2: DenseNet
    [0.40, 0.60, 0.80],  # 3: Bert
    [0.20, 0.30, 0.40],  # 4: GPT-neo
    [0.10, 0.15, 0.20],  # 5: GPT-2
    [0.05, 0.08, 0.10],  # 6: OPT-2.7B
    [0.02, 0.04, 0.06]   # 7: OPT-6.7B
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
    rho: float = 0.0          # Fairness compensation term (FFT Eq 5)
    tau: float = 0.0          # Fair-share completion estimate
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
# 3. BASELINE FFT SCHEDULER (State-of-the-Art ILP Approximation)
# =====================================================================
class FFTScheduler:
    """
    Centralised FFT scheduler replicating equations 1-7 from the paper.
    Calculates cost matrices per job/GPU and executes greedy assignment 
    to approximate the ILP without requiring a commercial solver.
    """
    name = "FFT Baseline"

    def _tau(self, job: Job, t: int, n_active: int) -> float:
        # Eq: Fair share throughput estimation
        n = max(n_active, 1)
        best_fair_theta = 0.0
        for i in range(NUM_GPU_TYPES):
            fair_slots = max(1, GPU_CAPACITY[i] // n)
            if fair_slots >= job.d_j:
                best_fair_theta = max(best_fair_theta, job.theta[i])
        if best_fair_theta == 0: best_fair_theta = max(job.theta)
        return t + max(job.remaining_epochs, 1.0) / best_fair_theta

    def _phi(self, job: Job, i: int, t: int) -> float:
        # Eq: JCT Cost formulation
        if job.theta[i] <= 0: return 1e7
        age = max(0, t - job.arrival_round)
        return age * job.theta[i] / job.W_j + job.d_j * job.W_j / job.theta[i]

    def update_rho(self, job: Job, gpu: Optional[int], t: int):
        # Eq 5: Dynamic fairness penalty update
        fair_rate = job.W_j / max(job.tau - job.arrival_round, 1.0)
        actual_rate = job.theta[gpu] if gpu is not None else 0.0
        mu = max(0.0, t - job.arrival_round) / max(job.tau - job.arrival_round, 1.0)
        job.rho = max(0.0, job.rho + mu * (fair_rate - actual_rate))

    def schedule(self, active_jobs: List[Job], t: int) -> Dict[int, Optional[int]]:
        if not active_jobs: return {}
        
        # 1. Update Tau for all jobs
        n = len(active_jobs)
        for job in active_jobs: job.tau = self._tau(job, t, n)
        
        # 2. Build Global Cost Matrix C
        avail = dict(GPU_CAPACITY)
        C = np.full((n, NUM_GPU_TYPES), 1e7)
        for j, job in enumerate(active_jobs):
            for i in range(NUM_GPU_TYPES):
                cost = self._phi(job, i, t) + job.migration_cost(i) - (job.rho * job.theta[i])
                C[j, i] = cost

        # 3. Greedy Placement (Approximates ILP objective)
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
    """
    Proposed Architecture with Hybrid Event-Driven FFT.
    """
    name = "New Architecture"

    # ── TUNABLE HYPERPARAMETERS (Adjust these to find pros/cons!) ──
    ALPHA = 0.8         # Weight of GPU demand (size)
    BETA = 0.005        # Weight of Workload (epochs)
    GAMMA = 1.0         # Weight of Compute capability
    DELTA = 1.0         # Weight of Node Availability
    TAU_THRESH = 1.2    # Strictness of match (Higher = more jobs take fast path)
    
    def __init__(self):
        self.redis_avail = dict(GPU_CAPACITY)
        self.profiled_models: Set[int] = set() # Historical Cache
        self.global_queue: List[Job] = []
        self.profiling_active: Dict[int, int] = {} # job_id -> start_round
        self.fft = FFTScheduler() # Background brain

    def _S_job(self, job: Job, t: int) -> float:
        # Fairness-injected scoring
        base = self.ALPHA * job.d_j + self.BETA * job.W_j
        t_wait = max(0, t - job.arrival_round)
        return base * (1.0 + (0.15 * t_wait)) 

    def _S_node(self, gpu: int, avail: int) -> float:
        # A_n = node availability ratio (assume 4 GPUs per physical node)
        A_n = min(avail, 4) / 4.0
        return self.GAMMA * GPU_C_N[gpu] + self.DELTA * A_n

    def schedule(self, active_jobs: List[Job], new_arrivals: List[Job], t: int) -> Dict[int, Optional[int]]:
        allocation = {}
        
        # 1. Sync Shared State (Redis)
        self.redis_avail = dict(GPU_CAPACITY)
        for j in active_jobs:
            if j.last_gpu_type is not None and j not in new_arrivals and j not in self.global_queue:
                if j.job_id not in self.profiling_active:
                    self.redis_avail[j.last_gpu_type] -= j.d_j

        # 2. Process Profiling Completions (1 round delay)
        newly_profiled = []
        for job_id, start_r in list(self.profiling_active.items()):
            if t > start_r: # 1 round has passed
                job = next(j for j in active_jobs if j.job_id == job_id)
                self.profiled_models.add(job.model_type)
                self.global_queue.append(job)
                newly_profiled.append(job)
                del self.profiling_active[job_id]

        # 3. Queue-Velocity Passthrough (Turn off heuristic if cluster is quiet)
        is_stressed = len(self.global_queue) > 3 or len(new_arrivals) > 4
        
        if not is_stressed:
            # Bypass heuristic completely, hand directly to FFT
            for job in new_arrivals:
                self.global_queue.append(job)
            new_arrivals = []
        else:
            # 4. Anti-Fragmentation Batch Sorting
            new_arrivals.sort(key=lambda j: (j.d_j, j.W_j), reverse=True)

        # 5. Smart Dispatcher (O(1) Entry)
        for job in new_arrivals:
            s_job = self._S_job(job, t)
            best_gpu, best_delta = None, float('inf')
            
            for i in range(NUM_GPU_TYPES):
                if self.redis_avail[i] >= job.d_j:
                    delta = abs(self._S_node(i, self.redis_avail[i]) - s_job)
                    if delta < best_delta:
                        best_gpu, best_delta = i, delta
            
            # Bounded Deferral: Prevent LLMs (>=4 GPUs) getting trapped on T4s (i==0)
            if best_gpu == 0 and job.d_j >= 4 and (t - job.arrival_round) < 5:
                best_delta = float('inf') # Force to Slow Path to wait for better GPU
            
            if best_gpu is not None and best_delta < self.TAU_THRESH:
                # FAST PATH: Instant execution
                allocation[job.job_id] = best_gpu
                self.redis_avail[best_gpu] -= job.d_j
                self.profiled_models.add(job.model_type) # Profiled instantly on-the-fly
            else:
                # SLOW PATH: Historical Cache Check & Profiling
                if job.model_type in self.profiled_models:
                    self.global_queue.append(job) # Cache Hit
                else:
                    # Cache Miss: Send to Micro-Profiler
                    prof_gpu = next((i for i in range(NUM_GPU_TYPES) if self.redis_avail[i] >= job.d_j), None)
                    if prof_gpu is not None:
                        self.profiling_active[job.job_id] = t
                        allocation[job.job_id] = prof_gpu
                        self.redis_avail[prof_gpu] -= job.d_j
                    else:
                        self.global_queue.append(job) # Fallback if totally full

        # 6. Hybrid Event-Driven FFT (Triggers instantly for queued jobs)
        if self.global_queue:
            fft_alloc = self.fft.schedule(self.global_queue, t)
            for job in list(self.global_queue):
                gpu = fft_alloc.get(job.job_id)
                if gpu is not None and self.redis_avail[gpu] >= job.d_j:
                    allocation[job.job_id] = gpu
                    self.redis_avail[gpu] -= job.d_j
                    self.global_queue.remove(job)

        # 7. Maintain active jobs & Update Fairness
        for job in active_jobs:
            if job.job_id not in allocation and job.job_id not in self.profiling_active:
                allocation[job.job_id] = job.last_gpu_type
            self.fft.update_rho(job, allocation.get(job.job_id), t)

        return allocation

# =====================================================================
# 5. SIMULATION ENGINE
# =====================================================================
def generate_trace(name: str) -> List[Job]:
    """Generates synthetic traces mimicking the Microsoft Philly distribution."""
    jobs = []
    
    # [VGG, ResNet, DenseNet, Bert, GPT-neo, GPT-2, OPT-2.7B, OPT-6.7B]
    if name == "Trace 1 (vision)": 
        model_dist, arr_rate = [0.4, 0.4, 0.2, 0, 0, 0, 0, 0], 2
    elif name == "Trace 2 (mixed)": 
        model_dist, arr_rate = [0.2]*4 + [0.1]*2 + [0]*2, 3
    elif name == "Trace 6 (LLM heavy)": 
        model_dist, arr_rate = [0.1]*4 + [0.15]*4, 2
    elif name == "Trace 7 (high arrival)": 
        model_dist, arr_rate = [0.2]*4 + [0.1]*2 + [0]*2, 6
    elif name == "Burst (dynamic arrivals)": 
        model_dist, arr_rate = [0.15]*4 + [0.1]*4, 0 # Handled below
    
    jid = 0
    if name == "Burst (dynamic arrivals)":
        # Simulate a massive, sudden influx of 60 heterogeneous jobs at t=5
        for _ in range(60): 
            jobs.append(Job(jid, 5, np.random.choice(8, p=model_dist)))
            jid += 1
    else:
        # Steady state Poisson-like arrival
        for t in range(0, 100):
            if np.random.rand() < (arr_rate / 10.0):
                jobs.append(Job(jid, t, np.random.choice(8, p=model_dist)))
                jid += 1
    return jobs

def run_simulation(trace_name: str, SchedulerClass):
    jobs_pool = generate_trace(trace_name)
    active_jobs = []
    scheduler = SchedulerClass()
    t = 0
    
    while jobs_pool or active_jobs:
        new_arrivals = [j for j in jobs_pool if j.arrival_round == t]
        jobs_pool = [j for j in jobs_pool if j.arrival_round != t]
        active_jobs.extend(new_arrivals)
        
        # Schedule
        if isinstance(scheduler, NewArchScheduler):
            alloc = scheduler.schedule(active_jobs, new_arrivals, t)
        else:
            alloc = scheduler.schedule(active_jobs, t)
            
        # Execute & Process Progress
        for job in active_jobs:
            gpu = alloc.get(job.job_id)
            if gpu is not None:
                if job.first_scheduled is None: job.first_scheduled = t
                # Advance training. (Subtract migration cost if moved)
                progress = THROUGHPUT[job.model_type][gpu]
                if gpu != job.last_gpu_type and job.last_gpu_type is not None:
                    # Penalty for moving
                    penalty = migration_cost_rounds(job.model_type) * THROUGHPUT[job.model_type][gpu]
                    progress = max(0, progress - penalty)
                job.completed_epochs += progress
            job.last_gpu_type = gpu
            
        # Clean up completed jobs
        completed = [j for j in active_jobs if j.completed_epochs >= j.W_j]
        for c in completed:
            c.finish_round = t
            active_jobs.remove(c)
            if isinstance(scheduler, NewArchScheduler):
                if c in scheduler.global_queue: scheduler.global_queue.remove(c)
                if c.job_id in scheduler.profiling_active: del scheduler.profiling_active[c.job_id]
                
        t += 1
        if t > 1000: # Safety timeout for infinite queues
            for j in active_jobs: j.finish_round = 1000 
            completed.extend(active_jobs)
            break
            
    # Calculate Metrics
    jcts = [j.finish_round - j.arrival_round for j in completed]
    # FTF = JCT / Ideal_JCT (Ideal = Work / Best possible throughput)
    ftfs = [(j.finish_round - j.arrival_round) / (j.W_j / max(THROUGHPUT[j.model_type])) for j in completed]
    
    return np.mean(jcts) if jcts else 0, np.mean(ftfs) if ftfs else 0

# =====================================================================
# 6. MAIN EXECUTION (Comparing the Architectures)
# =====================================================================
if __name__ == "__main__":
    traces = [
        "Trace 1 (vision)", 
        "Trace 2 (mixed)", 
        "Trace 6 (LLM heavy)", 
        "Trace 7 (high arrival)", 
        "Burst (dynamic arrivals)"
    ]
    
    print("\nRunning Multi-Trace Comparison Simulator...")
    print("===========================================================================================")
    print(f" FINAL COMPARISON: FFT Baseline  vs  New Architecture (Hybrid)")
    print("===========================================================================================")
    print(f"{'Trace':<28} {'FFT_JCT':<8} {'NA_JCT':<8} {'Improv':<8} | {'FFT_FTF':<8} {'NA_FTF':<8}")
    print("-------------------------------------------------------------------------------------------")
    
    for trace in traces:
        # Seed to ensure BOTH schedulers get the exact same sequence of random jobs
        np.random.seed(42)
        fft_jct, fft_ftf = run_simulation(trace, FFTScheduler)
        
        np.random.seed(42)
        na_jct, na_ftf = run_simulation(trace, NewArchScheduler)
        
        # Calculate Improvement Multipliers
        jct_improv = fft_jct / na_jct if na_jct > 0 else 1.0
        
        marker = "<- NA WINS" if jct_improv > 1.05 else ""
        
        print(f"{trace:<28} {fft_jct:<8.1f} {na_jct:<8.1f} {jct_improv:<5.2f}x   | {fft_ftf:<8.2f} {na_ftf:<8.2f}  {marker}")
    
    print("===========================================================================================\n")
    print("HOW TO EXPERIMENT:")
    print("1. Find 'class NewArchScheduler' in the code.")
    print("2. Change ALPHA, BETA, or TAU_THRESH parameters.")
    print("3. Notice how making the Heuristic too greedy hurts Trace 6 (LLMs) but helps the Burst trace!")