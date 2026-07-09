"""common.py — shared helpers for the Option A emulation."""
from __future__ import annotations
import json, time, uuid
import redis
from redis.exceptions import TimeoutError as RTimeout, ConnectionError as RConnErr
from config import REDIS_HOST, REDIS_PORT

def R() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

def now() -> float:
    return time.time()

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

# ---------------- RPC: the decentralised handshake, for real ----------------
# The brain/dispatcher never mutates a node's slots directly. It sends a request
# through Redis; the TARGET agent verifies its own hardware table and ACKs or
# NACKs. This is the component-7 handshake as an actual network round-trip.
def safe_brpop(r, key, timeout: float):
    """BRPOP that never raises on socket/read timeouts (returns None instead)."""
    try:
        return r.brpop(key, timeout=max(1, int(round(timeout))))
    except (RTimeout, RConnErr):
        return None

def rpc(r, gpu_type: str, op: str, payload: dict, timeout=10.0) -> dict:
    resp = f"resp:{uuid.uuid4().hex}"
    r.lpush(f"agent:{gpu_type}:req", json.dumps({"op": op, "resp": resp, **payload}))
    got = safe_brpop(r, resp, timeout)
    if got is None:
        return {"ok": False, "err": "timeout"}
    return json.loads(got[1])

def shutdown_set(r): r.set("shutdown", "1")
def shutting_down(r): return r.get("shutdown") == "1"
