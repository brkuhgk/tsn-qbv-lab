"""Load TSNKit problem files (task/topo CSV) and schedule files (GCL/OFFSET/QUEUE/ROUTE CSV).

File formats (TSNKit docs, ``doc/source/dataprep.md``; units are ns and bytes):

* ``task.csv``  : stream, src, dst ("[n]"), size, period, deadline, jitter
* ``topo.csv``  : link ("(u, v)"), q_num, rate (bit/ns; 1 = 1 Gbps), t_proc, t_prop
* ``*-GCL.csv`` : link, queue, start, end, cycle   -- one row = one gate-open window
* ``*-OFFSET.csv``: stream, frame, offset          -- release offset inside the period
* ``*-QUEUE.csv`` : stream, frame, link, queue     -- queue used by a frame on a link
* ``*-ROUTE.csv`` : stream, link                   -- the links a stream traverses
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass, field
from functools import reduce
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd

LinkId = Tuple[int, int]


def _link(text: str) -> LinkId:
    u, v = ast.literal_eval(str(text))
    return int(u), int(v)


@dataclass(frozen=True)
class Link:
    src: int
    dst: int
    rate: float  # bit / ns
    t_proc: int  # ns, processing delay before the frame can leave the next egress port
    t_prop: int  # ns, propagation delay

    @property
    def id(self) -> LinkId:
        return (self.src, self.dst)

    def tx_time(self, size_bytes: int) -> int:
        return int(math.ceil(size_bytes * 8 / self.rate))


@dataclass(frozen=True)
class Flow:
    id: int
    src: int
    dst: int
    size: int  # bytes
    period: int  # ns
    deadline: int  # ns


@dataclass
class Problem:
    flows: List[Flow]
    links: Dict[LinkId, Link]

    @property
    def hyperperiod(self) -> int:
        return reduce(lambda a, b: a * b // math.gcd(a, b), (f.period for f in self.flows))

    @property
    def end_stations(self) -> set:
        """Nodes with exactly one outgoing link (TSNKit's own rule: ES have 2 links)."""
        out_deg: Dict[int, int] = {}
        for u, _ in self.links:
            out_deg[u] = out_deg.get(u, 0) + 1
        return {n for n, d in out_deg.items() if d == 1}

    @staticmethod
    def load(task_csv: Path, topo_csv: Path) -> "Problem":
        t = pd.read_csv(task_csv)
        flows = []
        for r in t.itertuples(index=False):
            dst = ast.literal_eval(str(r.dst))
            if len(dst) != 1:
                raise ValueError("only unicast streams are supported")
            flows.append(Flow(int(r.stream), int(r.src), int(dst[0]), int(r.size),
                              int(r.period), int(r.deadline)))
        n = pd.read_csv(topo_csv)
        links = {}
        for r in n.itertuples(index=False):
            u, v = _link(r.link)
            links[(u, v)] = Link(u, v, float(r.rate), int(r.t_proc), int(r.t_prop))
        return Problem(sorted(flows, key=lambda f: f.id), links)


@dataclass(frozen=True)
class GateWindow:
    """Gate of ``queue`` is open in [start, end) of every ``cycle`` (ns)."""

    queue: int
    start: int
    end: int


@dataclass
class Schedule:
    gcl: Dict[LinkId, List[GateWindow]]  # sorted by start (stable)
    cycle: Dict[LinkId, int]
    offset: Dict[Tuple[int, int], int]  # (stream, frame) -> offset in period
    n_offsets: Dict[int, int]  # stream -> number of distinct frame offsets
    queue: Dict[Tuple[int, int, LinkId], int]  # (stream, frame, link) -> queue
    route: Dict[int, List[LinkId]]  # stream -> ordered links, talker first
    name: str = ""
    meta: dict = field(default_factory=dict)

    def queue_of(self, stream: int, frame_idx: int, link: LinkId) -> int:
        key = (stream, frame_idx % self.n_offsets[stream], link)
        if key in self.queue:
            return self.queue[key]
        return self.queue[(stream, 0, link)]

    def release_offset(self, stream: int, instance: int) -> int:
        return self.offset[(stream, instance % self.n_offsets[stream])]

    @staticmethod
    def load(prefix: Path, problem: Problem) -> "Schedule":
        prefix = str(prefix)
        g = pd.read_csv(prefix + "-GCL.csv")
        gcl: Dict[LinkId, List[GateWindow]] = {}
        cycle: Dict[LinkId, int] = {}
        for r in g.itertuples(index=False):
            l = _link(r.link)
            gcl.setdefault(l, []).append(GateWindow(int(r.queue), int(r.start), int(r.end)))
            cycle.setdefault(l, int(r.cycle))
        for l in gcl:
            gcl[l].sort(key=lambda w: w.start)  # Python's sort is stable, like TSNKit's

        o = pd.read_csv(prefix + "-OFFSET.csv")
        offset = {(int(r.stream), int(r.frame)): int(r.offset) for r in o.itertuples(index=False)}
        n_offsets: Dict[int, int] = {}
        for s, _ in offset:
            n_offsets[s] = n_offsets.get(s, 0) + 1

        q = pd.read_csv(prefix + "-QUEUE.csv")
        queue = {(int(r.stream), int(r.frame), _link(r.link)): int(r.queue)
                 for r in q.itertuples(index=False)}

        rt = pd.read_csv(prefix + "-ROUTE.csv")
        nxt: Dict[int, Dict[int, int]] = {}
        for r in rt.itertuples(index=False):
            u, v = _link(r.link)
            nxt.setdefault(int(r.stream), {})[u] = v
        route = {}
        for f in problem.flows:
            hops, node = [], f.src
            while node != f.dst:
                v = nxt[f.id][node]
                hops.append((node, v))
                node = v
                if len(hops) > len(problem.links):
                    raise ValueError(f"route of stream {f.id} does not reach its listener")
            route[f.id] = hops
        return Schedule(gcl, cycle, offset, n_offsets, queue, route, name=Path(prefix).name)
