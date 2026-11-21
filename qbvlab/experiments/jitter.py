"""Step 4: end-station send-time error ("transmission determinism at the talker").

Every TSNKit scheduler assumes the talker's NIC opens its gate exactly on schedule. Standard
NICs without hardware launch-time support cannot do that: the frame leaves at the intended
time plus an error. Here each talker is modelled as a standard (ungated) NIC that hands
frame ``(flow, k)`` to the wire at ``intended + error`` and we measure what the switches do
with it.

Error models (``e`` in {0, 1, 5, 10, 30} us):

* ``uniform``: error ~ U[-e, +e], drawn per frame. Common random numbers: the same
  ``u ~ U[-1, 1]`` per frame is reused for every ``e`` and every scheduler (``error = u * e``),
  so differences between curves come from the schedules, not from sampling noise.
* ``late``: error = +e for every frame (worst case for lateness).

Metrics per run (all frames in ``hyperperiods`` hyperperiods):

* **gate-window miss rate**: fraction of switch hops where the frame did not start in the
  window instance the schedule gave it (see :func:`qbvlab.sim.compare`).
* **extra latency**: delivered time minus delivered time in the zero-error run.
* **deadline-miss rate**: frames delivered later than ``intended start + deadline`` (or lost).

* **flows affected**: fraction of flows with at least one window miss.

Backlog effect: TSNKit sizes each gate window to fit exactly one frame. When a frame misses
its window it waits for the next window of its queue, which belongs to another frame (often
the same flow's next frame). There is no spare capacity, so the queue never drains, and
every later frame of that flow is also one window late. A single late frame therefore turns
into a permanent one-period delay. Frame-level rates grow with the simulated horizon;
"flows affected" is the horizon-independent view.

First-miss attribution: for each affected flow, the cause of its *first* miss is classified
as its own error, the talker's wire being busy with a neighbouring frame (standard NICs send
first-in, first-out, so independent errors make frames of one talker collide), a blocked
queue at the switch, or a downstream hop (see :func:`first_miss_causes`).

``summary.csv`` uses only instances that every variant scheduled, so the variants are compared
on the same networks. ``summary_all_instances.csv`` uses every instance each variant solved.

Mechanism check: from the zero-error run we compute, per frame, its *late tolerance* at
the first switch, i.e. how late it may arrive and still fit its window. Under the ``late``
model the predicted first-switch miss rate is the fraction of frames whose tolerance is
below ``e``. Comparing that with the simulated miss rate shows whether the mechanism is
understood.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd

from ..instances import InstanceSpec, generate
from ..model import Problem, Schedule
from ..runner import run_scheduler
from ..sim import Simulator, compare, verify

#: name -> (TSNKit method, guard band in ns passed to TSNKit as E_SYNC)
VARIANTS = {
    "smt_wa": ("smt_wa", 0),
    "ls": ("ls", 0),
    "dt": ("dt", 0),
    "smt_wa+guard": ("smt_wa", 10_000),
}

FULL = dict(
    instance=dict(num_sw=8, num_flows=30, topo="mesh", period_opt=4, size_opt=3, deadline_opt=1),
    seeds=list(range(10)),
    variants=list(VARIANTS),
    errors_us=[0, 1, 5, 10, 30],
    models=["uniform", "late"],
    hyperperiods=20,
    error_seed=2026,
    timeout_s=120,
)

QUICK = dict(FULL, instance=dict(FULL["instance"], num_sw=4, num_flows=8), seeds=[0],
             hyperperiods=4, timeout_s=60)


def first_hop_tolerance(problem: Problem, schedule: Schedule, base) -> np.ndarray:
    """Per frame: (latest start that still fits the first switch window) - (arrival time)."""
    sim = Simulator(problem, schedule)
    flows = {f.id: f for f in problem.flows}
    out = []
    for (fid, _k), rec in base.records.items():
        route = schedule.route[fid]
        if len(route) < 2:
            continue
        l0, l1 = problem.links[route[0]], problem.links[route[1]]
        arrival = rec.hop_start[0] + l0.tx_time(flows[fid].size) + l0.t_prop + l1.t_proc
        ws, we, _ = sim.window_at(route[1], rec.hop_start[1])
        out.append((we - l1.tx_time(flows[fid].size)) - arrival)
    return np.array(out)


FIRST_MISS_CAUSES = ("own_error", "talker_wire", "queue_blocked", "downstream")


def first_miss_causes(problem: Problem, schedule: Schedule, base, run, send_time) -> dict:
    """For every flow that missed at least one window: why did its *first* miss happen?

    * ``own_error``: the frame's own send-time error made it arrive after the latest start that
      still fits its window.
    * ``talker_wire``: its own error alone would have been fine, but the standard NIC was still
      sending another frame of the same talker, which delayed it past its window.
    * ``queue_blocked``: it arrived in time, but another frame was ahead of it in the queue.
    * ``downstream``: the first miss happened after the first switch.
    """
    sim = Simulator(problem, schedule)
    flows = {f.id: f for f in problem.flows}
    first = {}
    for key, b in base.records.items():
        r = run.records[key]
        fid = key[0]
        route = schedule.route[fid]
        size = flows[fid].size
        for h in range(1, len(route)):
            ws, we, _ = sim.window_at(route[h], b.hop_start[h])
            latest = we - problem.links[route[h]].tx_time(size)
            a = r.hop_start[h]
            if a is not None and ws <= a <= latest:
                continue
            if h == 1 and r.hop_start[0] is not None:
                l0, l1 = problem.links[route[0]], problem.links[route[1]]
                hop = l0.tx_time(size) + l0.t_prop + l1.t_proc
                if r.hop_start[0] + hop <= latest:
                    cause = "queue_blocked"
                elif send_time[key] + hop <= latest:
                    cause = "talker_wire"
                else:
                    cause = "own_error"
            else:
                cause = "downstream"
            t = b.hop_start[h]
            if fid not in first or t < first[fid][0]:
                first[fid] = (t, cause)
            break
    counts = {c: 0 for c in FIRST_MISS_CAUSES}
    for _, cause in first.values():
        counts[cause] += 1
    return counts


def error_vector(keys, model: str, e_ns: int, u: np.ndarray) -> np.ndarray:
    if model == "uniform":
        return np.rint(u * e_ns).astype(np.int64)
    if model == "late":
        return np.full(len(keys), e_ns, dtype=np.int64)
    raise ValueError(model)


def run(config: dict, out_dir: Path, log=print):
    out_dir = Path(out_dir)
    rows, tol_rows, sched_rows = [], [], []
    t0 = time.time()
    for seed in config["seeds"]:
        spec = InstanceSpec(seed=seed, **config["instance"])
        inst = generate(spec, out_dir / "instances")
        problem = Problem.load(inst.task, inst.topo)
        for var in config["variants"]:
            method, guard = VARIANTS[var]
            r = run_scheduler(method, inst.task, inst.topo, inst.dir / "schedules",
                              timeout_s=config["timeout_s"], guard_ns=guard)
            sched_rows.append(dict(instance=spec.name, variant=var, status=r.status,
                                   wall_s=r.wall_s))
            if not r.ok:
                log(f"  {var} on {spec.name}: {r.status}")
                continue
            sched = Schedule.load(r.schedule_prefix, problem)
            sim = Simulator(problem, sched)
            base = sim.run(config["hyperperiods"])
            v = verify(problem, sched, base)
            if not v["ok"]:
                log(f"  !! {var} on {spec.name} failed zero-error verification; skipped")
                sched_rows[-1]["status"] = "unverified"
                continue
            sched_rows[-1]["worst_e2e_us"] = v["worst_e2e_ns"] / 1e3
            sched_rows[-1]["mean_e2e_us"] = v["mean_e2e_ns"] / 1e3
            tol = first_hop_tolerance(problem, sched, base)
            tol_rows += [dict(instance=spec.name, variant=var, tolerance_us=t / 1e3) for t in tol]
            intended = base.first_tx()
            keys = sorted(intended)
            # common random numbers: one u per frame, shared by all variants and error sizes
            rng = np.random.default_rng([config["error_seed"], seed])
            u = rng.uniform(-1.0, 1.0, size=len(keys))
            for model in config["models"]:
                for e_us in config["errors_us"]:
                    err = error_vector(keys, model, int(e_us * 1000), u)
                    send = {k: intended[k] + int(x) for k, x in zip(keys, err)}
                    run_ = sim.run(config["hyperperiods"], talker_gated=False, send_time=send)
                    m = compare(problem, sched, base, run_)
                    causes = first_miss_causes(problem, sched, base, run_, send)
                    x = np.array(m.extra_latency_ns) / 1e3
                    rows.append(dict(
                        instance=spec.name, seed=seed, variant=var, model=model, error_us=e_us,
                        frames=m.frames, switch_hops=m.switch_hops, window_misses=m.window_misses,
                        first_switch_misses=m.first_switch_misses,
                        downstream_hops=m.downstream_hops, downstream_misses=m.downstream_misses,
                        frames_with_miss=m.frames_with_miss, lost=m.lost,
                        flows=m.flows, flows_with_miss=m.flows_with_miss,
                        deadline_misses=m.deadline_misses,
                        extra_mean_us=float(x.mean()) if len(x) else np.nan,
                        extra_p99_us=float(np.percentile(x, 99)) if len(x) else np.nan,
                        extra_max_us=float(x.max()) if len(x) else np.nan,
                        predicted_first_switch_miss=float((tol < e_us * 1000).mean())
                        if model == "late" else np.nan,
                        **{f"first_miss_{c}": n for c, n in causes.items()},
                    ))
            log(f"[{time.time() - t0:6.1f}s] {spec.name} {var:13s} frames={len(keys)} "
                f"median first-hop tolerance={np.median(tol) / 1e3:.1f} us")
    raw = pd.DataFrame(rows)
    raw.to_csv(out_dir / "raw.csv", index=False)
    tol_df = pd.DataFrame(tol_rows)
    sch_df = pd.DataFrame(sched_rows)
    sch_df.to_csv(out_dir / "schedules.csv", index=False)
    # Fair comparison: only instances that every variant scheduled (and verified).
    ok = sch_df[sch_df.status == "schedulable"].groupby("instance").variant.nunique()
    common = set(ok[ok == len(config["variants"])].index)
    log(f"instances scheduled by all {len(config['variants'])} variants: {len(common)}")
    tol_df[tol_df.instance.isin(common)].to_csv(out_dir / "first_hop_tolerance.csv", index=False)
    summarize(raw).to_csv(out_dir / "summary_all_instances.csv", index=False)
    summary = summarize(raw[raw.instance.isin(common)])
    summary.to_csv(out_dir / "summary.csv", index=False)
    return summary


def summarize(raw: pd.DataFrame) -> pd.DataFrame:
    out = []
    for (var, model, e), g in raw.groupby(["variant", "model", "error_us"], sort=False):
        causes = {f"first_miss_{c}": int(g[f"first_miss_{c}"].sum()) for c in FIRST_MISS_CAUSES}
        out.append(dict(
            variant=var, model=model, error_us=e, instances=g.instance.nunique(),
            frames=int(g.frames.sum()),
            gate_window_miss_rate=g.window_misses.sum() / g.switch_hops.sum(),
            first_switch_miss_rate=g.first_switch_misses.sum() / g.frames.sum(),
            predicted_first_switch_miss_rate=(g.predicted_first_switch_miss * g.frames).sum()
            / g.frames.sum() if model == "late" else np.nan,
            downstream_miss_rate=g.downstream_misses.sum() / max(g.downstream_hops.sum(), 1),
            frame_miss_rate=g.frames_with_miss.sum() / g.frames.sum(),
            flows_affected_rate=g.flows_with_miss.sum() / g.flows.sum(),
            deadline_miss_rate=g.deadline_misses.sum() / g.frames.sum(),
            lost_frames=int(g.lost.sum()),
            extra_latency_mean_us=(g.extra_mean_us * g.frames).sum() / g.frames.sum(),
            extra_latency_p99_us_median_over_instances=g.extra_p99_us.median(),
            extra_latency_max_us=g.extra_max_us.max(),
            **causes,
        ))
    return pd.DataFrame(out)


def plot(out_dir: Path, fig_path: Path, tol_fig_path: Path) -> list:
    from .. import style
    style.apply()
    plt = style.plt
    out_dir = Path(out_dir)
    sm = pd.read_csv(out_dir / "summary.csv")
    errs = sorted(sm.error_us.unique())
    pos = {e: i for i, e in enumerate(errs)}
    models = [m for m in ("uniform", "late") if m in set(sm.model)]
    model_title = {"uniform": "error ~ U[−e, +e]", "late": "every frame late by +e"}
    cols = [("flows_affected_rate", "flows with ≥1 window miss (%)", 100),
            ("gate_window_miss_rate", "gate-window miss rate (%)", 100),
            ("deadline_miss_rate", "deadline-miss rate (%)", 100),
            ("extra_latency_p99_us_median_over_instances", "p99 extra latency (µs)", 1)]
    fig, axes = plt.subplots(len(models), 4, figsize=(16, 3.5 * len(models) + 0.9), squeeze=False)
    for i, model in enumerate(models):
        for j, (col, ylabel, scale) in enumerate(cols):
            ax = axes[i, j]
            for var in [v for v in VARIANTS if v in set(sm.variant)]:
                st = style.SCHEDULER_STYLE[var]
                g = sm[(sm.model == model) & (sm.variant == var)].sort_values("error_us")
                ax.plot([pos[e] for e in g.error_us], g[col] * scale, color=st["color"],
                        marker=st["marker"], label=st["label"])
            ax.set_xticks(range(len(errs)))
            ax.set_xticklabels([f"{e:g}" for e in errs])
            ax.set_xlabel("talker send-time error e (µs)")
            ax.set_ylabel(ylabel)
            ax.set_title(f"{ylabel.split(' (')[0][0].upper() + ylabel.split(' (')[0][1:]}\n"
                         f"{model_title[model]}", loc="left", fontsize=9.5)
            if scale == 100:
                ax.set_ylim(-4, 104)
    h, l = axes[0, 0].get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", ncol=len(l), bbox_to_anchor=(0.5, 1.0), fontsize=9)
    n_inst = int(sm.instances.max())
    fig.text(0.5, 0.955, f"{n_inst} instances that all variants scheduled. ls and dt lie on top of "
             "each other wherever they reach 100%.", ha="center", fontsize=8.5, color=style.TEXT_2)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(fig_path)
    plt.close(fig)

    # Mechanism figure: distribution of first-hop late tolerance + simulated miss rates.
    tol = pd.read_csv(out_dir / "first_hop_tolerance.csv")
    fig, ax = plt.subplots(figsize=(8.5, 4.4))
    for var in [v for v in VARIANTS if v in set(tol.variant)]:
        st = style.SCHEDULER_STYLE[var]
        t = np.sort(tol[tol.variant == var].tolerance_us.values)
        cdf = np.arange(1, len(t) + 1) / len(t)
        ax.step(np.maximum(t, 0.05), cdf * 100, where="post", color=st["color"], label=st["label"])
        g = sm[(sm.model == "late") & (sm.variant == var) & (sm.error_us > 0)]
        ax.scatter(g.error_us, g.first_switch_miss_rate * 100, color=st["color"],
                   marker=st["marker"], s=42, zorder=3, edgecolors=style.SURFACE, linewidths=1)
    ax.set_xscale("log")
    ax.set_xlabel("late-arrival tolerance at the first switch (µs, log; ≤0 drawn at 0.05)")
    ax.set_ylabel("frames with tolerance below x (%)")
    fig.text(0.01, 0.975, "Why frames miss: how much lateness each frame can absorb at its first switch",
             fontsize=11, fontweight="bold", color=style.TEXT, va="top")
    fig.text(0.01, 0.925, "Lines: share of frames whose slack is below x, read from the zero-error "
             "schedule (prediction). Markers: simulated first-switch miss\nrate when every frame "
             "is late by x. ls and dt coincide: every one of their frames has zero slack.",
             fontsize=8, color=style.TEXT_2, va="top")
    ax.legend(loc="lower right")
    fig.tight_layout(rect=(0, 0, 1, 0.84))
    fig.savefig(tol_fig_path)
    plt.close(fig)
    return [fig_path, tol_fig_path]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Talker send-time error experiment (Step 4).")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default="results/jitter")
    ap.add_argument("--fig", default="docs/figures/jitter.png")
    ap.add_argument("--tol-fig", default="docs/figures/jitter_mechanism.png")
    a = ap.parse_args(argv)
    cfg = QUICK if a.quick else FULL
    Path(a.out).mkdir(parents=True, exist_ok=True)
    s = run(cfg, Path(a.out))
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(s.round(4).to_string(index=False))
    print(plot(Path(a.out), Path(a.fig), Path(a.tol_fig)))


if __name__ == "__main__":
    main()
