"""
compare_all.py — Aggregate and compare all FFT vs SMART runs across all regimes and seeds.

Usage:
  python compare_all.py
"""
import glob, json, os
import numpy as np

REGIMES = ["bursty", "dynamic", "heavy", "mixed", "random", "steady"]
METRICS = [
    ("jct_mean_rounds", "Mean JCT (rounds)", "lower"),
    ("starvation_mean_rounds", "Starvation Mean (rounds)", "lower"),
    ("ftf_mean", "Finish-Time Fairness (FTF)", "lower"),
    ("decision_latency_ms_mean", "Decision Latency (ms)", "speedup"),
    ("migrations", "Total Migrations", "lower"),
    ("profile_stalls", "Profile Stalls", "lower"),
    ("fast_path_frac", "Fast-Path Fraction", "higher"),
    ("preemptions", "Preemptions", "lower"),
]

def load_regime_stats(label, regime):
    files = glob.glob(f"runs/{label}_{regime}_s*.json")
    if not files:
        return None
    data = [json.load(open(f)) for f in files]
    stats = {}
    for key, _, _ in METRICS:
        vals = [d.get(key, 0) for d in data if key in d]
        stats[key] = np.mean(vals) if vals else 0.0
    return stats

def main():
    print("=" * 90)
    print("      COMPREHENSIVE CLUSTER EVALUATION: BASELINE (FFT) vs PROPOSED (SMART)")
    print("=" * 90)

    for regime in REGIMES:
        fft_data = load_regime_stats("fft", regime)
        smart_data = load_regime_stats("smart", regime)

        if not fft_data or not smart_data:
            continue

        print(f"\n>> REGIME: {regime.upper()} (Averaged across seeds)")
        print(f"{'Metric':<30} {'FFT (Baseline)':>18} {'SMART (Proposed)':>18} {'Comparison':>18}")
        print("-" * 90)

        for key, name, mode in METRICS:
            f_val = fft_data.get(key, 0)
            s_val = smart_data.get(key, 0)

            if mode == "speedup":
                comp = f"{f_val / max(1e-6, s_val):.1f}x Faster"
            elif mode == "lower":
                diff = ((s_val - f_val) / max(1e-6, f_val)) * 100
                comp = f"{diff:+.1f}%"
            elif mode == "higher":
                comp = f"{s_val*100:.1f}% fast-path"
            else:
                comp = ""

            print(f"{name:<30} {f_val:>18.3f} {s_val:>18.3f} {comp:>18}")

    print("\n" + "=" * 90)

if __name__ == "__main__":
    main()
