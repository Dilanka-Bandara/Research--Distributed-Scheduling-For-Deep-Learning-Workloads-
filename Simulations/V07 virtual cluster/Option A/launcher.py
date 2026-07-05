"""
launcher.py — start the virtual cluster for one run.

  python launcher.py --scheduler smart --jobs 40 --regime bursty --seed 1
  python launcher.py --scheduler fft   --jobs 40 --regime bursty --seed 1

Spawns: 3 node agents (T4/V100/A10) + trace replayer + either
(dispatcher + brain) or the centralised FFT process. Waits until
done_count == total_jobs, then sets shutdown and runs the analyzer.
"""
from __future__ import annotations
import argparse, subprocess, sys, time
from common import R
from workload import GPU_TYPES
from config import N_PER_TYPE

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scheduler", choices=["smart", "fft"], required=True)
    ap.add_argument("--jobs", type=int, default=40)
    ap.add_argument("--regime", default="bursty")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=600.0)
    a = ap.parse_args()

    r = R(); r.flushdb()
    py = sys.executable
    procs = []
    def spawn(*args):
        procs.append(subprocess.Popen([py, *args]))
    for g in GPU_TYPES:
        spawn("node_agent.py", g, str(N_PER_TYPE))
    time.sleep(0.5)
    if a.scheduler == "smart":
        spawn("dispatcher.py"); spawn("brain.py")
    else:
        spawn("fft_scheduler_proc.py")
    time.sleep(0.5)
    spawn("trace_replayer.py", str(a.jobs), a.regime, str(a.seed))

    t0 = time.time()
    try:
        while time.time() - t0 < a.timeout:
            total, done = r.get("total_jobs"), r.get("done_count")
            if total and done and int(done) >= int(total):
                print(f"all {total} jobs finished in {time.time()-t0:.1f}s wall")
                break
            time.sleep(1.0)
        else:
            print("TIMEOUT — see analyze_results.py for partial data")
    finally:
        r.set("shutdown", "1")
        time.sleep(1.5)
        for p in procs:
            p.terminate()
    subprocess.run([py, "analyze_results.py", "--mode", a.scheduler,
                    "--save", f"runs/{a.scheduler}_{a.regime}_s{a.seed}.json"])

if __name__ == "__main__":
    main()
