"""
Main experiment: run both schedulers across workload modes + the burst-scaling
study, then produce publication-style charts mapped to the evaluation metrics.
"""
import time
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from workload import generate_trace, Job, MODEL_NAMES, MODEL_ZOO
from fft_baseline import FFTScheduler
from smart_scheduler import SmartScheduler
from engine import run_fft, run_smart

plt.rcParams.update({
    "figure.dpi": 130, "font.size": 10, "axes.grid": True,
    "grid.alpha": 0.3, "axes.axisbelow": True,
})
C_FFT, C_SMART = "#4C72B0", "#DD8452"
N_JOBS = 80
SEED = 7
MODES = ["steady", "mixed", "bursty"]


def run_all():
    results = {}
    for mode in MODES:
        trace = generate_trace(N_JOBS, seed=SEED, mode=mode)
        results[mode] = dict(fft=run_fft(trace), smart=run_smart(trace))
    return results


def burst_scaling():
    sizes = [10, 25, 50, 75, 100, 150, 200]
    fft_lat, smart_lat = [], []
    rng = np.random.default_rng(0)
    for b in sizes:
        jobs = []
        for jid in range(b):
            m = rng.choice(MODEL_NAMES)
            w = int(rng.choice(MODEL_ZOO[m]["typical_workers"]))
            e = float(np.clip(rng.lognormal(2.2, 0.9), 3, 200))
            jobs.append(Job(jid, 0, m, w, round(e, 1)))
        f = FFTScheduler()
        t0 = time.perf_counter(); f._solve_round(jobs, 1)
        fft_lat.append((time.perf_counter() - t0) * 1000)
        s = SmartScheduler()
        t0 = time.perf_counter()
        for j in jobs:
            s.dispatch(j, 0)
        smart_lat.append((time.perf_counter() - t0) / b * 1000)
    return sizes, fft_lat, smart_lat


def chart_metrics(results):
    """Grouped bars: JCT, makespan, FTF, starvation across modes."""
    fig, axes = plt.subplots(2, 2, figsize=(11, 8))
    metrics = [
        ("jct_mean", "Mean JCT (min)  -- lower is better"),
        ("makespan", "Makespan (min)  -- lower is better"),
        ("ftf_mean", "Mean Finish-Time-Fairness  -- closer to 1 is better"),
        ("starv_mean", "Mean Starvation (min)  -- lower is better"),
    ]
    x = np.arange(len(MODES)); w = 0.36
    for ax, (key, title) in zip(axes.ravel(), metrics):
        fft_v = [results[m]["fft"][key] for m in MODES]
        sm_v = [results[m]["smart"][key] for m in MODES]
        ax.bar(x - w/2, fft_v, w, label="FFT (centralized)", color=C_FFT)
        ax.bar(x + w/2, sm_v, w, label="Enhanced (yours)", color=C_SMART)
        ax.set_title(title, fontsize=10)
        ax.set_xticks(x); ax.set_xticklabels([m.capitalize() for m in MODES])
        ax.legend(fontsize=8)
        for i, (a, b) in enumerate(zip(fft_v, sm_v)):
            ax.text(i - w/2, a, f"{a:.0f}", ha="center", va="bottom", fontsize=7)
            ax.text(i + w/2, b, f"{b:.0f}", ha="center", va="bottom", fontsize=7)
    fig.suptitle(f"Scheduler comparison across workload regimes ({N_JOBS} jobs)", fontweight="bold")
    fig.tight_layout()
    fig.savefig("/home/claude/sim/out_metrics.png", bbox_inches="tight")
    plt.close(fig)


