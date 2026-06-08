"""
Discrete-event simulation engine.

Runs a given scheduler over a fixed trace, round by round, until all jobs finish.
Collects: JCT, makespan, Finish-Time-Fairness (FTF), starvation, admission latency,
and per-round solver time (the centralization cost).

Both schedulers are driven by the same loop so the comparison is apples-to-apples.
"""

import copy
import numpy as np

from workload import GPU_TYPES, GPU_COUNT, M, ROUND_MIN
from fft_baseline import FFTScheduler
from smart_scheduler import SmartScheduler


def run_fft(trace, max_rounds=20000):
    sched = FFTScheduler()
    jobs = copy.deepcopy(trace)
    by_arrival = {}
    for j in jobs:
        by_arrival.setdefault(j.arrival, []).append(j)

    active, done = [], []
    t = 0
    while (active or any(a >= t for a in by_arrival)) and t < max_rounds:
        # admit all jobs that have arrived by round t
        if t in by_arrival:
            active.extend(by_arrival[t])

        if active:
            alloc = sched.step(active, t)
            if alloc is not None:
                # advance progress for scheduled jobs; pay migration if type changed
                for j_idx, job in enumerate(active):
                    chosen = np.where(alloc[j_idx] == 1)[0]
                    if len(chosen):
                        i = int(chosen[0])
                        if job.start_round is None:
                            job.start_round = t
                        eff = 1.0
                        if job.current_type is not None and job.current_type != i:
                            from workload import migration_rounds
                            eff = max(0.0, 1.0 - migration_rounds(job.state_gb))
                        job.progress += job.theta[i] * eff
                        job.current_type = i
                sched.update_fairness(active, alloc, t)

            # retire finished jobs
            still = []
            for job in active:
                if job.remaining <= 0 and job.finish_round is None:
                    job.finish_round = t + 1
                    done.append(job)
                else:
                    still.append(job)
            active = still
        t += 1

    return _metrics(done, sched, t)


def run_smart(trace, max_rounds=20000):
    sched = SmartScheduler()
    jobs = copy.deepcopy(trace)
    by_arrival = {}
    for j in jobs:
        by_arrival.setdefault(j.arrival, []).append(j)

    fast_running, slow_queue, done = [], [], []
    t = 0
    while (fast_running or slow_queue or any(a >= t for a in by_arrival)) and t < max_rounds:
        # ---- O(1) dispatch for every arrival this round ----
        if t in by_arrival:
            for job in by_arrival[t]:
                matched, gtype = sched.dispatch(job, t)
                if matched:
                    sched.place_fast(job, gtype, t)
                    fast_running.append(job)
                else:
                    sched.admit_slow(job)
                    slow_queue.append(job)

        # ---- Fast path jobs advance every round on their locked slot ----
        for job in fast_running:
            if job.current_type is not None:
                job.progress += job.theta[job.current_type]

        # ---- Slow-path jobs that already hold a slot advance every round ----
        for job in slow_queue:
            if job.current_type is not None and getattr(job, "placed", False):
                # micro-profile cost: cache-MISS job loses its very first round
                if not getattr(job, "profiled", True):
                    job.profiled = True
                    continue
                if job.start_round is None:
                    job.start_round = t
                job.progress += job.theta[job.current_type]

        # ---- Async FFT (background brain): manages the SLOW-PATH queue ----
        # Batched + starvation-prioritized inside async_optimize. Fast-path jobs keep
        # the slot they were instantly admitted to (the deliberate JCT/fairness cost
        # of O(1) decoupled admission). A hard global fast-budget guarantees the slow
        # path always retains capacity, so the optimizer is never fully starved.
        ASYNC_PERIOD = 2
        if slow_queue and (t % ASYNC_PERIOD == 0 or t == 0):
            alloc, active = sched.async_optimize(slow_queue, t)
            if alloc is not None:
                for j_idx, job in enumerate(active):
                    chosen = np.where(alloc[j_idx] == 1)[0]
                    if len(chosen):
                        i = int(chosen[0])
                        # Decentralised Handshake: verify real availability before commit
                        if sched.handshake(job, i):
                            job.placed = True
                sched.update_fairness(active, alloc, t)

        # ---- retire finished jobs from both pools, release their slots ----
        nf = []
        for job in fast_running:
            if job.remaining <= 0 and job.finish_round is None:
                job.finish_round = t + 1
                sched.free[job.current_type] += job.workers
                sched.fast_workers -= job.workers
                done.append(job)
            else:
                nf.append(job)
        fast_running = nf

        nq = []
        for job in slow_queue:
            if job.remaining <= 0 and job.finish_round is None:
                job.finish_round = t + 1
                if job.current_type is not None:
                    sched.free[job.current_type] += job.workers
                done.append(job)
            else:
                nq.append(job)
        slow_queue = nq
        t += 1

    return _metrics(done, sched, t)


def _metrics(done, sched, final_round):
    if not done:
        return None
    jcts = np.array([(j.finish_round - j.arrival) for j in done], dtype=float)
    # Finish-Time-Fairness: T_sh / T_id
    ftf = np.array([
        (j.finish_round - j.arrival) / max(j.ideal_jct_rounds(), 1e-9) for j in done
    ])
    # starvation: arrival -> first execution
    starv = np.array([
        ((j.start_round if j.start_round is not None else j.finish_round) - j.arrival)
        for j in done
    ], dtype=float)

    return dict(
        scheduler=sched.name,
        n_done=len(done),
        jct_mean=jcts.mean() * ROUND_MIN,
        jct_p95=np.percentile(jcts, 95) * ROUND_MIN,
        makespan=final_round * ROUND_MIN,
        ftf_mean=ftf.mean(),
        ftf_max=ftf.max(),
        starv_mean=starv.mean() * ROUND_MIN,
        starv_max=starv.max() * ROUND_MIN,
        admit_lat_mean=np.mean(sched.admission_lat) * 1000 if sched.admission_lat else 0.0,  # ms
        solve_time_mean=np.mean(sched.solve_times) * 1000 if sched.solve_times else 0.0,      # ms
        solve_time_max=np.max(sched.solve_times) * 1000 if sched.solve_times else 0.0,        # ms
        jct_raw=jcts * ROUND_MIN,
        ftf_raw=ftf,
        starv_raw=starv * ROUND_MIN,
        solve_raw=np.array(sched.solve_times) * 1000,
        fast_count=getattr(sched, "fast_count", None),
        slow_count=getattr(sched, "slow_count", None),
    )
