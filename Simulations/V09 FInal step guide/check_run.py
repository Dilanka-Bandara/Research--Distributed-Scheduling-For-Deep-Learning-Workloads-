"""
check_run.py — is this result file a valid, quotable-candidate run?

Runs on the PC 1 host with plain Python (no redis, numpy or Docker needed).

    python check_run.py                      # every runs/*.json
    python check_run.py runs/smart_dyn_flash_s1.json
    python check_run.py --expect-pc2         # also require agents labelled PC2

Exit code 0 = every file passed, 1 = something failed.
"""
from __future__ import annotations

import glob
import json
import sys


def check(path: str, expect_pc2: bool) -> bool:
    try:
        d = json.load(open(path))
    except Exception as e:
        print(f"\n{path}\n  FAIL  unreadable: {e}")
        return False

    cfg = d.get("config") or {}
    prov = d.get("provenance") or {}
    hosts = d.get("agent_hosts") or []
    clock = prov.get("clock") or {}
    checks = []

    n, fin = d.get("n_jobs", 0), d.get("n_finished", 0)
    checks.append((fin >= n > 0, f"all jobs finished ({fin}/{n})"))

    gaps = d.get("sched_gaps_over_2_rounds")
    mg = d.get("sched_max_gap_rounds")
    checks.append((gaps == 0,
                   f"scheduler never stalled (gaps>2 rounds: {gaps}, "
                   f"longest {mg if mg is None else round(mg, 2)} rounds)"))
    checks.append((d.get("rpc_bad_target", 1) == 0,
                   f"no RPCs to a missing agent ({d.get('rpc_bad_target')})"))
    checks.append((d.get("rpc_timeouts", 1) == 0,
                   f"no RPC timeouts ({d.get('rpc_timeouts')})"))
    checks.append(("dyn_error" not in d, "adaptability metrics computed"))
    checks.append((prov.get("code_match") is True,
                   f"PC 1 and agents run identical code "
                   f"(code_match={prov.get('code_match')})"))
    checks.append((len(hosts) == 3, f"three agents registered ({len(hosts)})"))
    checks.append((str(cfg.get("n_per_type")) == "12",
                   f"36-GPU cluster (N_PER_TYPE={cfg.get('n_per_type')})"))
    checks.append((str(cfg.get("time_scale")) == "100",
                   f"TIME_SCALE=100 ({cfg.get('time_scale')})"))
    if expect_pc2:
        checks.append((all(h.startswith("PC2:") for h in hosts) and hosts,
                       f"agents ran on PC 2 ({hosts})"))

    ok = all(c for c, _ in checks)
    print(f"\n{path}   [{cfg.get('mode')} | {cfg.get('regime')} | "
          f"seed {cfg.get('seed')} | {n} jobs]   -> {'PASS' if ok else 'FAIL'}")
    for c, msg in checks:
        print(f"  {'ok  ' if c else 'FAIL'}  {msg}")

    # Informational: the real PC1/PC2 clock offset V09 corrected for.
    offs = sorted({round(v.get("offset_ms", 0)) for v in clock.values()})
    if offs:
        print(f"  info  clock offsets vs Redis clock (ms): {offs}"
              f"   <- record the PC 2 value for the thesis")
    return ok


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    expect_pc2 = "--expect-pc2" in sys.argv
    files = args or sorted(glob.glob("runs/*.json"))
    if not files:
        print("no result files found under runs/")
        sys.exit(1)
    results = [check(f, expect_pc2) for f in files]
    bad = results.count(False)
    print(f"\n{len(results) - bad}/{len(results)} passed")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
