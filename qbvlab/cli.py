"""Command line: ``qbvlab {schedule,gcl,benchmark,jitter}``."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path


def _cmd_schedule(a):
    from .runner import run_scheduler

    r = run_scheduler(a.method, a.task, a.topo, a.out, timeout_s=a.timeout, guard_ns=a.guard_ns)
    print(f"{a.method}: {r.status}  wall={r.wall_s:.3f}s  solve_cpu={r.solve_cpu_s}s")
    if r.ok:
        print(f"schedule files: {r.schedule_prefix}-{{GCL,OFFSET,QUEUE,ROUTE}}.csv")


def _cmd_gcl(a):
    from .model import Problem, Schedule
    from .visualize import export_gate_lists, plot_gcl

    p = Problem.load(Path(a.task), Path(a.topo))
    s = Schedule.load(Path(a.schedule), p)
    ports = [tuple(ast.literal_eval(x)) for x in a.ports] if a.ports else None
    png = plot_gcl(p, s, Path(a.out), ports=ports, include_talkers=not a.no_talkers,
                   t_range_us=tuple(a.range_us) if a.range_us else None,
                   highlight_flow=a.highlight_flow)
    print(png)
    if a.export:
        for f in export_gate_lists(p, s, Path(a.export)):
            print(f)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="qbvlab", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("schedule", help="run one TSNKit scheduler with a time limit")
    s.add_argument("method", help="TSNKit algorithm module, e.g. smt_wa, ls, dt")
    s.add_argument("task"), s.add_argument("topo")
    s.add_argument("--out", default="out")
    s.add_argument("--timeout", type=float, default=60)
    s.add_argument("--guard-ns", type=int, default=0, help="E_SYNC guard band (multiple of 100)")
    s.set_defaults(fn=_cmd_schedule)

    g = sub.add_parser("gcl", help="draw per-port gate timetables (Step 2)")
    g.add_argument("task"), g.add_argument("topo")
    g.add_argument("schedule", help="schedule prefix, e.g. out/smt_wa for out/smt_wa-GCL.csv")
    g.add_argument("--out", default="gcl.png")
    g.add_argument("--ports", nargs="*", help='links to draw, e.g. "(0, 4)" "(1, 0)"')
    g.add_argument("--no-talkers", action="store_true", help="hide talker NIC ports")
    g.add_argument("--range-us", nargs=2, type=float, metavar=("FROM", "TO"))
    g.add_argument("--highlight-flow", type=int)
    g.add_argument("--export", metavar="DIR", help="also write gate-control-list CSV + taprio templates")
    g.set_defaults(fn=_cmd_gcl)

    b = sub.add_parser("benchmark", help="scheduler benchmark (Step 3)")
    b.add_argument("rest", nargs=argparse.REMAINDER)
    b.set_defaults(fn=lambda a: __import__("qbvlab.experiments.benchmark", fromlist=["main"]).main(a.rest))

    j = sub.add_parser("jitter", help="talker send-time error experiment (Step 4)")
    j.add_argument("rest", nargs=argparse.REMAINDER)
    j.set_defaults(fn=lambda a: __import__("qbvlab.experiments.jitter", fromlist=["main"]).main(a.rest))

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
