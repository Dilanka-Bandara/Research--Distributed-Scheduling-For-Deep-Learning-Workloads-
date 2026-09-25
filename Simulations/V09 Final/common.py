"""common.py — shared helpers for the emulation (V09).

V09 FIXES IN THIS FILE
----------------------
1. REDIS CLIENT THAT HONOURS ITS TIMEOUTS  (root cause of the 58 s brain freeze)
   redis-py 8.x wraps a timed-out socket read in automatic retries. A BRPOP
   with timeout=10 that finds nothing therefore blocks for ~58 s, not 10 s
   (measured). The Dockerfile installed `redis` unpinned, so every image got
   this behaviour. Clients are now built with an explicit socket timeout that
   is LONGER than any blocking command we issue, and with retries disabled, so
   BRPOP returns None on the server's schedule. Fractional timeouts are also
   honoured now, so the brain's 0.9 s interval is no longer rounded up to 1 s.

2. ONE CLUSTER CLOCK  (cross-host timestamps)
   V08's now() was time.time() — the local clock of whichever host called it.
   On the two-PC cluster, arrival_ts is stamped on PC 1 but first_exec_ts and
   finish_ts are stamped on PC 2, so every TTFE, JCT and starvation value
   carried the PC1/PC2 clock offset. At TIME_SCALE=100 a 300 ms offset is 0.1
   rounds — larger than SMART's entire admission latency. Worse, Docker Desktop
   containers run inside the WSL2 VM, whose clock is separate from Windows and
   drifts (notably after sleep), so `w32tm /resync` on the host does not fix
   it. Every process now anchors to the Redis server's clock (TIME command,
   NTP-style minimum-RTT sample). All timestamps are therefore on ONE clock:
   Redis's, on PC 1.

3. RPC TARGET GUARD
   rpc() to a GPU type that no agent serves (V08 sent evicts to `None`) now
   fails immediately and loudly, instead of silently blocking.

The EMU_FAKE_SKEW environment variable offsets this process's LOCAL clock by
the given seconds. It exists only so you can prove, on one machine, that
injected skew no longer changes any metric. Never set it for real runs.
"""
from __future__ import annotations

import json
import os
import socket
import time
import uuid

import redis
from redis.backoff import NoBackoff
from redis.exceptions import ConnectionError as RConnErr
from redis.exceptions import TimeoutError as RTimeout
from redis.retry import Retry

from config import REDIS_HOST, REDIS_PORT
from workload import GPU_TYPES

# Must exceed the longest blocking command issued anywhere (rpc = 10 s).
SOCKET_TIMEOUT = 30.0


def R() -> redis.Redis:
    return redis.Redis(
        host=REDIS_HOST, port=REDIS_PORT, decode_responses=True,
        socket_timeout=SOCKET_TIMEOUT, socket_connect_timeout=5.0,
        retry=Retry(NoBackoff(), 0),
    )


# ---------------- the cluster clock ----------------
_FAKE_SKEW = float(os.environ.get("EMU_FAKE_SKEW", "0") or 0)
_CLOCK = {"offset": 0.0, "rtt": None, "synced": False}


def node_label() -> str:
    """Human-readable identity for provenance (set EMU_NODE_LABEL=PC1/PC2)."""
    return os.environ.get("EMU_NODE_LABEL") or socket.gethostname()


def code_fingerprint() -> str:
    """md5 over every .py next to this file. PC 1 and PC 2 must match."""
    import glob, hashlib
    here = os.path.dirname(os.path.abspath(__file__))
    h = hashlib.md5()
    for f in sorted(glob.glob(os.path.join(here, "*.py"))):
        h.update(os.path.basename(f).encode()); h.update(open(f, "rb").read())
    return h.hexdigest()[:12]


def _local() -> float:
    return time.time() + _FAKE_SKEW


def sync_clock(r: redis.Redis, samples: int = 25) -> dict:
    """Anchor this process to the Redis server clock.

    Take the sample with the smallest round trip (least queueing noise) and
    assume the server read its clock at the midpoint. Error is bounded by
    RTT/2 — sub-millisecond on a direct Cat 8 link, i.e. ~0.0003 rounds.
    """
    best = None
    for _ in range(samples):
        t1 = _local()
        sec, usec = r.time()
        t2 = _local()
        rtt = t2 - t1
        off = (sec + usec / 1e6) - 0.5 * (t1 + t2)
        if best is None or rtt < best[0]:
            best = (rtt, off)
    _CLOCK.update(rtt=best[0], offset=best[1], synced=True)
    try:
        r.hset("clock", f"{node_label()}:{os.getpid()}", json.dumps({
            "offset_ms": round(best[1] * 1000, 3),
            "rtt_ms": round(best[0] * 1000, 3),
        }))
    except Exception:
        pass
    return dict(_CLOCK)


def now() -> float:
    """Cluster time (Redis server clock). Lazily syncs on first use."""
    if not _CLOCK["synced"]:
        try:
            sync_clock(R())
        except Exception:
            return _local()          # Redis not up yet; retry on next call
    return _local() + _CLOCK["offset"]


# ---------------- job records ----------------
def job_key(jid): return f"jobs:{jid}"


def save_job(r, j: dict):
    r.hset(job_key(j["id"]), mapping={k: json.dumps(v) for k, v in j.items()})


def load_job(r, jid) -> dict | None:
    h = r.hgetall(job_key(jid))
    if not h: return None
    return {k: json.loads(v) for k, v in h.items()}


def update_job(r, jid, **fields):
    r.hset(job_key(jid), mapping={k: json.dumps(v) for k, v in fields.items()})


# ---------------- telemetry ----------------
def emit(r, etype: str, **kw):
    kw.update(type=etype, ts=now())
    r.rpush("metrics:events", json.dumps(kw))


# ---------------- RPC: the decentralised handshake ----------------
def safe_brpop(r, key, timeout: float):
    """BRPOP that returns None on timeout. Fractional seconds are honoured."""
    try:
        return r.brpop(key, timeout=max(0.05, float(timeout)))
    except (RTimeout, RConnErr):
        return None


def rpc(r, gpu_type: str, op: str, payload: dict, timeout=10.0) -> dict:
    if gpu_type not in GPU_TYPES:
        # V08 sent evicts to `None` from a stale snapshot; nobody listens on
        # agent:None:req, so the caller froze. Fail fast and leave evidence.
        try:
            emit(r, "rpc_bad_target", target=repr(gpu_type), op=op,
                 job=payload.get("job"))
        except Exception:
            pass
        return {"ok": False, "err": f"no agent serves {gpu_type!r}"}
    resp = f"resp:{uuid.uuid4().hex}"
    r.lpush(f"agent:{gpu_type}:req", json.dumps({"op": op, "resp": resp, **payload}))
    got = safe_brpop(r, resp, timeout)
    if got is None:
        try:
            emit(r, "rpc_timeout", target=gpu_type, op=op, job=payload.get("job"))
        except Exception:
            pass
        return {"ok": False, "err": "timeout"}
    return json.loads(got[1])


def shutdown_set(r): r.set("shutdown", "1")
def shutting_down(r): return r.get("shutdown") == "1"
