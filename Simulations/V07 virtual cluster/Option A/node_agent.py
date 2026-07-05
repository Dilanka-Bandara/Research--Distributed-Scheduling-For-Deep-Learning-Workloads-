"""
node_agent.py — component 7 (Decentralised Zone), one PROCESS per GPU type.

Owns the hardware truth for its type: a slot table nobody else may mutate.
All placement goes through its RPC loop — that IS the decentralised handshake:
  reserve : verify free slots >= d, start a worker thread, ACK; else NACK
            (a NACK observed by the brain = a real handshake rejection)
  evict   : set the worker's preempt flag; worker checkpoints progress to Redis,
            frees its slots, job returns to the global queue (Mechanism E)

Workers are threads that SLEEP in ticks (no real training), advancing
progress at theta epochs per scaled round, consuming any stall (profiling /
migration state-transfer) first — same physics as scheduler_simulation_v2.
"""
from __future__ import annotations
import json, sys, threading, time
from common import R, now, emit, load_job, update_job, safe_brpop
from config import (TICK_REAL, real_to_rounds, rounds_to_real, scaled,
                    LAN_GBPS, N_PER_TYPE)

class Agent:
    def __init__(self, gpu_type: str, slots: int):
        self.g = gpu_type
        self.slots = slots
        self.free = slots
        self.lock = threading.Lock()
        self.preempt: dict[int, threading.Event] = {}
        self.r = R()
        self.r.set(f"free:{self.g}", self.free)

    # ---------------- worker thread ----------------
    def _worker(self, job: dict, stall_rounds: float):
        r = R()
        jid = job["id"]
        prog = job["progress"]
        stall = stall_rounds
        t0 = now()
        update_job(r, jid, status="running", gpu=self.g)
        if prog == 0 and not job.get("first_exec_ts"):
            update_job(r, jid, first_exec_ts=t0)
            emit(r, "first_exec", job=jid, gpu=self.g)
        theta = job["theta"][self.g]
        last = t0
        while True:
            time.sleep(TICK_REAL)
            t = now(); dt_rounds = real_to_rounds(t - last); last = t
            if stall > 0:                      # profiling / state-transfer stall
                use = min(stall, dt_rounds); stall -= use; dt_rounds -= use
            prog += theta * dt_rounds
            if self.preempt.get(jid, threading.Event()).is_set():
                # ---- Mechanism E eviction: checkpoint, free, requeue ----
                update_job(r, jid, progress=prog, status="queued", gpu=None)
                self._release(jid, job["d"])
                r.lpush("queue:global", jid)
                r.lpush("chan:queue_event", "1")
                emit(r, "preempted", job=jid, gpu=self.g, progress=prog)
                return
            if prog >= job["W"]:
                update_job(r, jid, progress=prog, status="done",
                           finish_ts=now(), gpu=None)
                self._release(jid, job["d"])
                r.incr("done_count")
                emit(r, "finished", job=jid, gpu=self.g)
                return
            update_job(r, jid, progress=prog)

    def _release(self, jid, d):
        with self.lock:
            self.free += d
            self.r.set(f"free:{self.g}", self.free)
            self.r.srem(f"running:{self.g}", jid)
            self.preempt.pop(jid, None)

    # ---------------- RPC loop (the handshake) ----------------
    def run(self):
        r = self.r
        emit(r, "agent_up", gpu=self.g, slots=self.slots)
        while r.get("shutdown") != "1":
            got = safe_brpop(r, f"agent:{self.g}:req", 1)
            if not got: continue
            req = json.loads(got[1])
            op, resp = req["op"], req["resp"]
            if op == "reserve":
                job = load_job(r, req["job"])
                migrate_from = req.get("migrate_from")
                with self.lock:
                    ok = job is not None and self.free >= job["d"] \
                         and job["status"] not in ("done", "running")
                    if ok:
                        self.free -= job["d"]
                        r.set(f"free:{self.g}", self.free)
                        r.sadd(f"running:{self.g}", job["id"])
                if ok:
                    stall = req.get("stall_rounds", 0.0)
                    if migrate_from and migrate_from != self.g and job["progress"] > 0:
                        # real migration cost: state transfer over the (scaled) LAN
                        stall += (job["state_gb"] / LAN_GBPS) / 300.0
                        emit(r, "migration", job=job["id"],
                             src=migrate_from, dst=self.g)
                    ev = threading.Event(); self.preempt[job["id"]] = ev
                    threading.Thread(target=self._worker,
                                     args=(job, stall), daemon=True).start()
                r.lpush(resp, json.dumps({"ok": bool(ok)}))
                if not ok:
                    emit(r, "handshake_reject", job=req["job"], gpu=self.g)
            elif op == "evict":
                jid = req["job"]
                ev = self.preempt.get(jid)
                if ev: ev.set()
                r.lpush(resp, json.dumps({"ok": ev is not None}))
            else:
                r.lpush(resp, json.dumps({"ok": False, "err": "bad op"}))

if __name__ == "__main__":
    g = sys.argv[1]
    slots = int(sys.argv[2]) if len(sys.argv) > 2 else N_PER_TYPE
    Agent(g, slots).run()
