"""Draw per-port IEEE 802.1Qbv gate timetables from a TSNKit schedule."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import style
from .gcl import schedule_gate_lists, taprio_command
from .model import LinkId, Problem, Schedule
from .sim import Simulator

style.apply()
plt = style.plt


def node_name(n: int, end_stations: set) -> str:
    return f"ES{n}" if n in end_stations else f"SW{n}"


def port_name(link: LinkId, end_stations: set) -> str:
    return f"{node_name(link[0], end_stations)} → {node_name(link[1], end_stations)}"


def window_flows(problem: Problem, schedule: Schedule) -> Dict[Tuple[LinkId, int, int], List[int]]:
    """Which flows use each window ``(link, queue, start)`` in the first hyperperiod.

    Derived by simulating the schedule with zero error, so the labels show what actually
    happens on the wire, not just what the CSV says.
    """
    sim = Simulator(problem, schedule)
    base = sim.run(1)
    used: Dict[Tuple[LinkId, int, int], List[int]] = {}
    for (fid, _k), rec in base.records.items():
        for h, link in enumerate(schedule.route[fid]):
            if rec.hop_start[h] is None:
                continue
            w = sim.window_at(link, rec.hop_start[h])
            if w is not None:
                c = schedule.cycle[link]
                used.setdefault((link, w[2], w[0] % c), []).append(fid)
    return used


def plot_gcl(
    problem: Problem,
    schedule: Schedule,
    out_png: Path,
    *,
    ports: Optional[Sequence[LinkId]] = None,
    include_talkers: bool = True,
    t_range_us: Optional[Tuple[float, float]] = None,
    highlight_flow: Optional[int] = None,
    title: Optional[str] = None,
) -> Path:
    es = problem.end_stations
    if ports is None:
        ports = [l for l in schedule.gcl if include_talkers or l[0] not in es]
        # switches first (by switch id), then talker NICs; stable and readable
        ports = sorted(ports, key=lambda l: (l[0] in es, l[0], l[1]))
    used = window_flows(problem, schedule)

    if t_range_us is None:
        # Default view: the busy part of the first (shortest) period.
        p_min = min(f.period for f in problem.flows)
        wins = [w for l in ports for w in schedule.gcl[l] if w.start < p_min]
        lo, hi = min(w.start for w in wins), max(w.end for w in wins)
        pad = 0.03 * (hi - lo)
        t_range_us = (max(0.0, (lo - pad) / 1e3), (hi + pad) / 1e3)
    span_us = t_range_us[1] - t_range_us[0]

    fig_h = 1.3 + 0.36 * len(ports)
    fig, ax = plt.subplots(figsize=(11, fig_h))
    queues_seen = set()
    for row, link in enumerate(ports):
        y = len(ports) - 1 - row
        for w in schedule.gcl[link]:
            x0, x1 = w.start / 1e3, w.end / 1e3
            if x1 < t_range_us[0] or x0 > t_range_us[1]:
                continue
            flows = used.get((link, w.queue, w.start), [])
            hl = highlight_flow is not None and highlight_flow in flows
            ax.barh(
                y, x1 - x0, left=x0, height=0.62,
                color=style.CATEGORICAL[w.queue % 8],
                edgecolor=style.TEXT if hl else style.SURFACE,
                linewidth=1.8 if hl else 1.0,
                alpha=1.0 if flows else 0.35,  # faded = window no frame used
            )
            queues_seen.add(w.queue)
            label = ",".join(f"f{f}" for f in sorted(set(flows)))
            if label and (x1 - x0) / span_us > 0.012 * len(label) / 2:
                ax.text((x0 + x1) / 2, y, label, ha="center", va="center",
                        fontsize=7, color=style.TEXT)
    ax.set_yticks(range(len(ports)))
    ax.set_yticklabels([port_name(l, es) for l in reversed(ports)], fontsize=8)
    ax.set_xlim(*t_range_us)
    ax.set_xlabel("time within the gate cycle (µs)")
    ax.grid(axis="y", visible=False)
    cyc = sorted(set(schedule.cycle.values()))
    head = 0.62 / fig_h  # fraction of the figure height reserved for title + subtitle
    fig.text(0.01, 1 - 0.18 / fig_h, title or f"802.1Qbv gate-open windows per egress port — {schedule.name}",
             fontsize=11, fontweight="bold", color=style.TEXT, va="top")
    fig.text(0.01, 1 - 0.42 / fig_h,
             f"Gate cycle {', '.join(f'{c/1e3:g} µs' for c in cyc)}. Bar label = flow that used the "
             "window in a zero-error simulation; faded bar = window no frame used.",
             fontsize=8, color=style.TEXT_2, va="top")
    handles = [plt.Rectangle((0, 0), 1, 1, color=style.CATEGORICAL[q % 8]) for q in sorted(queues_seen)]
    ax.legend(handles, [f"queue {q}" for q in sorted(queues_seen)], title="gate open",
              loc="upper left", bbox_to_anchor=(1.005, 1.0), fontsize=8, title_fontsize=8)
    fig.tight_layout(rect=(0, 0, 1, 1 - head))
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png)
    plt.close(fig)
    return out_png


def export_gate_lists(problem: Problem, schedule: Schedule, out_dir: Path) -> List[Path]:
    """Write the 802.1Qbv gate control list of every port as CSV and as taprio commands."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    es = problem.end_stations
    rows = ["port,link,entry,start_ns,interval_ns,gate_mask_bin,gate_mask_hex"]
    tap = []
    for link, entries in sorted(schedule_gate_lists(schedule).items()):
        name = port_name(link, es)
        for i, e in enumerate(entries):
            rows.append(f"{name},\"{link}\",{i},{e.start},{e.interval},{e.mask_str()},{e.mask:02x}")
        tap.append(f"# ---- {name}  link {link}\n" + taprio_command(entries, iface=f"port{link[0]}_{link[1]}"))
    p1 = out_dir / f"{schedule.name}-gate-control-list.csv"
    p2 = out_dir / f"{schedule.name}-taprio.sh"
    p1.write_text("\n".join(rows) + "\n")
    p2.write_text("#!/bin/sh\n# Generated by qbvlab from a TSNKit schedule. Templates only: review before use.\n\n"
                  + "\n\n".join(tap) + "\n")
    return [p1, p2]
