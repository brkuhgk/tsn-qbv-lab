"""Convert TSNKit's per-window GCL rows into an IEEE 802.1Qbv gate control list.

TSNKit writes one row per *gate-open window*: ``(link, queue, start, end, cycle)``.
802.1Qbv instead describes each egress port as a cyclic list of entries
``SetGateStates(gate_mask, time_interval)``: for ``time_interval`` ns, queue ``q``'s gate
is open iff bit ``q`` of ``gate_mask`` is set. The intervals of a port add up to its cycle
time. This module performs that conversion and emits Linux ``tc taprio`` ``sched-entry``
lines, so a schedule can be inspected in the form a real switch or NIC is configured with.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from .model import GateWindow, LinkId, Schedule


@dataclass(frozen=True)
class GateEntry:
    start: int  # offset inside the cycle (ns)
    interval: int  # duration (ns), always > 0
    mask: int  # bit q set  <=>  gate of queue q open

    def mask_str(self, num_queues: int = 8) -> str:
        """Gate states as a string, queue 7 first (e.g. '00000101' = queues 0 and 2 open)."""
        return format(self.mask, f"0{num_queues}b")


def overlaps(windows: Iterable[GateWindow]) -> List[tuple]:
    ws = sorted(windows, key=lambda w: w.start)
    return [(a, b) for a, b in zip(ws, ws[1:]) if a.end > b.start]


def to_gate_entries(windows: List[GateWindow], cycle: int, idle_mask: int = 0) -> List[GateEntry]:
    """Build the gate control list of one port.

    ``idle_mask`` is the gate state used when no scheduled window is open. TSNKit only
    schedules time-triggered traffic, so the default 0 closes every gate. On a real device
    you would usually open the best-effort queue there.
    """
    if cycle <= 0:
        raise ValueError("cycle must be positive")
    for w in windows:
        if not (0 <= w.start < w.end <= cycle):
            raise ValueError(f"window {w} outside cycle [0, {cycle}]")
    if overlaps(windows):
        warnings.warn("overlapping windows: the gate list opens all of them together")
    points = sorted({0, cycle} | {w.start for w in windows} | {w.end for w in windows})
    entries: List[GateEntry] = []
    for a, b in zip(points, points[1:]):
        mask = 0
        for w in windows:
            if w.start <= a and b <= w.end:
                mask |= 1 << w.queue
        mask = mask or idle_mask
        if entries and entries[-1].mask == mask:  # merge equal neighbours
            last = entries.pop()
            entries.append(GateEntry(last.start, last.interval + (b - a), mask))
        else:
            entries.append(GateEntry(a, b - a, mask))
    return entries


def schedule_gate_lists(schedule: Schedule, idle_mask: int = 0) -> Dict[LinkId, List[GateEntry]]:
    return {l: to_gate_entries(w, schedule.cycle[l], idle_mask) for l, w in schedule.gcl.items()}


def taprio_command(
    entries: List[GateEntry], iface: str = "eth0", num_tc: int = 8, base_time: Optional[int] = None
) -> str:
    """A ``tc taprio`` command template for one port (software mode, CLOCK_TAI).

    Assumes traffic class ``q`` maps 1:1 to TSNKit queue ``q`` and hardware queue ``q``.
    Real NICs expose fewer queues (e.g. 4 on an Intel i210), so the ``map``/``queues``
    arguments need adapting. This is a starting point, not a drop-in config.
    """
    lines = [
        f"# cycle = {sum(e.interval for e in entries)} ns, {len(entries)} entries",
        f"tc qdisc replace dev {iface} parent root handle 100 taprio \\",
        f"    num_tc {num_tc} map " + " ".join(str(min(i, num_tc - 1)) for i in range(16)) + " \\",
        "    queues " + " ".join(f"1@{i}" for i in range(num_tc)) + " \\",
        f"    base-time {base_time if base_time is not None else '$BASE_TIME'} \\",
    ]
    lines += [f"    sched-entry S {e.mask:02x} {e.interval} \\" for e in entries]
    lines.append("    clockid CLOCK_TAI")
    return "\n".join(lines)
