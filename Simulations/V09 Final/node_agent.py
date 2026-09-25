"""
node_agent.py — component 7 (Decentralised Zone), one PROCESS per GPU type.

Owns the hardware truth for its type: a slot table nobody else may mutate.
All placement goes through its RPC loop — that IS the decentralised handshake:
  reserve : verify free slots >= d, start a worker thread, ACK; else NACK
  evict   : preempt a worker; it checkpoints progress, frees its slots and the
            job returns to the global queue

Workers are threads that SLEEP in ticks (no real training), advancing progress
at theta epochs per scaled round, consuming any stall (profiling / migration
state-transfer) first.

This agent is SHARED by both schedulers, so every fix below applies equally to
FFT and SMART. None of them favours either side.

V09 FIXES
---------
1. SYNCHRONOUS EVICT. V08 acknowledged an evict the moment it set the worker's
   flag, and callers then slept 0.15 s hoping the worker had checkpointed. The
   worker only notices on its next 50 ms tick and then does several Redis round
   trips, so across two machines the checkpoint could land AFTER the caller had
   already re-reserved the job elsewhere. The old worker's late write of
   status="queued", gpu=None then overwrote the new worker's status="running",
   and the job was simultaneously running and apparently queued — eligible to be
   reserved a second time. Evict now acknowledges only after the worker has
   checkpointed, released its slots and requeued. No caller sleeps any more.

2. ATOMIC EVICT-AND-RESERVE. A reserve request may carry `evict: [ids]`. The
   agent evicts those victims synchronously and then reserves, inside one
   request. Because the RPC loop is single-threaded, nothing — in particular
   not the fast-path dispatcher — can take the freed slots in between. In V08
   the brain evicted, slept, then reserved, and a fast-path arrival landing in
   that window stole the slots: the victims lost their progress for nothing.
   This keeps the node agent as the single authority over its own hardware,
   which is the decentralised design stated in the architecture.

3. RESERVATION IS COMMITTED BEFORE THE ACK. The job record is marked
   running on this GPU before the ACK is sent, closing the window in which a
   second reserve for the same job could pass the status check.

4. RUN-EPOCH GUARD. PC 2's agents persist across runs. In V08 a worker still
   alive when a run ended (e.g. after a timeout) kept running into the NEXT
   run: it inflated `free` beyond the slot count and incremented the new run's
   done_count, which could end that run early. Workers now carry the epoch they
   were started in and exit silently, touching nothing, once it changes.

5. first_progress_ts. first_exec_ts is stamped at reservation, BEFORE any
   profiling or migration stall. first_progress_ts marks the first moment of
   useful work, so profiling cost is visible in the metrics instead of hidden.

6. CLOCK. Re-anchors to the Redis clock at the start of every run (the WSL2
   VM clock can drift between runs) and registers as <label>:<gpu>.
"""
from __future__ import annotations

import json
import sys
import threading
import time

from common import (R, code_fingerprint, emit, load_job, node_label,
                    now, safe_brpop, sync_clock, update_job)
from config import (LAN_GBPS, N_PER_TYPE, REDIS_HOST, REDIS_PORT, TICK_REAL,
                    real_to_rounds)

EVICT_WAIT_REAL = 3.0   # max real seconds to wait for a worker to checkpoint


