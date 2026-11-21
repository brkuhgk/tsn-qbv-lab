"""Event-driven simulator of IEEE 802.1Qbv time-aware shaping, driven by a TSNKit schedule.

Why not TSNKit's own simulator (``tsnkit.simulation.tas``)? It releases every frame exactly
at its scheduled offset and has no way to perturb talker send times, and it advances time in
100 ns steps. This simulator keeps the same gate semantics, so with zero error it reproduces
TSNKit's simulator frame for frame (checked in ``tests/test_sim_vs_tsnkit.py``), and adds
talkers whose transmit time is off by an error.

Model
-----
* Every directed link has an egress **port**. A switch port has per-queue FIFOs and follows
  its gate control list (GCL), which repeats every ``cycle`` ns.
* **Gate semantics (same as TSNKit's simulator):** at time ``t`` the active GCL entry is the
  one with the latest ``start <= t`` within the cycle (none before the first entry or after
  the last entry's ``end``). Only that entry's queue is open. The head-of-line frame of the
  open queue may start transmitting at ``t`` only if the port is idle and the frame fits in
  the rest of the window (``end - t >= tx_time``), i.e. there is no overrun.
* A frame that finishes transmitting on link ``(u, v)`` at ``t`` can be sent on the next link
  ``(v, w)`` from ``t + t_prop(u, v) + t_proc(v, w)``. At the listener it is delivered at
  ``t + t_prop``.
* **Talker modes:**
    - ``talker_gated=True``: the talker's NIC is itself a Qbv port that follows its GCL entries.
      This is what every TSNKit scheduler assumes (they all emit GCL rows for the talkers'
      egress links), and is used as the *baseline*.
    - ``talker_gated=False``: a **standard NIC** with no gate. Each frame is handed to the
      NIC at ``send_time[(flow, k)]``, normally the baseline's intended transmit time plus an
      error, and sent first-in, first-out as soon as the wire is free.

Latency definition (same as ``smt_wa``'s deadline constraint): from the start of transmission
on the first link (the *intended* start when errors are injected) to the end of reception
at the listener.
"""

from __future__ import annotations

import heapq
from bisect import bisect_right
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .model import LinkId, Problem, Schedule

_RELEASE, _ARRIVE, _TRY = 0, 1, 2  # same-time ordering: releases, arrivals, then gate checks

FrameKey = Tuple[int, int]  # (flow id, instance number k)


@dataclass
class FrameRecord:
    flow: int
    instance: int
    handed_to_nic: int  # time the frame entered the talker's egress port
    hop_start: List[Optional[int]]  # transmission start per hop (hop 0 = talker NIC)
    delivered: Optional[int] = None  # end of reception at the listener


@dataclass
class SimResult:
    records: Dict[FrameKey, FrameRecord]
    hyperperiods: int
    talker_gated: bool

    def first_tx(self) -> Dict[FrameKey, int]:
        return {k: r.hop_start[0] for k, r in self.records.items() if r.hop_start[0] is not None}


class _Port:
    __slots__ = ("link", "gated", "windows", "starts", "cycle", "queues", "fifo",
                 "free_at", "token")

    def __init__(self, link, gated, windows, cycle):
        self.link = link
        self.gated = gated
        self.windows = windows
        self.starts = [w.start for w in windows]
        self.cycle = cycle
        self.queues: Dict[int, deque] = {}
        self.fifo: deque = deque()
        self.free_at = -(1 << 62)
        self.token = 0


class _Frame:
    __slots__ = ("flow", "rec", "hop", "queue")

    def __init__(self, flow, rec):
        self.flow = flow
        self.rec = rec
        self.hop = 0
        self.queue = 0


