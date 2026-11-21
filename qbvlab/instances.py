"""Seeded problem-instance generation on top of TSNKit's own generator.

TSNKit's ``DatasetGenerator`` draws from NumPy's *global* random state and never seeds it
(``tsnkit.core.SEED`` exists but is unused). We seed it explicitly right before each call,
so the same (parameters, seed) always produces byte-identical CSV files.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import random
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

# TSNKit topology codes (tsnkit/data/dataset_spec.py: TOPO_FUNC)
TOPOLOGIES = {"line": 0, "ring": 1, "tree": 2, "mesh": 3, "mesh2d": 4}


@dataclass(frozen=True)
class InstanceSpec:
    """Parameters of one generated instance. Option codes are TSNKit's (dataset_spec.py)."""

    num_sw: int
    num_flows: int
    seed: int
    topo: str = "mesh"
    period_opt: int = 3  # 3: periods drawn from {0.5, 1, 2, 4} ms
    size_opt: int = 2  # 2: payload drawn from {100..500} bytes
    deadline_opt: int = 1  # 1: deadline = period

    @property
    def name(self) -> str:
        return (
            f"{self.topo}-sw{self.num_sw}-f{self.num_flows}"
            f"-p{self.period_opt}s{self.size_opt}d{self.deadline_opt}-seed{self.seed}"
        )


@dataclass(frozen=True)
class Instance:
    spec: InstanceSpec
    dir: Path

    @property
    def task(self) -> Path:
        return self.dir / "task.csv"

    @property
    def topo(self) -> Path:
        return self.dir / "topo.csv"


def generate(spec: InstanceSpec, root: os.PathLike) -> Instance:
    """Generate (or reuse) the instance described by ``spec`` under ``root/spec.name``."""
    # Imported lazily so that importing qbvlab does not pull in TSNKit's heavy dependencies.
    from tsnkit.data.generator import DatasetGenerator

    out = Path(root) / spec.name
    inst = Instance(spec, out)
    if inst.task.exists() and inst.topo.exists():
        return inst

    with tempfile.TemporaryDirectory() as tmp:
        np.random.seed(spec.seed)
        random.seed(spec.seed)
        gen = DatasetGenerator(
            1,
            spec.num_flows,
            spec.num_sw,
            spec.period_opt,
            spec.size_opt,
            spec.deadline_opt,
            TOPOLOGIES[spec.topo],
        )
        # The generator prints a tqdm bar and swallows exceptions (prints them instead),
        # so capture its output and check that the files were actually written.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            gen.run(tmp + os.sep)
        task, topo = Path(tmp, "1_task.csv"), Path(tmp, "1_topo.csv")
        if not (task.exists() and topo.exists()):
            raise RuntimeError(f"TSNKit generator failed for {spec}:\n{buf.getvalue()}")
        out.mkdir(parents=True, exist_ok=True)
        shutil.copy(task, inst.task)
        shutil.copy(topo, inst.topo)
    (out / "spec.json").write_text(json.dumps(asdict(spec), indent=2) + "\n")
    return inst
