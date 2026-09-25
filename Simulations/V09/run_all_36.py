"""
run_all_36.py — Master Automated Test Runner for Distributed 36-GPU Simulation.

Runs all 36 workload combinations across PC 1 and PC 2:
  2 Modes (smart, fft) x 6 Regimes (bursty, dynamic, heavy, mixed, random, steady) x 3 Seeds (1, 2, 3) = 36 runs.

Usage:
  python run_all_36.py              # Run all 36 runs automatically
  python run_all_36.py --mode smart # Run only 18 runs for SMART scheduler
  python run_all_36.py --mode fft   # Run only 18 runs for FFT baseline
  python run_all_36.py --force      # Re-run completed runs (default resumes/skips completed)
"""
import argparse, json, os, subprocess, sys, time

ALL_MODES = ["smart", "fft"]
ALL_REGIMES = ["bursty", "dynamic", "heavy", "mixed", "random", "steady"]
ALL_SEEDS = [1, 2, 3]

COMPOSE_FILE = "docker-compose.pc1.yml"

def run_cmd(cmd, check=True):
    """Run shell command and return CompletedProcess."""
    return subprocess.run(cmd, shell=True, check=check)

def is_run_completed(mode, regime, seed, jobs):
    """Check if output json already exists and finished successfully."""
    path = f"runs/{mode}_{regime}_s{seed}.json"
    if not os.path.exists(path):
        return False
    try:
        data = json.load(open(path))
        return data.get("n_finished", 0) >= jobs
    except Exception:
        return False

def clean_shutdown(mode):
    """Safely bring down PC 1 containers."""
    try:
        subprocess.run(
            f"docker compose -f {COMPOSE_FILE} --profile {mode} down",
            shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:
        pass

def main():
    parser = argparse.ArgumentParser(description="Automate all 36 cluster runs across PC 1 and PC 2.")
    parser.add_argument("--mode", choices=["all", "smart", "fft"], default="all", help="Scheduler mode to run")
    parser.add_argument("--regime", choices=["all"] + ALL_REGIMES, default="all", help="Specific regime or all")
    parser.add_argument("--seeds", default="1,2,3", help="Comma-separated seeds, e.g. '1,2,3'")
    parser.add_argument("--jobs", type=int, default=60, help="Number of jobs per run (default: 60)")
    parser.add_argument("--timeout", type=int, default=1800, help="Timeout in seconds per run")
    parser.add_argument("--force", action="store_true", help="Force re-run even if run output file exists")
    args = parser.parse_args()

    modes = ALL_MODES if args.mode == "all" else [args.mode]
    regimes = ALL_REGIMES if args.regime == "all" else [args.regime]
    seeds = [int(s.strip()) for s in args.seeds.split(",")]

    total_runs = len(modes) * len(regimes) * len(seeds)
    print("=" * 80)
    print(f"   STARTING AUTOMATED EXPERIMENT SUITE ({total_runs} TOTAL RUNS)")
    print(f"   Modes:   {modes}")
    print(f"   Regimes: {regimes}")
    print(f"   Seeds:   {seeds}")
    print(f"   Jobs:    {args.jobs} per run | Timeout: {args.timeout}s")
    print("=" * 80)

    current_idx = 0
    completed_count = 0
    skipped_count = 0

    current_mode = None

    try:
        for mode in modes:
            for regime in regimes:
                for seed in seeds:
                    current_idx += 1
                    label = f"[{current_idx}/{total_runs}] {mode.upper()} | {regime} | seed {seed}"
                    
                    if not args.force and is_run_completed(mode, regime, seed, args.jobs):
                        print(f"\n>> {label} -> ALREADY COMPLETED (Skipping)")
                        skipped_count += 1
                        continue

                    print(f"\n" + "=" * 80)
                    print(f"   RUNNING {label}")
                    print(f"=" * 80)

                    current_mode = mode

                    # Step 1: Start PC 1 stack
                    print(f"[*] Starting PC 1 stack (profile: {mode})...")
                    run_cmd(f"docker compose -f {COMPOSE_FILE} --profile {mode} up -d")

                    # Step 2: Give 3 seconds for Redis to be fully healthy and PC 2 agents to discover it
                    time.sleep(3)

                    # Step 3: Launch the orchestrator run
                    print(f"[*] Launching orchestrator for {args.jobs} jobs...")
                    cmd = (
                        f"docker compose -f {COMPOSE_FILE} --profile tools "
                        f"run --rm orchestrator {args.jobs} {regime} {seed} {mode} {args.timeout}"
                    )
                    res = run_cmd(cmd, check=False)

                    # Step 4: Cleanly bring down PC 1 stack to ensure fresh state for next seed
                    print(f"[*] Resetting PC 1 cluster state...")
                    run_cmd(f"docker compose -f {COMPOSE_FILE} --profile {mode} down")

                    if res.returncode == 0:
                        completed_count += 1
                        print(f"[+] Run {current_idx}/{total_runs} finished successfully.")
                    else:
                        print(f"[-] Run {current_idx}/{total_runs} failed or timed out (code: {res.returncode}).")

                    # Step 5: Brief cooldown before starting next run
                    time.sleep(3)

    except KeyboardInterrupt:
        print("\n[!] Execution interrupted by user (Ctrl+C). Cleaning up containers...")
        if current_mode:
            clean_shutdown(current_mode)
        sys.exit(1)

    print("\n" + "=" * 80)
    print(f"   EXPERIMENT SUITE FINISHED!")
    print(f"   Completed: {completed_count} | Skipped (already done): {skipped_count} | Total: {total_runs}")
    print("=" * 80)

    # Automatically generate the summary comparison table
    print("\nGenerating final comparative results...")
    try:
        run_cmd("python compare_all.py", check=False)
    except Exception as e:
        print(f"Could not run compare_all.py automatically: {e}")

if __name__ == "__main__":
    main()
