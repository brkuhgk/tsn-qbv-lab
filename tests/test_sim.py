"""Hand-checked behaviour of the 802.1Qbv simulator on tiny networks.

Network used below:  talker ES10 --(10,0)--> SW0 --(0,11)--> listener ES11
1 Gbps links, 2 us processing delay per hop, no propagation delay.
A 125-byte frame takes 1000 ns to transmit.
"""

import pytest

from qbvlab.sim import Simulator, compare, verify

from .conftest import write_case

LINKS = [(10, 0), (0, 10), (0, 11), (11, 0)]
ROUTE = {0: [(10, 0), (0, 11)]}
P = 100_000  # period = cycle = 100 us


def case(tmp_path, sw_window=(3000, 4000), period=P):
    # Talker sends at t=0 for 1 us; it reaches SW0's egress at 0 + 1000 + 2000 = 3000.
    return write_case(
        tmp_path, flows=[(0, 10, 11, 125, period, period)], links=LINKS,
        gcl=[((10, 0), 0, 0, 1000), ((0, 11), 0, *sw_window)],
        offsets={0: 0}, routes=ROUTE, cycle=period)


def send(base, delta):
    return {k: t + delta for k, t in base.first_tx().items()}


def test_zero_error_baseline(tmp_path):
    p, s = case(tmp_path)
    base = Simulator(p, s).run(1)
    rec = base.records[(0, 0)]
    assert rec.hop_start == [0, 3000]
    assert rec.delivered == 4000
    v = verify(p, s, base)
    assert v["ok"] and v["worst_e2e_ns"] == 4000


def test_ungated_talker_without_error_matches_baseline(tmp_path):
    p, s = case(tmp_path)
    sim = Simulator(p, s)
    base = sim.run(3)
    run = sim.run(3, talker_gated=False, send_time=send(base, 0))
    assert {k: r.hop_start for k, r in run.records.items()} == \
           {k: r.hop_start for k, r in base.records.items()}
    m = compare(p, s, base, run)
    assert m.window_misses == 0 and max(m.extra_latency_ns) == 0


def test_late_by_one_ns_misses_exact_fit_window(tmp_path):
    p, s = case(tmp_path)
    sim = Simulator(p, s)
    base = sim.run(1)
    run = sim.run(1, talker_gated=False, send_time=send(base, 1))
    # arrives at 3001; only 999 ns of the window left; must wait one full cycle
    assert run.records[(0, 0)].hop_start[1] == P + 3000
    m = compare(p, s, base, run)
    assert m.window_misses == 1 and m.first_switch_misses == 1
    assert m.extra_latency_ns == [P]
    assert m.deadline_misses == 1


def test_early_frame_waits_for_its_window(tmp_path):
    p, s = case(tmp_path)
    sim = Simulator(p, s)
    base = sim.run(1)
    run = sim.run(1, talker_gated=False, send_time=send(base, -500))
    assert run.records[(0, 0)].hop_start[1] == 3000  # gate opens at 3000, not at arrival
    m = compare(p, s, base, run)
    assert m.window_misses == 0 and m.extra_latency_ns == [0]


def test_slack_in_window_absorbs_small_lateness(tmp_path):
    p, s = case(tmp_path, sw_window=(3000, 4500))  # 500 ns spare
    sim = Simulator(p, s)
    base = sim.run(1)
    ok = sim.run(1, talker_gated=False, send_time=send(base, 400))
    assert ok.records[(0, 0)].hop_start[1] == 3400
    assert compare(p, s, base, ok).window_misses == 0
    bad = sim.run(1, talker_gated=False, send_time=send(base, 600))
    assert compare(p, s, base, bad).window_misses == 1


def test_one_late_frame_causes_permanent_backlog(tmp_path):
    """Exact-fit windows have no spare capacity, so one miss shifts every later frame."""
    p, s = case(tmp_path)
    sim = Simulator(p, s)
    base = sim.run(5)
    st = send(base, 0)
    st[(0, 0)] += 1  # only the very first frame is late, by 1 ns
    run = sim.run(5, talker_gated=False, send_time=st)
    for k in range(5):
        assert run.records[(0, k)].hop_start[1] == (k + 1) * P + 3000
    m = compare(p, s, base, run)
    assert m.frames_with_miss == 5 and m.flows_with_miss == 1
    assert m.extra_latency_ns == [P] * 5


def test_standard_nic_serialises_frames(tmp_path):
    # Two flows from the same talker, scheduled back to back on its NIC.
    links = LINKS
    routes = {0: [(10, 0), (0, 11)], 1: [(10, 0), (0, 11)]}
    p, s = write_case(
        tmp_path, flows=[(0, 10, 11, 125, P, P), (1, 10, 11, 125, P, P)], links=links,
        gcl=[((10, 0), 0, 0, 1000), ((10, 0), 1, 1000, 2000),
             ((0, 11), 0, 3000, 4000), ((0, 11), 1, 4000, 5000)],
        offsets={0: 0, 1: 1000}, routes=routes,
        queues={(1, (10, 0)): 1, (1, (0, 11)): 1})
    sim = Simulator(p, s)
    base = sim.run(1)
    st = send(base, 0)
    st[(0, 0)] += 500  # flow 0 leaves late and still occupies the wire at t=1000
    run = sim.run(1, talker_gated=False, send_time=st)
    assert run.records[(0, 0)].hop_start[0] == 500
    assert run.records[(1, 0)].hop_start[0] == 1500  # had to wait for the wire

    # Attribution: flow 0 missed because of its own error; flow 1 left on time but was held
    # back by flow 0 on the talker's wire (arrives 4500 > latest fitting start 4000).
    from qbvlab.experiments.jitter import first_miss_causes
    causes = first_miss_causes(p, s, base, run, st)
    assert causes == {"own_error": 1, "talker_wire": 1, "queue_blocked": 0, "downstream": 0}


def test_window_at_semantics(tmp_path):
    p, s = case(tmp_path)
    sim = Simulator(p, s)
    assert sim.window_at((0, 11), 2999) is None
    assert sim.window_at((0, 11), 3000) == (3000, 4000, 0)
    assert sim.window_at((0, 11), 4000) is None  # after the last entry's end: closed
    assert sim.window_at((0, 11), P + 3500) == (P + 3000, P + 4000, 0)


@pytest.mark.parametrize("method", ["smt_wa", "ls", "dt"])
def test_real_schedules_verify_and_are_stable(small, method):
    _, problem, schedules = small
    sim = Simulator(problem, schedules[method])
    base = sim.run(3)
    assert verify(problem, schedules[method], base)["ok"]
    run = sim.run(3, talker_gated=False, send_time=base.first_tx())
    m = compare(problem, schedules[method], base, run)
    assert m.window_misses == 0 and m.lost == 0 and set(m.extra_latency_ns) == {0}
