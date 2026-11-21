"""Run a TSNKit scheduler on an instance with a hard time limit."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: Schedulers this project uses. All three run without a commercial solver licence.
SCHEDULERS = {
    "smt_wa": "SMT, exact (z3)",
    "ls": "List-scheduling heuristic",
    "dt": "Divisibility-theory heuristic",
}

STATUS_MAP = {"succ": "schedulable", "fail": "infeasible", "unkwon": "unknown", "err": "error"}


@dataclass
class RunResult:
    method: str
    status: str  # schedulable | infeasible | unknown | error | timeout
    solve_cpu_s: float  # TSNKit's own measurement: CPU time of solve() only
    wall_s: float  # wall-clock time of init+prepare+solve+output (excludes Python start-up)
    peak_mem_mb: float
    schedule_prefix: Optional[Path]  # e.g. out/ls  ->  out/ls-GCL.csv, out/ls-OFFSET.csv, ...
    guard_ns: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "schedulable"


def run_scheduler(
    method: str,
    task: os.PathLike,
    topo: os.PathLike,
    out_dir: os.PathLike,
    *,
    timeout_s: float = 60.0,
    guard_ns: int = 0,
    name: Optional[str] = None,
) -> RunResult:
    """Run ``tsnkit.algorithms.<method>`` in a fresh Python process.

    Results are cached: if ``out_dir`` already holds a result for the same name, it is reused.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = name or (method if guard_ns == 0 else f"{method}+g{guard_ns}")
    result_json = out_dir / f"{name}.result.json"
    prefix = out_dir / name

    if not result_json.exists():
        cmd = [
            sys.executable, "-m", "qbvlab._entry", method, str(task), str(topo),
            str(out_dir), name, str(result_json), str(guard_ns),
        ]
        t0 = time.perf_counter()
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            out, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)  # kill the solver and anything it spawned
            proc.communicate()
            result_json.write_text(json.dumps({
                "status": "timeout", "solve_cpu_s": None,
                "wall_s": time.perf_counter() - t0, "peak_mem_mb": None,
            }))
        else:
            if proc.returncode != 0 or not result_json.exists():
                result_json.write_text(json.dumps({
                    "status": "error", "solve_cpu_s": None, "wall_s": None,
                    "peak_mem_mb": None, "stdout_tail": out.decode(errors="replace")[-2000:],
                }))

    r = json.loads(result_json.read_text())
    status = STATUS_MAP.get(r["status"], r["status"])
    has_schedule = Path(f"{prefix}-GCL.csv").exists()
    if status == "schedulable" and not has_schedule:
        status = "error"
    return RunResult(
        method=method,
        status=status,
        solve_cpu_s=r.get("solve_cpu_s") if r.get("solve_cpu_s") is not None else float("nan"),
        wall_s=r.get("wall_s") if r.get("wall_s") is not None else float("nan"),
        peak_mem_mb=r.get("peak_mem_mb") if r.get("peak_mem_mb") is not None else float("nan"),
        schedule_prefix=prefix if status == "schedulable" else None,
        guard_ns=guard_ns,
    )