class Agent:
    def __init__(self, gpu_type: str, slots: int):
        self.g = gpu_type
        self.slots = slots
        self.free = slots
        self.lock = threading.Lock()
        self.preempt: dict[int, threading.Event] = {}
        self.released: dict[int, threading.Event] = {}
        self.epoch = 0
        self.r = None

    # ---------------- worker thread ----------------
    def _worker(self, job: dict, stall_rounds: float, epoch: int):
        r = R()
        jid = job["id"]
        prog = job["progress"]
        stall = stall_rounds
        progressed = bool(job.get("first_progress_ts"))
        last = now()
        theta = job["theta"][self.g]
        while True:
            time.sleep(TICK_REAL)
            if epoch != self.epoch:          # run ended: vanish, touch nothing
                return
            t = now(); dt_rounds = real_to_rounds(t - last); last = t
            if stall > 0:                    # profiling / state-transfer stall
                use = min(stall, dt_rounds); stall -= use; dt_rounds -= use
            if dt_rounds > 0:
                prog += theta * dt_rounds
                if not progressed:
                    progressed = True
                    update_job(r, jid, first_progress_ts=t)
            if self.preempt.get(jid, threading.Event()).is_set():
                # checkpoint, free, requeue — THEN signal the waiting evict RPC
                update_job(r, jid, progress=prog, status="queued", gpu=None)
                self._release(jid, job["d"])
                r.lpush("queue:global", jid)
                r.lpush("chan:queue_event", "1")
                emit(r, "preempted", job=jid, gpu=self.g, progress=prog)
                self._signal_released(jid)
                return
            if prog >= job["W"]:
                update_job(r, jid, progress=prog, status="done",
                           finish_ts=now(), gpu=None)
                self._release(jid, job["d"])
                r.incr("done_count")
                emit(r, "finished", job=jid, gpu=self.g)
                self._signal_released(jid)
                return
            update_job(r, jid, progress=prog)

    def _release(self, jid, d):
        with self.lock:
            self.free += d
            if self.r:
                self.r.set(f"free:{self.g}", self.free)
                self.r.srem(f"running:{self.g}", jid)
                self.r.srem("running:fast", jid)
            self.preempt.pop(jid, None)

    def _signal_released(self, jid):
        ev = self.released.pop(jid, None)
        if ev:
            ev.set()

    # ---------------- synchronous eviction ----------------
    def _evict_sync(self, jid) -> bool:
        """Preempt a worker and wait until it has checkpointed and released."""
        ev = self.preempt.get(jid)
        if ev is None:
            return False
        done = self.released.setdefault(jid, threading.Event())
        ev.set()
        return done.wait(EVICT_WAIT_REAL)

    # ---------------- RPC handlers ----------------
    def _reserve(self, r, req) -> bool:
        # Optional victims first: evicted and fully released before we look at
        # capacity, inside this one request, so nothing can steal the slots.
        for vid in req.get("evict") or []:
            self._evict_sync(int(vid))

        job = load_job(r, req["job"])
        migrate_from = req.get("migrate_from")
        with self.lock:
            ok = (job is not None and self.free >= job["d"]
                  and job["status"] not in ("done", "running"))
            if ok:
                self.free -= job["d"]
                r.set(f"free:{self.g}", self.free)
                r.sadd(f"running:{self.g}", job["id"])
                if req.get("route") == "fast":
                    r.sadd("running:fast", job["id"])
                # committed before the ACK: a second reserve now fails
                update_job(r, job["id"], status="running", gpu=self.g)
        if not ok:
            return False

        stall = req.get("stall_rounds", 0.0) or 0.0
        if migrate_from and migrate_from != self.g and job["progress"] > 0:
            stall += (job["state_gb"] / LAN_GBPS) / 300.0
            emit(r, "migration", job=job["id"], src=migrate_from, dst=self.g)
        if not job.get("first_exec_ts"):
            update_job(r, job["id"], first_exec_ts=now())
            emit(r, "first_exec", job=job["id"], gpu=self.g)
        self.preempt[job["id"]] = threading.Event()
        threading.Thread(target=self._worker,
                         args=(job, stall, self.epoch), daemon=True).start()
        return True

    # ---------------- RPC loop ----------------
    def run(self):
        print(f"[{self.g}] Agent started with {self.slots} slots. "
              f"Target Redis: {REDIS_HOST}:{REDIS_PORT}", flush=True)
        while True:
            attempts = 0
            while True:
                try:
                    r = R()
                    r.ping()
                    if r.get("shutdown") != "1":
                        self.r = r
                        break
                except Exception:
                    pass
                if attempts % 5 == 0:
                    print(f"[{self.g}] Waiting for Redis at "
                          f"{REDIS_HOST}:{REDIS_PORT}...", flush=True)
                attempts += 1
                time.sleep(1)

            # fresh run: new epoch, clean slot table, re-anchored clock
            with self.lock:
                self.epoch += 1
                self.free = self.slots
                self.preempt.clear()
                self.released.clear()

            try:
                r = self.r
                clk = sync_clock(r)
                r.set(f"free:{self.g}", self.free)
                emit(r, "agent_up", gpu=self.g, slots=self.slots,
                     node=node_label(), clock_offset_ms=clk["offset"] * 1000,
                     clock_rtt_ms=clk["rtt"] * 1000)
                r.sadd("agent_hosts", f"{node_label()}:{self.g}:{code_fingerprint()}")
                print(f"[{self.g}] Ready (epoch {self.epoch}). {self.free} slots; "
                      f"clock offset {clk['offset']*1000:+.1f} ms "
                      f"(rtt {clk['rtt']*1000:.2f} ms)", flush=True)

                while True:
                    if r.get("shutdown") == "1":
                        # End the epoch FIRST: every worker exits on its next
                        # tick without writing anything.
                        with self.lock:
                            self.epoch += 1
                            self.preempt.clear()
                            self.released.clear()
                        drained = 0
                        while safe_brpop(r, f"agent:{self.g}:req", 0.1):
                            drained += 1
                        print(f"[{self.g}] Run over. Workers retired; drained "
                              f"{drained} stale request(s).", flush=True)
                        time.sleep(1.5)
                        break

                    got = safe_brpop(r, f"agent:{self.g}:req", 1)
                    if not got:
                        continue
                    req = json.loads(got[1])
                    op, resp = req["op"], req["resp"]
                    if op == "reserve":
                        ok = self._reserve(r, req)
                        r.lpush(resp, json.dumps({"ok": bool(ok)}))
                        if not ok:
                            emit(r, "handshake_reject", job=req["job"], gpu=self.g)
                    elif op == "evict":
                        ok = self._evict_sync(int(req["job"]))
                        r.lpush(resp, json.dumps({"ok": bool(ok)}))
                    elif op == "ping":
                        r.lpush(resp, json.dumps({"ok": True, "time": now()}))
                    else:
                        r.lpush(resp, json.dumps({"ok": False, "err": "bad op"}))
            except Exception as e:
                print(f"[{self.g}] Redis disconnected ({e}). "
                      f"Waiting for next stack...", flush=True)
                with self.lock:
                    self.epoch += 1          # retire any orphaned workers
                time.sleep(1)


if __name__ == "__main__":
    g = sys.argv[1]
    slots = int(sys.argv[2]) if len(sys.argv) > 2 else N_PER_TYPE
    Agent(g, slots).run()
