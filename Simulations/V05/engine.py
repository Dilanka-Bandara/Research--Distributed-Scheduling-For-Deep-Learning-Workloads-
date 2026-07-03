"""
engine.py
---------
Discrete-event simulation engine for comparing the FFT baseline against the
upgraded Enhanced Decentralised Score-Based Scheduler.

METRIC DEFINITIONS (the comparison hinges on this):

  * admission_latency == SCHEDULING-DECISION latency: time from arrival until
    the scheduler DECIDES a placement.
      - FFT: decided only when a synchronous round solve includes the job, so it
        pays (wait to next round) + (MEASURED solve time, which grows with the
        active-job count). This is the centralised bottleneck.
      - Smart: a Fast-Path job is decided in O(1) the instant it arrives,
        independent of load. A Slow-Path job is decided when the background brain
        next runs (event-triggered the moment it queues); the brain's solve cost
        runs in the background and does NOT block the decision.
    This is the architectural quantity the design targets. It is deliberately
    NOT the time until a free GPU appears.

  * starvation == time from arrival to first EXECUTION (capacity-bound). Similar
    across schedulers because it is governed by physical GPU availability.
    Reported separately to keep decision-latency honest.

  * jct / makespan / ftf are measured from actual simulated execution.
"""

from __future__ import annotations
from typing import Dict, List
import statistics

from workload import (Job, GPU_TYPES, profile_job,
                      migration_stall_rounds, profiling_stall_rounds)


def _execute_round(j: Job, round_len: float) -> None:
    """Advance a placed job by one round, consuming any pending stall first.

    Stall (migration state-transfer or profiling) is real lost GPU time: the
    job holds its workers but makes no training progress for that fraction of
    the round (paper Sec. 2.2 migration overhead; Sec. 5 profiling).
    """
    avail = round_len
    if j.stall > 0:
        used = min(j.stall, avail)
        j.stall -= used
        avail -= used
    if avail > 0 and j.current_gpu is not None:
        j.epochs_done += j.throughput_on(j.current_gpu) * avail
from fft_baseline import FFTScheduler
from smart_scheduler import SmartScheduler


def _ideal_jct(job: Job) -> float:
    best_th = max((th for th in job.theta.values() if th > 0), default=1.0)
    return job.W_j / max(1e-6, best_th)


def _collect_metrics(jobs: List[Job], decision_latencies: List[float],
                     fast_count: int = 0, handshake_rejects: int = 0) -> Dict[str, float]:
    finished = [j for j in jobs if j.is_done() and j.finish_time is not None]
    jcts = [j.finish_time - j.arrival for j in finished]
    ftfs, starv = [], []
    for j in finished:
        t_id = _ideal_jct(j)
        t_sh = j.finish_time - j.arrival
        ftfs.append(t_sh / max(1e-6, t_id))
        if j.first_exec_time is not None:
            starv.append(j.first_exec_time - j.arrival)

    def mean(xs): return statistics.mean(xs) if xs else 0.0
    def p99(xs):
        if not xs: return 0.0
        s = sorted(xs); return s[min(len(s) - 1, int(0.99 * len(s)))]

    return {
        "n_jobs": len(jobs),
        "n_finished": len(finished),
        "jct_mean": mean(jcts),
        "makespan": max((j.finish_time for j in finished), default=0.0),
        "ftf_mean": mean(ftfs),
        "ftf_max": max(ftfs) if ftfs else 0.0,
        "starvation_mean": mean(starv),
        "admission_latency_mean": mean(decision_latencies),
        "admission_latency_p99": p99(decision_latencies),
        "fast_path_frac": fast_count / max(1, len(jobs)),
        # overhead accounting (rounds of GPU time lost, totals across jobs)
        "migrations_total": sum(j.migrations for j in jobs),
        "migration_time_lost": sum(j.migration_time_lost for j in jobs),
        "profile_time_lost": sum(j.profile_time_lost for j in jobs),
        "handshake_rejects": handshake_rejects,
    }


