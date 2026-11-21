import pytest

from qbvlab.gcl import GateEntry, schedule_gate_lists, taprio_command, to_gate_entries
from qbvlab.model import GateWindow


def test_conversion_by_hand():
    # queue 0 open [10,20), queue 3 open [20,25), cycle 100
    e = to_gate_entries([GateWindow(0, 10, 20), GateWindow(3, 20, 25)], 100)
    assert e == [GateEntry(0, 10, 0b0), GateEntry(10, 10, 0b1),
                 GateEntry(20, 5, 0b1000), GateEntry(25, 75, 0b0)]
    assert e[2].mask_str() == "00001000"


def test_adjacent_windows_of_same_queue_are_merged():
    e = to_gate_entries([GateWindow(1, 0, 10), GateWindow(1, 10, 30)], 50)
    assert e == [GateEntry(0, 30, 0b10), GateEntry(30, 20, 0)]


def test_idle_mask_and_full_cycle():
    e = to_gate_entries([GateWindow(0, 0, 50)], 50, idle_mask=0x80)
    assert e == [GateEntry(0, 50, 1)]
    e = to_gate_entries([GateWindow(0, 0, 10)], 50, idle_mask=0x80)
    assert e[-1] == GateEntry(10, 40, 0x80)


def test_overlap_warns_and_opens_both():
    with pytest.warns(UserWarning):
        e = to_gate_entries([GateWindow(0, 0, 20), GateWindow(1, 10, 30)], 40)
    assert GateEntry(10, 10, 0b11) in e


def test_window_outside_cycle_rejected():
    with pytest.raises(ValueError):
        to_gate_entries([GateWindow(0, 90, 110)], 100)


@pytest.mark.parametrize("method", ["smt_wa", "ls", "dt"])
def test_real_schedules_cover_each_cycle_exactly(small, method):
    _, _, schedules = small
    s = schedules[method]
    for link, entries in schedule_gate_lists(s).items():
        assert sum(x.interval for x in entries) == s.cycle[link]
        assert all(x.interval > 0 for x in entries)
        starts = [x.start for x in entries]
        assert starts == sorted(starts) and starts[0] == 0
        # every scheduled window appears with its queue bit set
        for w in s.gcl[link]:
            covering = [x for x in entries if x.start <= w.start < x.start + x.interval]
            assert covering and covering[0].mask >> w.queue & 1


def test_taprio_template():
    entries = to_gate_entries([GateWindow(0, 10, 20)], 100)
    cmd = taprio_command(entries, iface="eth9", base_time=0)
    lines = [l for l in cmd.splitlines() if "sched-entry" in l]
    assert [l.split()[2:4] for l in lines] == [["00", "10"], ["01", "10"], ["00", "80"]]
    assert "dev eth9" in cmd and "clockid CLOCK_TAI" in cmd
