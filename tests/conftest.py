from pathlib import Path

import pytest

from qbvlab.instances import InstanceSpec, generate
from qbvlab.model import Problem, Schedule
from qbvlab.runner import run_scheduler

# A small instance with mixed periods (0.5-4 ms), so flows have several frames per hyperperiod.
SMALL = InstanceSpec(num_sw=4, num_flows=8, seed=3, topo="line", period_opt=3, size_opt=2,
                     deadline_opt=1)


@pytest.fixture(scope="session")
def small(tmp_path_factory):
    """(instance, problem, {scheduler: Schedule}) for smt_wa, ls and dt."""
    root = tmp_path_factory.mktemp("inst")
    inst = generate(SMALL, root)
    problem = Problem.load(inst.task, inst.topo)
    schedules = {}
    for m in ("smt_wa", "ls", "dt"):
        r = run_scheduler(m, inst.task, inst.topo, inst.dir / m, timeout_s=120)
        assert r.ok, f"{m} did not schedule the test instance: {r.status}"
        schedules[m] = Schedule.load(r.schedule_prefix, problem)
    return inst, problem, schedules


def write_case(tmp: Path, flows, links, gcl, offsets, queues=None, routes=None, cycle=100_000):
    """Write a hand-made problem + schedule in TSNKit's CSV formats and load it back.

    flows: [(id, src, dst, size, period, deadline)]; links: [(u, v)] (1 Gbps, t_proc 2000, t_prop 0)
    gcl: [((u, v), queue, start, end)]; offsets: {flow: offset}; routes: {flow: [(u, v), ...]}
    """
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "task.csv").write_text(
        "stream,src,dst,size,period,deadline,jitter\n"
        + "".join(f"{i},{s},[{d}],{sz},{p},{dl},{dl}\n" for i, s, d, sz, p, dl in flows))
    (tmp / "topo.csv").write_text(
        "link,q_num,rate,t_proc,t_prop\n" + "".join(f"\"{l}\",8,1,2000,0\n" for l in links))
    (tmp / "x-GCL.csv").write_text(
        "link,queue,start,end,cycle\n" + "".join(f"\"{l}\",{q},{a},{b},{cycle}\n" for l, q, a, b in gcl))
    (tmp / "x-OFFSET.csv").write_text(
        "stream,frame,offset\n" + "".join(f"{f},0,{o}\n" for f, o in offsets.items()))
    routes = routes or {}
    queues = queues or {}
    (tmp / "x-ROUTE.csv").write_text(
        "stream,link\n" + "".join(f"{f},\"{l}\"\n" for f, ls in routes.items() for l in ls))
    (tmp / "x-QUEUE.csv").write_text(
        "stream,frame,link,queue\n" + "".join(
            f"{f},0,\"{l}\",{queues.get((f, l), 0)}\n" for f, ls in routes.items() for l in ls))
    p = Problem.load(tmp / "task.csv", tmp / "topo.csv")
    return p, Schedule.load(tmp / "x", p)
