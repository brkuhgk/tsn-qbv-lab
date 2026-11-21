"""Cross-check: with zero error, our event-driven simulator reproduces TSNKit's own
simulator (``tsnkit.simulation.tas``) frame for frame.

TSNKit's log per flow is ``[send_times, receive_times]`` where (see tas.py)
send time = the moment the frame becomes eligible at the first switch
          = first-hop transmission start + transmission time + T_PROC, and
receive time = end of transmission on the last hop.
"""

import shutil

import pytest

from qbvlab.sim import Simulator

T_PROC = 2000


@pytest.mark.slow
@pytest.mark.parametrize("method", ["smt_wa", "ls", "dt"])
def test_matches_tsnkit_simulator(small, method, tmp_path):
    from tsnkit.simulation.tas import simulation

    inst, problem, schedules = small
    sched = schedules[method]
    # TSNKit's simulator selects config files by prefix, so give it a clean folder.
    d = tmp_path / method
    d.mkdir()
    src = inst.dir / method
    for kind in ("GCL", "OFFSET", "QUEUE", "ROUTE"):
        shutil.copy(src / f"{method}-{kind}.csv", d / f"{method}-{kind}.csv")

    iters = 2
    log, _ = simulation(str(inst.task), str(d / method), it=iters,
                        draw_results=False, disable_pbar=True)

    ours = Simulator(problem, sched).run(iters)
    flows = {f.id: f for f in problem.flows}
    compared = 0
    for f in problem.flows:
        recs = sorted((r for (fid, _), r in ours.records.items() if fid == f.id),
                      key=lambda r: r.instance)
        tx = problem.links[sched.route[f.id][0]].tx_time(flows[f.id].size)
        our_send = [r.hop_start[0] + tx + T_PROC for r in recs]
        our_recv = [r.delivered for r in recs]
        tsn_send, tsn_recv = log[f.id]
        assert len(tsn_recv) > 0
        # TSNKit stops at the horizon, so frames still in flight are missing from its log.
        assert tsn_send == our_send[: len(tsn_send)]
        assert tsn_recv == our_recv[: len(tsn_recv)]
        compared += len(tsn_recv)
    assert compared >= len(problem.flows)