def run_fft(jobs: List[Job], cluster: Dict[str, int],
            round_len: float = 1.0, max_rounds: int = 30000,
            **fft_kwargs) -> Dict[str, float]:
    jobs = [Job(**{k: getattr(j, k) for k in
                   ("job_id", "arrival", "model", "d_j", "W_j")}) for j in jobs]
    for j in jobs:
        profile_job(j)

    sched = FFTScheduler(cluster, round_len=round_len, **fft_kwargs)
    decision_latencies: List[float] = []
    pending = sorted(jobs, key=lambda j: j.arrival)
    arrived: List[Job] = []
    t, idx = 0.0, 0

    for _ in range(max_rounds):
        while idx < len(pending) and pending[idx].arrival <= t:
            j = pending[idx]; idx += 1
            arrived.append(j)
            # FFT profiles EVERY new job on the fly before it can train
            # (paper Sec. 5: profiling has top priority; suspends training).
            p = profiling_stall_rounds()
            j.stall += p
            j.profile_time_lost += p
        active = [j for j in arrived if not j.is_done()]
        if not active and idx >= len(pending):
            break

        alloc = sched.schedule_round(active, t)
        solve_wall = sched.last_solve_wall

        for j in active:
            if not getattr(j, "_decided", False):
                j._decided = True
                decision_latencies.append((t - j.arrival) + solve_wall)
            if j.admit_time is None and j.job_id in alloc:
                j.admit_time = t

        for j in active:
            gpu = alloc.get(j.job_id)
            if gpu is None:
                continue
            if j.first_exec_time is None:
                j.first_exec_time = t
            # Migration = real lost time: switching GPU type transfers the
            # training state over the LAN (paper Sec. 2.2).
            if j.current_gpu is not None and j.current_gpu != gpu and j.epochs_done > 0:
                m = migration_stall_rounds(j.state_gb)
                j.stall += m
                j.migration_time_lost += m
                j.migrations += 1
            j.current_gpu = gpu
            _execute_round(j, round_len)
            if j.is_done() and j.finish_time is None:
                j.finish_time = t + round_len
        t += round_len

    for j in arrived:
        if j.is_done() and j.finish_time is None:
            j.finish_time = t
    return _collect_metrics(arrived, decision_latencies, fast_count=0)


def run_smart(jobs: List[Job], cluster: Dict[str, int],
              round_len: float = 1.0, max_rounds: int = 30000,
              brain_interval: float = 2.0, **smart_kwargs) -> Dict[str, float]:
    jobs = [Job(**{k: getattr(j, k) for k in
                   ("job_id", "arrival", "model", "d_j", "W_j")}) for j in jobs]

    sched = SmartScheduler(cluster, round_len=round_len, **smart_kwargs)
    decision_latencies: List[float] = []
    pending = sorted(jobs, key=lambda j: j.arrival)
    active: List[Job] = []
    t, idx = 0.0, 0
    last_brain = 0.0
    fast_count = 0

    for _ in range(max_rounds):
        while idx < len(pending) and pending[idx].arrival <= t:
            job = pending[idx]; idx += 1
            active.append(job)
            route = sched.admit(job, t)
            if route == "fast":
                job._decided = True
                decision_latencies.append(sched.last_dispatch_cost)
                fast_count += 1
            else:
                job._decided = False

        event = len(sched.global_queue) > 0
        if event or (t - last_brain) >= brain_interval:
            fast_jobs = [j for j in active
                         if j.current_gpu is not None and not j.is_done()]
            sched.run_brain(fast_jobs, t)
            last_brain = t
            for j in active:
                if not getattr(j, "_decided", False):
                    j._decided = True
                    decision_latencies.append(t - j.arrival)

        any_active = False
        for j in active:
            if j.is_done():
                continue
            any_active = True
            if j.current_gpu is not None:
                if j.first_exec_time is None:
                    j.first_exec_time = t
                _execute_round(j, round_len)
                if j.is_done() and j.finish_time is None:
                    j.finish_time = t + round_len
                    sched._release(j)
                    if sched.fast_used > 0:
                        sched.fast_used = max(0, sched.fast_used - j.d_j)

        if not any_active and idx >= len(pending) and not sched.global_queue:
            break
        t += round_len

    for j in active:
        if j.is_done() and j.finish_time is None:
            j.finish_time = t
    return _collect_metrics(active, decision_latencies, fast_count=fast_count,
                            handshake_rejects=sched.handshake_rejects)
