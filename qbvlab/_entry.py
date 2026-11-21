"""Subprocess entry point that runs one TSNKit scheduler and writes a JSON result.

Usage (internal, called by :mod:`qbvlab.runner`)::

    python -m qbvlab._entry METHOD TASK TOPO OUT_DIR NAME RESULT_JSON GUARD_NS

Why a subprocess? SMT solvers can run for a long time and ignore Python signals, so the
parent enforces the time limit by killing this process.

Why not TSNKit's command line? At commit 8924ff2, ``tsnkit.core.parse_command_line_args``
calls ``parser.parse_args()`` *before* the constant flags (``-e_sync``, ``-t_slot``, ...)
are registered, so every scheduler CLI exits with "unrecognized arguments" if one is
given. In addition, ``Link`` objects copy ``E_SYNC`` from ``tsnkit.core._constants`` at
import time, so assigning ``tsnkit.core.E_SYNC`` (what the CLI helper does) never reaches
``smt_wa``. We therefore set the guard band where ``Link`` reads it. Its unit there is
TSNKit's internal time slot (``T_SLOT`` = 100 ns), not nanoseconds.
See ``upstream/ISSUE.md``.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import sys
import time


def main(argv: list[str]) -> int:
    method, task, topo, out_dir, name, result_path, guard_ns = argv
    guard_ns = int(guard_ns)

    import tsnkit.core as core
    import tsnkit.core._network as network

    if guard_ns % core.T_SLOT:
        raise SystemExit(f"guard band must be a multiple of T_SLOT={core.T_SLOT} ns")
    guard_slots = guard_ns // core.T_SLOT
    core.E_SYNC = guard_slots  # read at call time by at / smt_fr / smt_pr / i_omt
    network.E_SYNC = guard_slots  # read by Link/Node constructors (smt_wa uses link.t_sync)

    algo = importlib.import_module(f"tsnkit.algorithms.{method}")
    buf = io.StringIO()
    t0 = time.perf_counter()
    with contextlib.redirect_stdout(buf):
        stat = algo.benchmark(name, task, topo, out_dir.rstrip("/") + "/", workers=1)
    wall = time.perf_counter() - t0

    result = {
        "status": str(stat.result),  # succ / fail / unkwon / err (TSNKit spelling)
        "solve_cpu_s": float(stat.algo_time),
        "total_cpu_s": float(stat.total_time),
        "peak_mem_mb": float(stat.total_mem),
        "wall_s": wall,
        "stdout_tail": buf.getvalue()[-2000:],
    }
    with open(result_path, "w") as f:
        json.dump(result, f)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
