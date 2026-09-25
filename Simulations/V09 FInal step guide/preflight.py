import os, sys, time, statistics
from common import R, rpc
from workload import GPU_TYPES
from config import N_PER_TYPE

def main():
    print("=" * 60)
    print("   PREFLIGHT CHECK: Two-Machine Distributed Emulation")
    print("=" * 60)

    # 1. Connect to Redis
    try:
        r = R()
        r.ping()
        print("[+] Redis reachable on PC 1")
    except Exception as e:
        print(f"[-] Redis unreachable: {e}")
        print("    Did you run 'docker compose -f docker-compose.pc1.yml up -d redis'?")
        sys.exit(1)

    # 2. Measure Redis RTT (Thesis Requirement)
    rtts = []
    for _ in range(10):
        t0 = time.time()
        r.ping()
        rtts.append((time.time() - t0) * 1000)
    print(f"[+] Redis RTT on PC 1: {statistics.mean(rtts):.3f} ms (min: {min(rtts):.3f}, max: {max(rtts):.3f})")

    # 3. Check for PC 2 Agents
    print("\n[*] Waiting for PC 2 agents to connect (max 10s)...")
    r.set("shutdown", "0")  # Ensure they aren't waiting to reset
    agents_ready = 0
    for _ in range(20):
        agents_ready = sum(1 for g in GPU_TYPES if r.exists(f"free:{g}"))
        if agents_ready == len(GPU_TYPES):
            break
        time.sleep(0.5)

    if agents_ready < len(GPU_TYPES):
        print(f"[-] Only {agents_ready}/{len(GPU_TYPES)} agents connected!")
        print("    Did you run 'docker compose -f docker-compose.pc2.yml up -d' on PC 2?")
        print("    Is the Cat 8 cable connected and firewall open?")
        sys.exit(1)

    # 4. Verify Slot Counts
    slot_errors = 0
    for g in GPU_TYPES:
        slots = int(r.get(f"free:{g}") or 0)
        if slots != N_PER_TYPE:
            print(f"[-] Agent {g} reported {slots} slots, but expected {N_PER_TYPE}!")
            slot_errors += 1
        else:
            print(f"[+] Agent {g} registered {slots} slots.")
    if slot_errors > 0:
        print("    PC 2 might be using an old .env or didn't rebuild properly.")
        sys.exit(1)

    # 5. Clock (V09). Agents now stamp times on the Redis clock, so an RPC
    #    round-trip no longer reveals skew — it would always read ~0. Instead,
    #    each agent records the offset it CORRECTED for when it synced.
    print("\n[*] Measuring Cat 8 RPC latency and reading agent clock offsets...")
    t_start = time.time()
    ack = rpc(r, "T4", "ping", {}, timeout=5.0)
    t_end = time.time()
    if not ack.get("ok"):
        print("[-] Agent RPC ping failed or timed out!")
        sys.exit(1)
    print(f"[+] Cross-machine RPC RTT: {(t_end - t_start) * 1000:.2f} ms")

    import json as _json
    clock = {k: _json.loads(v) for k, v in (r.hgetall("clock") or {}).items()}
    agents = {k: v for k, v in clock.items() if not k.startswith("PC1")}
    if not agents:
        print("[-] No agent clock records found. Are PC 2's agents running V09?")
        print("    Re-sync the code and rebuild PC 2 (see the run guide).")
        sys.exit(1)
    for k, v in sorted(clock.items()):
        print(f"    {k:28} offset {v['offset_ms']:+10.1f} ms   rtt {v['rtt_ms']:.2f} ms")
    worst = max(abs(v["offset_ms"]) for v in clock.values())
    print(f"[+] V09 corrects these offsets automatically. Largest: {worst:.1f} ms")
    print("    RECORD THIS NUMBER: it is how far off V08's two-machine timestamps were.")

    hosts = sorted(r.smembers("agent_hosts") or [])
    codes = {h.rsplit(":", 1)[-1] for h in hosts}
    from common import code_fingerprint
    mine = code_fingerprint()
    if codes == {mine}:
        print(f"[+] PC 2 agents run the same code as PC 1 ({mine})")
    else:
        print(f"[-] CODE MISMATCH: PC 1 = {mine}, agents = {codes}")
        print("    Run sync_to_pc2.ps1, then rebuild PC 2 with --no-cache.")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED. You are cleared to run the dynamic suite.")
    print("=" * 60)

if __name__ == "__main__":
    main()

