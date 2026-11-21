#!/usr/bin/env bash
# One command to reproduce everything:  ./run_all.sh          (full run, roughly 15-25 min)
#                                        ./run_all.sh --quick  (smoke run, about 1 min)
# Creates .venv, installs qbvlab + the pinned TSNKit commit, runs the tests,
# then Step 2 (gate timetables), Step 3 (benchmark) and Step 4 (send-time error experiment).
set -euo pipefail
cd "$(dirname "$0")"

QUICK=""
if [[ "${1:-}" == "--quick" ]]; then QUICK="--quick"; fi
PY="${PYTHON:-python3}"

if [[ ! -d .venv ]]; then
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q --upgrade pip
python -m pip install -q -e ".[test]"

echo "== Tests"
python -m pytest -q

echo "== Step 2: gate timetables"
python - <<'EOF'
from pathlib import Path
from qbvlab.instances import InstanceSpec, generate
from qbvlab.model import Problem, Schedule
from qbvlab.runner import run_scheduler
from qbvlab.visualize import export_gate_lists, plot_gcl

inst = generate(InstanceSpec(num_sw=4, num_flows=8, seed=42, topo="line", period_opt=1,
                             size_opt=2, deadline_opt=1), "results/gcl_demo/instances")
p = Problem.load(inst.task, inst.topo)
for m in ("smt_wa", "ls"):
    r = run_scheduler(m, inst.task, inst.topo, inst.dir / "schedules", timeout_s=60)
    s = Schedule.load(r.schedule_prefix, p)
    print(plot_gcl(p, s, Path(f"docs/figures/gcl_{m}.png"), highlight_flow=1,
                   title=f"Gate timetable — {m} — 4-switch line, 8 flows (outlined: flow 1)"))
    export_gate_lists(p, s, Path("results/gcl_demo"))
EOF

if [[ -n "$QUICK" ]]; then
  OUT=results/quick
  echo "== Step 3 (quick)"; python -m qbvlab.experiments.benchmark --quick --out $OUT/benchmark --fig $OUT/benchmark.png
  echo "== Step 4 (quick)"; python -m qbvlab.experiments.jitter --quick --out $OUT/jitter --fig $OUT/jitter.png --tol-fig $OUT/jitter_mechanism.png
else
  echo "== Step 3: benchmark";  python -m qbvlab.experiments.benchmark
  echo "== Step 4: send-time error experiment"; python -m qbvlab.experiments.jitter
fi
echo "Done. Results in results/, figures in docs/figures/."
