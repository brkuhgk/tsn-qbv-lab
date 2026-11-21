# Draft issue for ChuanyuXue/tsnkit

**Title:** Constant flags (`-e_sync`, `-t_slot`, …) are rejected by every algorithm CLI, and `E_SYNC` set via `tsnkit.core` never reaches `smt_wa`

Tested at commit `8924ff2` (v0.3.0), Python 3.13, Linux.

## 1. Constant flags make every scheduler CLI exit

`tsnkit/core/_common.py::parse_command_line_args()` calls `parser.parse_args()` **before**
`parse_command_line_constants(parser)` registers the constant flags, so argparse rejects them:

```
$ python -m tsnkit.algorithms.smt_wa task.csv topo.csv -e_sync 100
smt_wa.py: error: unrecognized arguments: -e_sync 100
$ python -m tsnkit.algorithms.ls task.csv topo.csv -t_slot 200
ls.py: error: unrecognized arguments: -t_slot 200
```

## 2. `core.E_SYNC` does not reach `Link.t_sync`

`parse_command_line_constants` assigns `core.E_SYNC = args.e_sync`, but `_network.py` does
`from ._constants import T_SLOT, E_SYNC, NUM_PORT`, which copies the value once at import time.
`Link._t_sync` / `Node._sync_error` therefore stay 0. `smt_wa` reads the guard band through
`link.t_sync` (`add_flow_trans_const`, `add_delay_const`, `add_frame_isolation_const`), so it
silently ignores it. `at`, `smt_fr`, `smt_pr` and `i_omt` read `utils.E_SYNC` at call time and *do*
see it, so the same flag behaves differently across methods.

```python
import tsnkit.core as core, tsnkit.core._network as N
core.E_SYNC = 50
# ... build a network / run smt_wa ...
N.E_SYNC          # -> 0, every Link has t_sync == 0
```

## Proposed fix (attached: `tsnkit-constant-flags.patch`, 12 lines)

* `_common.py`: call `parse_command_line_constants(parser)` first (it registers and parses once),
  and also update `_constants.E_SYNC`.
* `_network.py`: read `_constants.E_SYNC` when a `Link`/`Node` is constructed instead of the
  import-time copy.

Verified on a 4-switch, 8-flow instance:
* with the patch, `python -m tsnkit.algorithms.smt_wa task topo -e_sync 100` runs and produces a
  schedule byte-identical to setting the guard band programmatically;
* without any flag, `smt_wa`, `ls` and `dt` produce byte-identical schedules to unpatched TSNKit.

## Open question (not changed by the patch)

`E_SYNC` is used in TSNKit's internal slot unit (`T_SLOT` = 100 ns), whereas `t_proc` / `t_prop`
in `topo.csv` are in ns and converted. Should `-e_sync` take ns for consistency? The same
import-time-copy pattern also affects `T_SLOT` in `_network.py`, `_stream.py` and `_config.py`.
