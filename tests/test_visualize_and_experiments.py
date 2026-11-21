import pandas as pd
import pytest

from qbvlab.experiments import benchmark, jitter
from qbvlab.visualize import export_gate_lists, plot_gcl, window_flows


@pytest.mark.parametrize("method", ["smt_wa", "ls"])
def test_plot_and_export(small, method, tmp_path):
    _, problem, schedules = small
    s = schedules[method]
    png = plot_gcl(problem, s, tmp_path / "g.png", highlight_flow=0)
    assert png.stat().st_size > 10_000
    csv, sh = export_gate_lists(problem, s, tmp_path)
    df = pd.read_csv(csv)
    sums = df.groupby("link").interval_ns.sum()
    assert set(sums.values) == set(s.cycle.values())
    assert sh.read_text().count("sched-entry") == len(df)


def test_every_used_window_belongs_to_one_flow(small):
    _, problem, schedules = small
    for s in schedules.values():
        for key, flows in window_flows(problem, s).items():
            assert len(set(flows)) == 1, key


@pytest.mark.slow
def test_quick_benchmark(tmp_path):
    df = benchmark.run(benchmark.QUICK, tmp_path, log=lambda *_: None)
    assert set(df.scheduler) == {"smt_wa", "ls", "dt"}
    ok = df[df.status == "schedulable"]
    assert ok.verified.all()  # every schedule TSNKit reports as feasible survives replay
    assert (tmp_path / "summary.csv").exists()
    assert benchmark.plot(tmp_path, tmp_path / "b.png", 30).exists()


@pytest.mark.slow
def test_quick_jitter(tmp_path):
    s = jitter.run(jitter.QUICK, tmp_path, log=lambda *_: None)
    zero = s[s.error_us == 0]
    assert (zero.gate_window_miss_rate == 0).all() and (zero.deadline_miss_rate == 0).all()
    g = s[(s.variant == "smt_wa+guard") & (s.error_us <= 10)]
    assert (g.gate_window_miss_rate == 0).all()  # a 10 us guard band absorbs <= 10 us errors
    late = s[(s.model == "late") & (s.error_us > 0)]
    # a frame whose slack is below e cannot fit its window, so the slack distribution is a
    # lower bound on the simulated first-switch miss rate (backlog can only add misses)
    assert (late.first_switch_miss_rate >= late.predicted_first_switch_miss_rate - 1e-12).all()
    assert all(p.exists() for p in jitter.plot(tmp_path, tmp_path / "j.png", tmp_path / "t.png"))