class Simulator:
    def __init__(self, problem: Problem, schedule: Schedule):
        self.p = problem
        self.s = schedule
        self.flows = {f.id: f for f in problem.flows}

    # ------------------------------------------------------------------ gate logic
    def window_at(self, link: LinkId, t: int) -> Optional[Tuple[int, int, int]]:
        """Absolute (start, end, queue) of the GCL entry active at time ``t``, or None."""
        wins = self.s.gcl.get(link, [])
        if not wins:
            return None
        c = self.s.cycle[link]
        k, tm = divmod(t, c)
        i = bisect_right([w.start for w in wins], tm) - 1
        if i < 0 or (i == len(wins) - 1 and tm >= wins[-1].end):
            return None
        w = wins[i]
        return k * c + w.start, k * c + w.end, w.queue

    def _next_gated_start(self, port: _Port, t0: int) -> Optional[Tuple[int, int]]:
        """Earliest time >= t0 at which some head-of-line frame can start, and its queue."""
        wins, n, c = port.windows, len(port.windows), port.cycle
        if n == 0:
            return None
        k, tm = divmod(t0, c)
        i = max(bisect_right(port.starts, tm) - 1, 0)
        for step in range(2 * n + 1):  # two full cycles are enough: the GCL is periodic
            idx, kk = (i + step) % n, k + (i + step) // n
            w = wins[idx]
            q = port.queues.get(w.queue)
            if not q:
                continue
            tx = self.p.links[port.link].tx_time(self.flows[q[0].flow].size)
            base = kk * c
            active_until = base + (wins[idx + 1].start if idx + 1 < n else c)  # exclusive
            lo = max(base + w.start, t0)
            hi = min(base + w.end - tx, active_until - 1)
            if lo <= hi:
                return lo, w.queue
        return None

    # ------------------------------------------------------------------ main loop
    def run(
        self,
        hyperperiods: int = 3,
        *,
        talker_gated: bool = True,
        send_time: Optional[Dict[FrameKey, int]] = None,
        drain_hyperperiods: int = 3,
    ) -> SimResult:
        if not talker_gated and send_time is None:
            raise ValueError("send_time is required when talker_gated=False")
        p, s = self.p, self.s
        hp = p.hyperperiod
        t_stop = (hyperperiods + drain_hyperperiods) * hp
        talkers = {f.src for f in p.flows}

        ports: Dict[LinkId, _Port] = {}

        def port_of(link: LinkId) -> _Port:
            if link not in ports:
                gated = talker_gated or link[0] not in talkers
                ports[link] = _Port(link, gated, s.gcl.get(link, []), s.cycle.get(link, hp))
            return ports[link]

        heap: list = []
        seq = 0

        def push(t, kind, a, b=None):
            nonlocal seq
            heapq.heappush(heap, (t, kind, seq, a, b))
            seq += 1

        def wake(port: _Port, t: int):
            port.token += 1
            push(max(t, port.free_at), _TRY, port, port.token)

        def enqueue(port: _Port, fr: _Frame, t: int):
            if port.gated:
                fr.queue = s.queue_of(fr.flow, fr.rec.instance, port.link)
                port.queues.setdefault(fr.queue, deque()).append(fr)
            else:
                port.fifo.append(fr)
            wake(port, t)

        def transmit(port: _Port, fr: _Frame, t: int):
            link = p.links[port.link]
            tx = link.tx_time(self.flows[fr.flow].size)
            port.free_at = t + tx
            fr.rec.hop_start[fr.hop] = t
            route = s.route[fr.flow]
            if fr.hop == len(route) - 1:
                fr.rec.delivered = t + tx + link.t_prop
            else:
                nxt = route[fr.hop + 1]
                fr.hop += 1
                push(t + tx + link.t_prop + p.links[nxt].t_proc, _ARRIVE, fr, nxt)
            wake(port, port.free_at)

        records: Dict[FrameKey, FrameRecord] = {}
        for f in p.flows:
            for k in range(hyperperiods * hp // f.period):
                if talker_gated:
                    t = k * f.period + s.release_offset(f.id, k)
                else:
                    t = send_time[(f.id, k)]
                rec = FrameRecord(f.id, k, t, [None] * len(s.route[f.id]))
                records[(f.id, k)] = rec
                push(t, _RELEASE, _Frame(f.id, rec), s.route[f.id][0])

        while heap:
            t, kind, _, a, b = heapq.heappop(heap)
            if t > t_stop:
                break
            if kind == _RELEASE or kind == _ARRIVE:
                enqueue(port_of(b), a, t)
                continue
            port, token = a, b  # _TRY
            if token != port.token or t < port.free_at:
                continue
            if port.gated:
                nxt = self._next_gated_start(port, t)
                if nxt is None:
                    continue  # nothing can be sent until a new frame arrives
                t_start, q = nxt
                if t_start > t:
                    push(t_start, _TRY, port, token)
                else:
                    transmit(port, port.queues[q].popleft(), t)
            elif port.fifo:
                transmit(port, port.fifo.popleft(), t)

        return SimResult(records, hyperperiods, talker_gated)


# ---------------------------------------------------------------------- analysis helpers
def e2e_latency(rec: FrameRecord, reference_start: int) -> Optional[int]:
    return None if rec.delivered is None else rec.delivered - reference_start


def verify(problem: Problem, schedule: Schedule, base: SimResult) -> dict:
    """Check a zero-error run: every frame delivered, within its deadline, and every switch
    hop transmitted inside a gate window of the queue assigned to that frame."""
    sim = Simulator(problem, schedule)
    flows = {f.id: f for f in problem.flows}
    lat: List[int] = []
    problems: List[str] = []
    for (fid, k), rec in base.records.items():
        if rec.delivered is None:
            problems.append(f"flow {fid} #{k} not delivered")
            continue
        e2e = rec.delivered - rec.hop_start[0]
        lat.append(e2e)
        if e2e > flows[fid].deadline:
            problems.append(f"flow {fid} #{k} latency {e2e} > deadline {flows[fid].deadline}")
        for h, link in enumerate(schedule.route[fid]):
            w = sim.window_at(link, rec.hop_start[h])
            if w is None or w[2] != schedule.queue_of(fid, k, link):
                problems.append(f"flow {fid} #{k} hop {link} sent outside its queue's window")
    return {
        "ok": not problems,
        "n_frames": len(base.records),
        "worst_e2e_ns": max(lat) if lat else None,
        "mean_e2e_ns": sum(lat) / len(lat) if lat else None,
        "problems": problems[:20],
    }


@dataclass
class ErrorMetrics:
    frames: int
    switch_hops: int
    window_misses: int
    first_switch_misses: int
    downstream_hops: int
    downstream_misses: int
    frames_with_miss: int
    lost: int
    deadline_misses: int
    flows: int = 0
    flows_with_miss: int = 0
    extra_latency_ns: List[int] = field(repr=False, default_factory=list)

    @property
    def miss_rate(self) -> float:
        return self.window_misses / self.switch_hops if self.switch_hops else 0.0

    @property
    def first_switch_miss_rate(self) -> float:
        return self.first_switch_misses / self.frames if self.frames else 0.0

    @property
    def downstream_miss_rate(self) -> float:
        return self.downstream_misses / self.downstream_hops if self.downstream_hops else 0.0


def compare(problem: Problem, schedule: Schedule, base: SimResult, run: SimResult) -> ErrorMetrics:
    """Compare a perturbed run against the zero-error baseline, frame by frame.

    A frame *misses its gate window* at a switch hop if it does not start transmission inside
    the very window instance the schedule gave it (the window in which the baseline sent it).
    Starting later (it arrived too late to fit) and starting earlier in another frame's window
    (queue order was disturbed by an early frame) both count as misses. The talker's own NIC
    (hop 0) is not counted: with a standard NIC it has no gate.
    """
    sim = Simulator(problem, schedule)
    flows = {f.id: f for f in problem.flows}
    m = ErrorMetrics(0, 0, 0, 0, 0, 0, 0, 0, 0)
    affected = set()
    for key, b in base.records.items():
        r = run.records[key]
        fid = key[0]
        route = schedule.route[fid]
        m.frames += 1
        missed_any = False
        for h in range(1, len(route)):
            ws, we, _ = sim.window_at(route[h], b.hop_start[h])
            tx = problem.links[route[h]].tx_time(flows[fid].size)
            a = r.hop_start[h]
            hit = a is not None and ws <= a and a + tx <= we
            m.switch_hops += 1
            if h >= 2:
                m.downstream_hops += 1
            if not hit:
                m.window_misses += 1
                missed_any = True
                if h == 1:
                    m.first_switch_misses += 1
                else:
                    m.downstream_misses += 1
        m.frames_with_miss += missed_any
        if missed_any:
            affected.add(fid)
        if r.delivered is None:
            m.lost += 1
            m.deadline_misses += 1
            continue
        m.extra_latency_ns.append(r.delivered - b.delivered)
        if r.delivered - b.hop_start[0] > flows[fid].deadline:
            m.deadline_misses += 1
    m.flows = len({k[0] for k in base.records})
    m.flows_with_miss = len(affected)
    return m
