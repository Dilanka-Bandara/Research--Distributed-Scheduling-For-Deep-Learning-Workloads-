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

    # 5. Measure Clock Skew via RPC
    print("\n[*] Measuring Cat 8 RPC Latency & Clock Skew...")
    # Send ping RPC to PC 2's T4 agent
    t_start = time.time()
    ack = rpc(r, "T4", "ping", {}, timeout=5.0)
    t_end = time.time()
    
    if not ack.get("ok"):
        print("[-] Agent RPC ping failed or timed out!")
        sys.exit(1)

    rpc_rtt_ms = (t_end - t_start) * 1000
    pc2_time = ack["time"]
    
    # Calculate skew: PC 2's time compared to the midpoint of our RPC request
    expected_pc2_time = t_start + ((t_end - t_start) / 2)
    skew_ms = (pc2_time - expected_pc2_time) * 1000

    print(f"[+] Cross-machine RPC RTT: {rpc_rtt_ms:.2f} ms")
    if abs(skew_ms) > 20:
        print(f"[-] WARNING: Clock Skew Detected: {skew_ms:+.1f} ms")
        print("    PC 2's clock is significantly out of sync with PC 1.")
        print("    This will corrupt Starvation metrics (which cross machine boundaries).")
        print("    Fix: Sync time on both Windows machines via 'w32tm /resync' before running.")
    else:
        print(f"[+] Clocks are well synchronised (Skew: {skew_ms:+.1f} ms)")

    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED. You are cleared to run the 36-experiment suite.")
    print("=" * 60)

if __name__ == "__main__":
    main()
