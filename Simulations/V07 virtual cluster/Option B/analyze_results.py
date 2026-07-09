"""
analyze_results.py — turn the telemetry into thesis metrics.

All times converted back to ROUNDS of scaled time so numbers are directly
comparable with scheduler_simulation_v2 output (the validation figure).
Decision latency is reported in REAL milliseconds too — the stopwatch
measurement of your gap (O(1) dispatch vs round-gated central solve).

  python analyze_results.py --mode smart --save runs/smart.json
  python analyze_results.py --compare runs/fft.json runs/smart.json
"""
from __future__ import annotations
import argparse, json, os, statistics
from common import R
from config import real_to_rounds

def collect(r):
    ev = [json.loads(x) for x in r.lrange("metrics:events", 0, -1)]
    jobs = {}
    for jid_key in r.keys("jobs:*"):
        h = r.hgetall(jid_key)
        j = {k: json.loads(v) for k, v in h.items()}
        jobs[j["id"]] = j
    return ev, jobs

def metrics(ev, jobs):
    fin = [j for j in jobs.values() if j.get("finish_ts")]
    jct = [real_to_rounds(j["finish_ts"] - j["arrival_ts"]) for j in fin]
    ftf = []
    for j in fin:
        best = max(j["theta"].values())
        ideal = j["W"] / max(1e-6, best)
        ftf.append(real_to_rounds(j["finish_ts"] - j["arrival_ts"]) / max(1e-6, ideal))
    starv = [real_to_rounds(j["first_exec_ts"] - j["arrival_ts"])
             for j in fin if j.get("first_exec_ts")]
    dlat_real = [e["latency"] for e in ev if e["type"] == "decision"]
    solves = [e["wall"] for e in ev if e["type"] in ("brain_solve", "fft_solve")]
    mean = lambda xs: statistics.mean(xs) if xs else 0.0
    return {
        "n_jobs": len(jobs), "n_finished": len(fin),
        "jct_mean_rounds": mean(jct),
        "ftf_mean": mean(ftf),
        "starvation_mean_rounds": mean(starv),
        "decision_latency_ms_mean": 1000 * mean(dlat_real),
        "decision_latency_rounds_mean": mean([real_to_rounds(x) for x in dlat_real]),
        "solve_wall_ms_mean": 1000 * mean(solves),
        "fast_path_frac": sum(1 for e in ev if e["type"] == "decision"
                              and e.get("route") == "fast") / max(1, len(dlat_real)),
        "handshake_rejects": sum(1 for e in ev if e["type"] == "handshake_reject"),
        "preemptions": sum(1 for e in ev if e["type"] == "preempted"),
        "migrations": sum(1 for e in ev if e["type"] == "migration"),
        "profile_stalls": sum(1 for e in ev if e["type"] == "profile_stall"),
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="run")
    ap.add_argument("--save")
    ap.add_argument("--compare", nargs=2)
    a = ap.parse_args()
    if a.compare:
        f = json.load(open(a.compare[0])); s = json.load(open(a.compare[1]))
        print(f"{'metric':32} {'FFT':>12} {'smart':>12} {'ratio/speedup':>14}")
        rows = [("jct_mean_rounds", "ratio"), ("ftf_mean", "ratio"),
                ("starvation_mean_rounds", "ratio"),
                ("decision_latency_ms_mean", "speedup"),
                ("solve_wall_ms_mean", "-"), ("fast_path_frac", "-"),
                ("handshake_rejects", "-"), ("preemptions", "-"),
                ("migrations", "-"), ("profile_stalls", "-")]
        for k, kind in rows:
            fv, sv = f.get(k, 0), s.get(k, 0)
            extra = (f"x{sv/max(1e-9,fv):.2f}" if kind == "ratio" else
                     f"x{fv/max(1e-9,sv):.1f}" if kind == "speedup" else "")
            print(f"{k:32} {fv:12.3f} {sv:12.3f} {extra:>14}")
        return
    r = R()
    ev, jobs = collect(r)
    m = metrics(ev, jobs)
    print(json.dumps(m, indent=2))
    if a.save:
        os.makedirs(os.path.dirname(a.save), exist_ok=True)
        json.dump(m, open(a.save, "w"), indent=2)
        print("saved ->", a.save)

if __name__ == "__main__":
    main()