def chart_admission(results, sizes, fft_lat, smart_lat):
    """The headline result: admission latency, flat vs growing."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.5))

    # left: mean admission latency under the bursty trace
    fft_a = [results[m]["fft"]["admit_lat_mean"] for m in MODES]
    sm_a = [results[m]["smart"]["admit_lat_mean"] for m in MODES]
    x = np.arange(len(MODES)); w = 0.36
    ax1.bar(x - w/2, fft_a, w, label="FFT (centralized)", color=C_FFT)
    ax1.bar(x + w/2, sm_a, w, label="Enhanced (yours)", color=C_SMART)
    ax1.set_yscale("log")
    ax1.set_ylabel("Mean admission latency (ms, log)")
    ax1.set_title("Admission latency by regime")
    ax1.set_xticks(x); ax1.set_xticklabels([m.capitalize() for m in MODES])
    ax1.legend(fontsize=8)

    # right: scaling with burst size
    ax2.plot(sizes, fft_lat, "o-", color=C_FFT, label="FFT: ILP solve grows with burst")
    ax2.plot(sizes, smart_lat, "s-", color=C_SMART, label="Enhanced: O(1) dispatch, flat")
    ax2.set_yscale("log")
    ax2.set_xlabel("Burst size (simultaneous arrivals)")
    ax2.set_ylabel("Admission latency (ms, log)")
    ax2.set_title("Admission latency vs burst size  (core claim)")
    ax2.legend(fontsize=8)
    fig.suptitle("Decoupled admission: the central contribution", fontweight="bold")
    fig.tight_layout()
    fig.savefig("/home/claude/sim/out_admission.png", bbox_inches="tight")
    plt.close(fig)


def chart_distributions(results):
    """CDFs of FTF and JCT for the bursty regime (mirrors FFT paper Fig 8/9)."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.5))
    r = results["bursty"]
    for label, data, color in [("FFT", r["fft"]["ftf_raw"], C_FFT),
                               ("Enhanced", r["smart"]["ftf_raw"], C_SMART)]:
        xs = np.sort(data); ys = np.linspace(0, 1, len(xs))
        ax1.plot(xs, ys, label=label, color=color, lw=2)
    ax1.set_xlabel("Finish-Time-Fairness  (T_sh / T_id)")
    ax1.set_ylabel("CDF"); ax1.set_title("FTF distribution (bursty)")
    ax1.axvline(1.0, ls="--", c="gray", alpha=0.6); ax1.legend(fontsize=8)
    ax1.set_xlim(0, np.percentile(r["smart"]["ftf_raw"], 95))

    for label, data, color in [("FFT", r["fft"]["jct_raw"], C_FFT),
                               ("Enhanced", r["smart"]["jct_raw"], C_SMART)]:
        xs = np.sort(data); ys = np.linspace(0, 1, len(xs))
        ax2.plot(xs, ys, label=label, color=color, lw=2)
    ax2.set_xlabel("JCT (min)"); ax2.set_ylabel("CDF")
    ax2.set_title("JCT distribution (bursty)"); ax2.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig("/home/claude/sim/out_distributions.png", bbox_inches="tight")
    plt.close(fig)


def print_table(results):
    print("\n" + "=" * 78)
    print(f"{'Metric':<28}{'Regime':<10}{'FFT':>14}{'Enhanced':>14}")
    print("-" * 78)
    rows = [("Mean JCT (min)", "jct_mean"), ("Makespan (min)", "makespan"),
            ("Mean FTF", "ftf_mean"), ("Max FTF", "ftf_max"),
            ("Mean starvation (min)", "starv_mean"),
            ("Admission lat (ms)", "admit_lat_mean")]
    for name, key in rows:
        for m in MODES:
            f = results[m]["fft"][key]; s = results[m]["smart"][key]
            print(f"{name:<28}{m:<10}{f:>14.3f}{s:>14.3f}")
        print()


if __name__ == "__main__":
    print("Running scheduler comparison ...")
    results = run_all()
    print("Running burst-scaling study ...")
    sizes, fft_lat, smart_lat = burst_scaling()
    chart_metrics(results)
    chart_admission(results, sizes, fft_lat, smart_lat)
    chart_distributions(results)
    print_table(results)
    print("Charts saved: out_metrics.png, out_admission.png, out_distributions.png")
