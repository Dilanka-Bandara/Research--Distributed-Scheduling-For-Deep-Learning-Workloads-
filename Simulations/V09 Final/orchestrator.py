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

import glob, gzip, hashlib, json, os, sys, time

import redis as _redis_pkg

from common import R, code_fingerprint, node_label, sync_clock
from workload import GPU_TYPES
from trace_replayer import replay, trace_horizon
from analyze_results import collect, metrics


def code_fingerprint() -> dict:
    """md5 of every .py in the image. V09 exists partly because a result file
    was produced by a different copy of arrival_process.py than the one being
    analysed, and nothing in the JSON could reveal it. Now every result names
    the exact code that produced it, and PC 1 vs PC 2 mismatches are visible."""
    here = os.path.dirname(os.path.abspath(__file__))
    files = sorted(glob.glob(os.path.join(here, "*.py")))
    h = hashlib.md5()
    per = {}
    for f in files:
        b = open(f, "rb").read()
        h.update(os.path.basename(f).encode()); h.update(b)
        per[os.path.basename(f)] = hashlib.md5(b).hexdigest()[:10]
    return {"all": h.hexdigest()[:12], "files": per}


def scheduler_health(ev) -> dict:
    """Watchdog for every regime, stationary ones included."""
    from config import real_to_rounds
    ts = sorted(e["ts"] for e in ev if e.get("type") in ("brain_solve", "fft_solve"))
    gaps = [real_to_rounds(b - a) for a, b in zip(ts, ts[1:])]
    return {
        "sched_solves": len(ts),
        "sched_max_gap_rounds": max(gaps) if gaps else None,
        "sched_gaps_over_2_rounds": sum(1 for g in gaps if g > 2.0),
        "rpc_bad_target": sum(1 for e in ev if e.get("type") == "rpc_bad_target"),
        "rpc_timeouts": sum(1 for e in ev if e.get("type") == "rpc_timeout"),
    }


def main():
    jobs = int(sys.argv[1]); regime = sys.argv[2]; seed = int(sys.argv[3])
    mode = sys.argv[4] if len(sys.argv) > 4 else "run"
    timeout = float(sys.argv[5]) if len(sys.argv) > 5 else 1800.0
    r = R()
    sync_clock(r)

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
    for k, v in scheduler_health(ev).items():
        m.setdefault(k, v)
    m["agent_hosts"] = sorted(r.smembers("agent_hosts") or [])
    m["provenance"] = {
        "code": code_fingerprint(),
        "redis_py": _redis_pkg.__version__,
        "orchestrator_node": node_label(),
        # per-process clock offset vs the Redis clock, measured at sync time
        "clock": {k: json.loads(v) for k, v in (r.hgetall("clock") or {}).items()},
    }
    mine = code_fingerprint()
    theirs = {h.rsplit(":", 1)[-1] for h in m["agent_hosts"]}
    m["provenance"]["code_match"] = theirs == {mine}
    if theirs != {mine}:
        print(f"WARNING: agents run different code ({theirs}) from PC 1 ({mine}). "
              f"Re-sync and rebuild PC 2. This run is NOT a valid result.")
    if m["sched_gaps_over_2_rounds"]:
        print(f"WARNING: scheduler stalled > 2 rounds "
              f"{m['sched_gaps_over_2_rounds']} time(s) "
              f"(max gap {m['sched_max_gap_rounds']:.1f} rounds). "
              f"This run is NOT a valid result.")

    os.makedirs("runs/raw", exist_ok=True)
    path = f"runs/{mode}_{regime}_s{seed}.json"
    json.dump(m, open(path, "w"), indent=2)
    # Raw telemetry is always kept (gzip, typically < 1 MB). Every anomaly in
    # V08 was diagnosable only by re-running with a patched orchestrator.
    with gzip.open(f"runs/raw/{mode}_{regime}_s{seed}.events.json.gz", "wt") as fh:
        json.dump({"events": ev, "jobs": job_recs}, fh)

    # The backlog series is long; keep it out of the console dump.
    printable = {k: v for k, v in m.items() if k != "dyn_backlog_series"}
    print(json.dumps(printable, indent=2)); print("saved ->", path)
    r.set("shutdown", "1")


if __name__ == "__main__":
    main()
