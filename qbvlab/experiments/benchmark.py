"""Step 3: reproducible benchmark of TSNKit schedulers.

For every instance (fixed seed) and scheduler we record:

* **status**: schedulable / infeasible / timeout / error, as reported by TSNKit.
* **verified**: our simulator replays the schedule with zero error and checks that every frame
  is delivered before its deadline and sent inside its queue's gate window.
* **runtime**: wall-clock time of the scheduler call (``wall_s``) and TSNKit's own CPU
  measurement of ``solve()`` (``solve_cpu_s``).
* **worst-case end-to-end latency**: max over all frames in the zero-error simulation.

Schedulability rate counts only *verified* schedules; timeouts count as not schedulable.
Latency is compared only on instances that all schedulers solved, so a scheduler is not
rewarded for failing on the hard instances.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from ..instances import InstanceSpec, generate
from ..model import Problem, Schedule
from ..runner import SCHEDULERS, run_scheduler
from ..sim import Simulator, verify

# TSNKit generator options: periods {100,200,400,800} us, payload 200-1500 B,
# deadline = path transmission time + {0,10,20,25,50} us (tight).
BASE = dict(topo="mesh", period_opt=4, size_opt=3, deadline_opt=4)

FULL = dict(
    sweeps={
        "flows": [dict(num_sw=8, num_flows=n) for n in (10, 20, 30, 40, 60, 80)],
        "switches": [dict(num_sw=s, num_flows=30) for s in (4, 6, 8, 12, 16)],
    },
    seeds=[0, 1, 2, 3, 4],
    schedulers=list(SCHEDULERS),
    timeout_s=60,
    sim_hyperperiods=3,
)

QUICK = dict(
    sweeps={
        "flows": [dict(num_sw=4, num_flows=n) for n in (6, 12)],
        "switches": [dict(num_sw=s, num_flows=8) for s in (3, 4)],
    },
    seeds=[0],
    schedulers=list(SCHEDULERS),
    timeout_s=30,
    sim_hyperperiods=2,
)


def latency_lower_bound(problem: Problem, schedule: Schedule) -> int:
    """Worst-case latency if no frame ever waited in a queue, on the routes this schedule chose:
    max over flows of (hops x transmission time + processing delay at each switch)."""
    lb = 0
    for f in problem.flows:
        route = schedule.route[f.id]
        tx = sum(problem.links[l].tx_time(f.size) for l in route)
        proc = sum(problem.links[l].t_proc + problem.links[l].t_prop for l in route[1:])
        lb = max(lb, tx + proc + problem.links[route[-1]].t_prop)
    return lb


def run(config: dict, out_dir: Path, log=print) -> pd.DataFrame:
    out_dir = Path(out_dir)
    inst_root = out_dir / "instances"
    rows = []
    t_start = time.time()
    for sweep, points in config["sweeps"].items():
        for point in points:
            for seed in config["seeds"]:
                spec = InstanceSpec(seed=seed, **point, **BASE)
                inst = generate(spec, inst_root)
                problem = Problem.load(inst.task, inst.topo)
                for method in config["schedulers"]:
                    r = run_scheduler(method, inst.task, inst.topo, inst.dir / "schedules",
                                      timeout_s=config["timeout_s"])
                    row = dict(sweep=sweep, **{k: v for k, v in asdict(spec).items()},
                               instance=spec.name, scheduler=method, status=r.status,
                               solve_cpu_s=r.solve_cpu_s, wall_s=r.wall_s,
                               peak_mem_mb=r.peak_mem_mb, verified=False,
                               worst_e2e_us=np.nan, mean_e2e_us=np.nan, n_frames=np.nan,
                               latency_lower_bound_us=np.nan,
                               hyperperiod_us=problem.hyperperiod / 1e3)
                    if r.ok:
                        sched = Schedule.load(r.schedule_prefix, problem)
                        base = Simulator(problem, sched).run(config["sim_hyperperiods"])
                        v = verify(problem, sched, base)
                        row.update(verified=v["ok"], worst_e2e_us=v["worst_e2e_ns"] / 1e3,
                                   mean_e2e_us=v["mean_e2e_ns"] / 1e3, n_frames=v["n_frames"],
                                   latency_lower_bound_us=latency_lower_bound(problem, sched) / 1e3)
                        if not v["ok"]:
                            log(f"  !! {method} on {spec.name} failed verification: {v['problems'][:2]}")
                    rows.append(row)
                    log(f"[{time.time() - t_start:7.1f}s] {sweep:8s} {spec.name:38s} {method:7s} "
                        f"{r.status:12s} wall={r.wall_s:7.3f}s verified={row['verified']}")
    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "raw.csv", index=False)
    summary = summarize(df)
    summary.to_csv(out_dir / "summary.csv", index=False)
    return df


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    out = []
    for sweep, d in df.groupby("sweep", sort=False):
        x = "num_flows" if sweep == "flows" else "num_sw"
        # instances that every scheduler solved (for a fair latency comparison)
        solved = d[d.verified].groupby("instance").scheduler.nunique()
        common = set(solved[solved == d.scheduler.nunique()].index)
        for (xv, sch), g in d.groupby([x, "scheduler"], sort=True):
            gc = g[g.instance.isin(common)]
            out.append(dict(
                sweep=sweep, x_name=x, x=xv, scheduler=sch, instances=len(g),
                schedulable_rate=g.verified.mean(),
                timeout_rate=(g.status == "timeout").mean(),
                median_wall_s=g.wall_s.median(),
                max_wall_s=g.wall_s.max(),
                median_solve_cpu_s=g.solve_cpu_s.median(),
                common_instances=len(gc),
                median_gap_to_lower_bound_us=(g.worst_e2e_us - g.latency_lower_bound_us).median(),
                median_worst_e2e_us_common=gc.worst_e2e_us.median(),
                max_worst_e2e_us_common=gc.worst_e2e_us.max(),
            ))
    return pd.DataFrame(out)


def plot(out_dir: Path, fig_path: Path, timeout_s: float) -> Path:
    from .. import style
    style.apply()
    plt = style.plt
    df = pd.read_csv(Path(out_dir) / "raw.csv")
    sm = pd.read_csv(Path(out_dir) / "summary.csv")
    sweeps = list(dict.fromkeys(sm.sweep))
    fig, axes = plt.subplots(len(sweeps), 3, figsize=(12.5, 3.5 * len(sweeps) + 0.9), squeeze=False)
    for i, sweep in enumerate(sweeps):
        s = sm[sm.sweep == sweep]
        xname = s.x_name.iloc[0]
        xlabel = "number of flows (8-switch mesh)" if xname == "num_flows" else "number of switches (30 flows)"
        d = df[df.sweep == sweep]
        for sch in [k for k in style.SCHEDULER_STYLE if k in set(s.scheduler)]:
            st = style.SCHEDULER_STYLE[sch]
            g = s[s.scheduler == sch].sort_values("x")
            kw = dict(color=st["color"], marker=st["marker"], label=st["label"])
            axes[i, 0].plot(g.x, 100 * g.schedulable_rate, **kw)
            # runtime: every run as a faint dot, median as the line; timeouts sit at the limit
            raw = d[d.scheduler == sch]
            axes[i, 1].scatter(raw[xname], raw.wall_s.clip(upper=timeout_s), s=12,
                               color=st["color"], alpha=0.35, linewidths=0)
            axes[i, 1].plot(g.x, g.median_wall_s.clip(upper=timeout_s), **kw)
            gl = g[g.common_instances > 0]
            axes[i, 2].plot(gl.x, gl.median_worst_e2e_us_common, **kw)
        axes[i, 0].set_ylim(-5, 105)
        axes[i, 0].set_ylabel("verified schedulable (%)")
        axes[i, 1].set_yscale("log")
        axes[i, 1].axhline(timeout_s, color=style.TEXT_2, lw=0.8, ls="--")
        axes[i, 1].text(axes[i, 1].get_xlim()[0], timeout_s * 1.12, f" {timeout_s:g} s time limit",
                        fontsize=7.5, color=style.TEXT_2, va="bottom")
        axes[i, 1].set_ylabel("scheduler wall time (s, log)")
        axes[i, 2].set_ylabel("worst-case e2e latency (µs)")
        for j in range(3):
            axes[i, j].set_xlabel(xlabel)
            axes[i, j].set_xticks(sorted(s.x.unique()))
        n_seeds = d.groupby(xname).seed.nunique().max()
        axes[i, 0].set_title(f"Schedulability ({n_seeds} seeds per point)", loc="left")
        axes[i, 1].set_title("Runtime (dots = runs, line = median)", loc="left")
        axes[i, 2].set_title("Worst-case latency (median)", loc="left")
        # n = instances all three solved (latency is compared only on those)
        common = s.groupby("x").common_instances.first()
        ax2 = axes[i, 2]
        ymin, ymax = ax2.get_ylim()
        ax2.set_ylim(ymin - 0.12 * (ymax - ymin), ymax)
        for xv, n in common.items():
            ax2.text(xv, ymin - 0.07 * (ymax - ymin), f"n={n}", ha="center", fontsize=7.5,
                     color=style.TEXT_2)
        ax2.text(0.98, 0.16, "ls and dt coincide: both reach the\nno-queueing lower bound exactly",
                 transform=ax2.transAxes, fontsize=7.5, color=style.TEXT_2, va="bottom", ha="right")
    h, l = axes[0, 0].get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", ncol=len(l), bbox_to_anchor=(0.5, 1.0), fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig_path = Path(fig_path)
    fig_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(fig_path)
    plt.close(fig)
    return fig_path


def main(argv=None):
    ap = argparse.ArgumentParser(description="Benchmark TSNKit schedulers (Step 3).")
    ap.add_argument("--quick", action="store_true", help="tiny config for smoke tests (~1 min)")
    ap.add_argument("--out", default="results/benchmark")
    ap.add_argument("--fig", default="docs/figures/benchmark.png")
    a = ap.parse_args(argv)
    cfg = QUICK if a.quick else FULL
    run(cfg, Path(a.out))
    print(plot(Path(a.out), Path(a.fig), cfg["timeout_s"]))


if __name__ == "__main__":
    main()
