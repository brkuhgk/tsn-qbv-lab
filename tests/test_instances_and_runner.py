import subprocess
import sys

import numpy as np

from qbvlab.experiments.jitter import first_hop_tolerance
from qbvlab.instances import InstanceSpec, generate
from qbvlab.model import Problem, Schedule
from qbvlab.runner import run_scheduler
from qbvlab.sim import Simulator


def test_same_seed_gives_identical_files(tmp_path):
    spec = InstanceSpec(num_sw=4, num_flows=10, seed=11)
    a = generate(spec, tmp_path / "a")
    b = generate(spec, tmp_path / "b")
    assert a.task.read_bytes() == b.task.read_bytes()
    assert a.topo.read_bytes() == b.topo.read_bytes()
    assert (a.dir / "spec.json").exists()


def test_different_seed_gives_different_flows(tmp_path):
    a = generate(InstanceSpec(num_sw=4, num_flows=10, seed=1), tmp_path)
    b = generate(InstanceSpec(num_sw=4, num_flows=10, seed=2), tmp_path)
    assert a.task.read_bytes() != b.task.read_bytes()


def test_seed_does_not_leak_between_calls(tmp_path):
    """Generating something else in between must not change an instance."""
    spec = InstanceSpec(num_sw=4, num_flows=10, seed=5)
    first = generate(spec, tmp_path / "x").task.read_bytes()
    generate(InstanceSpec(num_sw=6, num_flows=20, seed=99), tmp_path / "y")
    assert generate(spec, tmp_path / "z").task.read_bytes() == first


def test_timeout_is_enforced(tmp_path, small):
    inst, _, _ = small
    r = run_scheduler("smt_wa", inst.task, inst.topo, tmp_path, timeout_s=0.01, name="t")
    assert r.status == "timeout" and r.schedule_prefix is None


def test_guard_band_reaches_smt_wa(tmp_path, small):
    """Our workaround for the TSNKit E_SYNC issue: a 10 us guard band must give every frame
    at least 10 us of late-arrival tolerance at the first switch."""
    inst, problem, schedules = small
    r = run_scheduler("smt_wa", inst.task, inst.topo, tmp_path, guard_ns=10_000, timeout_s=120)
    assert r.ok
    guarded = Schedule.load(r.schedule_prefix, problem)
    tol_g = first_hop_tolerance(problem, guarded, Simulator(problem, guarded).run(1))
    tol_0 = first_hop_tolerance(problem, schedules["smt_wa"],
                                Simulator(problem, schedules["smt_wa"]).run(1))
    assert tol_g.min() >= 10_000
    assert tol_0.min() < 10_000  # without the guard band some frames have less


def test_upstream_cli_rejects_constant_flags(small):
    """Documents the TSNKit issue (upstream/ISSUE.md) at the pinned commit. If this starts
    failing after an upgrade, upstream fixed it and qbvlab._entry's workaround can go."""
    inst, _, _ = small
    out = subprocess.run(
        [sys.executable, "-m", "tsnkit.algorithms.ls", str(inst.task), str(inst.topo),
         "-e_sync", "50"], capture_output=True, text=True)
    assert out.returncode == 2 and "unrecognized arguments: -e_sync" in out.stderr


def test_upstream_core_esync_does_not_reach_links():
    import tsnkit.core as core
    import tsnkit.core._network as network

    old = core.E_SYNC, network.E_SYNC
    try:
        core.E_SYNC = 50  # what tsnkit.core.parse_command_line_constants does
        link = network.Link(id=0, src=network.Node(0, network.NodeType.sw),
                            dst=network.Node(1, network.NodeType.sw),
                            t_proc=2000, t_prop=0, q_num=8, rate=1)
        assert link.t_sync == 0
    finally:
        core.E_SYNC, network.E_SYNC = old
