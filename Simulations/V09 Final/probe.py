"""
probe.py — component 8, the cluster-state sampler (new, dynamic phase only).

Samples free slots and queue depth on a fixed cadence and writes them into
`metrics:events` as {"type": "probe", ...}. It never issues an RPC, never takes
a lock and never touches a job record, so it cannot influence scheduling — it
only reads.

WHY IT EXISTS
-------------
Backlog over time can be reconstructed exactly from the arrival/placed events
(see dynamic_metrics.backlog_series), so the probe is NOT needed for that. What
cannot be reconstructed is FREE CAPACITY over time, because slot occupancy is
mutated by reserve/evict/finish paths inside the agents on PC 2. Free capacity
is what the single most damning metric needs:

    stranded capacity = GPU-rounds that sat idle WHILE jobs were queued

Idle GPUs with a non-empty queue is a pure work-conservation violation. For a
round-gated centralized scheduler it is structural: a GPU that frees at the
start of a round cannot be given to a waiting job until the next solve, so the
capacity is simply lost. That is the research gap made visible as a number, and
it needs a real measurement of free:<type> over time.

COST
----
One pipelined Redis round trip per sample. At 5 Hz over a 10-minute run that is
~3000 samples and ~3000 small RPUSHes — negligible beside the tens of thousands
of events the schedulers themselves emit, and identical for both schedulers, so
it cannot bias the comparison. Run it in BOTH profiles or in neither.

Usage (inside the compose network):
    python probe.py [hz]
"""
from __future__ import annotations

import json
import sys
import time

from common import R, now
from workload import GPU_TYPES

DEFAULT_HZ = 5.0


def run(hz: float = DEFAULT_HZ):
    r = R()
    period = 1.0 / hz
    # Wait for the agents to publish their slot tables before sampling, so the
    # series does not open with a run of spurious zeros.
    for _ in range(240):
        if all(r.exists(f"free:{g}") for g in GPU_TYPES):
            break
        time.sleep(0.5)

    while r.get("shutdown") != "1":
        t_start = time.time()
        try:
            pipe = r.pipeline(transaction=False)
            for g in GPU_TYPES:
                pipe.get(f"free:{g}")
            for g in GPU_TYPES:
                pipe.scard(f"running:{g}")
            pipe.llen("queue:global")
            pipe.llen("arrivals")
            vals = pipe.execute()

            n = len(GPU_TYPES)
            free = {g: int(vals[i] or 0) for i, g in enumerate(GPU_TYPES)}
            running = {g: int(vals[n + i] or 0) for i, g in enumerate(GPU_TYPES)}
            q_slow, q_arr = int(vals[2 * n] or 0), int(vals[2 * n + 1] or 0)

            r.rpush("metrics:events", json.dumps({
                "type": "probe",
                "ts": now(),
                "free": free,
                "free_total": sum(free.values()),
                "running": running,
                # Both parking spots, because FFT leaves new jobs in `arrivals`
                # until its round boundary while SMART moves them to
                # `queue:global`. Summing them makes the two comparable.
                "q_slow": q_slow,
                "q_arrivals": q_arr,
                "q_total": q_slow + q_arr,
            }))
        except Exception:
            # The probe is instrumentation: it must never take down a run.
            time.sleep(period)
            continue

        slack = period - (time.time() - t_start)
        if slack > 0:
            time.sleep(slack)


if __name__ == "__main__":
    run(float(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_HZ)
