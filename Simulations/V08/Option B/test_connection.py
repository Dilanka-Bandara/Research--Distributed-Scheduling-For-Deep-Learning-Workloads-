"""
test_connection.py — Verify Cat 8 connection and Redis RPC latency between PC 1 and PC 2.

Usage:
  python test_connection.py [host]
  Example: python test_connection.py 192.168.100.1
"""
import sys, time
import redis

host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
port = 6379

print(f"[*] Testing connection to Redis at {host}:{port}...")

try:
    r = redis.Redis(host=host, port=port, socket_connect_timeout=3, decode_responses=True)
    t0 = time.time()
    res = r.ping()
    dt = (time.time() - t0) * 1000
    print(f"[+] PING response: {res} (RTT: {dt:.2f} ms)")

    # Test roundtrip latency over 10 iterations
    latencies = []
    for i in range(10):
        t0 = time.time()
        r.set(f"test:ping:{i}", "1", ex=10)
        _ = r.get(f"test:ping:{i}")
        latencies.append((time.time() - t0) * 1000)
    
    avg_lat = sum(latencies) / len(latencies)
    min_lat = min(latencies)
    max_lat = max(latencies)
    print(f"[+] 10-packet Redis RPC test:")
    print(f"    - Avg Latency: {avg_lat:.3f} ms")
    print(f"    - Min Latency: {min_lat:.3f} ms")
    print(f"    - Max Latency: {max_lat:.3f} ms")
    print("[SUCCESS] Cat 8 direct link and Redis connection verified successfully!")

except Exception as e:
    print(f"[-] Connection failed: {e}")
    print("\nTroubleshooting tips:")
    print(" 1. Check if the Cat 8 cable is plugged in and Ethernet adapters show 'Connected'.")
    print(" 2. Verify static IPs (PC 1: 192.168.100.1, PC 2: 192.168.100.2).")
    print(" 3. Ensure Windows Firewall on PC 1 allows inbound TCP port 6379.")
    print(" 4. Ensure Redis is running on PC 1 (`docker compose -f docker-compose.pc1.yml --profile smart up -d`).")
