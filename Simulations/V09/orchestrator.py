"""
orchestrator.py — Option B run driver (executes INSIDE the compose network).

Replaces launcher.py's process-spawning role: in Option B the components are
already running as containers, so this only (1) waits until all node agents
have registered their slot tables, (2) replays the trace, (3) waits for
completion, (4) analyzes and saves metrics to the mounted ./runs volume,
(5) signals shutdown so `docker compose down` exits cleanly.

DYNAMIC-PHASE CHANGE (one):
  The adaptability metrics are computed HERE, before shutdown, and merged into
  the same result JSON. They have to be: they read the raw `metrics:events`
  log, and Redis runs without persistence, so `docker compose down` destroys
  it. Anything not extracted before shutdown is gone for good, and the run has
  to be repeated. For stationary regimes dynamic_metrics() returns {} and this
  file behaves exactly as before.

Usage (from the host):
  docker compose --profile smart up -d
  docker compose --profile tools run --rm orchestrator 60 bursty 1 smart 1800
  docker compose --profile smart down
"""
from __future__ import annotations

import json, os, sys, time

from common import R
from workload import GPU_TYPES
from trace_replayer import replay, trace_horizon
from analyze_results import collect, metrics


def main():
    jobs = int(sys.argv[1]); regime = sys.argv[2]; seed = int(sys.argv[3])
    mode = sys.argv[4] if len(sys.argv) > 4 else "run"
    timeout = float(sys.argv[5]) if len(sys.argv) > 5 else 1800.0
    r = R()

    print("waiting for node agents to register slot tables...")
    for _ in range(120):
        if all(r.exists(f"free:{g}") for g in GPU_TYPES):
            break
        time.sleep(0.5)
    else:
        sys.exit("agents never came up — check `docker compose ps` / logs")
    print("agents ready:", {g: r.get(f'free:{g}') for g in GPU_TYPES})

    replay(jobs, regime, seed)            # blocks until all arrivals fired
    print("replay done; waiting for jobs to finish...")

    t0 = time.time()
    while time.time() - t0 < timeout:
        total, done = r.get("total_jobs"), r.get("done_count")
        if total and done and int(done) >= int(total):
            print(f"all {total} jobs finished")
            break
        time.sleep(2.0)
    else:
        print("TIMEOUT — saving PARTIAL metrics (check n_finished!)")

    ev, job_recs = collect(r)
    m = metrics(ev, job_recs)

    # --- adaptability metrics, extracted before Redis is destroyed ----------
    try:
        from dynamic_metrics import dynamic_metrics
        m.update(dynamic_metrics(ev, job_recs, regime, seed, jobs,
                                 trace_horizon(jobs)))
    except Exception as e:                       # never lose the base metrics
        print(f"[orchestrator] dynamic metrics failed: {e}")
        m["dyn_error"] = repr(e)

    m["config"] = {
        "regime": regime, "seed": seed, "n_jobs": jobs, "mode": mode,
        "horizon_rounds": trace_horizon(jobs),
        "time_scale": os.environ.get("EMU_TIME_SCALE"),
        "n_per_type": os.environ.get("EMU_N_PER_TYPE"),
        "horizon_per_job": os.environ.get("EMU_HORIZON_PER_JOB", "1.4"),
    }
    m["agent_hosts"] = list(r.smembers("agent_hosts") or [])

    os.makedirs("runs", exist_ok=True)
    path = f"runs/{mode}_{regime}_s{seed}.json"
    json.dump(m, open(path, "w"), indent=2)

    # The backlog series is long; keep it out of the console dump.
    printable = {k: v for k, v in m.items() if k != "dyn_backlog_series"}
    print(json.dumps(printable, indent=2)); print("saved ->", path)
    r.set("shutdown", "1")


if __name__ == "__main__":
    main()
