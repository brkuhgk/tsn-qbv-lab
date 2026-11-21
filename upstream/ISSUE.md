# TSNKit issue

Filed on 2026-11-21 as [ChuanyuXue/tsnkit#65](https://github.com/ChuanyuXue/tsnkit/issues/65).
The text below is exactly what was posted. The patch is also in `tsnkit-constant-flags.patch` next to this file.

---

**Title:** Constant flags (`-e_sync`, `-t_slot`, …) rejected by scheduler CLIs since merge aaea516; `E_SYNC` also never reaches `smt_wa`

**Body:**

Hi, thanks for TSNKit! While using it for a small study on talker timing errors, I ran into two related problems with the constant flags added in #62. Both reproduce on current `main` (8924ff2).

### 1. Every scheduler CLI rejects the constant flags

```
$ python -m tsnkit.algorithms.smt_wa task.csv topo.csv -e_sync 100
smt_wa.py: error: unrecognized arguments: -e_sync 100
$ python -m tsnkit.algorithms.ls task.csv topo.csv -t_slot 200
ls.py: error: unrecognized arguments: -t_slot 200
```

`parse_command_line_args()` calls `parser.parse_args()` before `parse_command_line_constants(parser)` has registered the flags. It looks like this came from the merge in aaea516 (2025-11-29), which combined the `--output` change from d147c56 with #62 and kept both `parse_args()` calls.

### 2. `E_SYNC` doesn't reach `Link.t_sync`, so `smt_wa` ignores it

`parse_command_line_constants()` sets `core.E_SYNC`, but `_network.py` does `from ._constants import T_SLOT, E_SYNC, NUM_PORT`, which copies the value once at import time. `Link._t_sync` and `Node._sync_error` therefore stay 0. `smt_wa` reads the guard band through `link.t_sync`, so it silently ignores it, while `at`, `smt_fr`, `smt_pr` and `i_omt` read `utils.E_SYNC` at call time and do see it.

### Proposed fix

```diff
--- a/tsnkit/core/_common.py
+++ b/tsnkit/core/_common.py
@@ -50,11 +50,11 @@ def parse_command_line_args():
-    parsed = parser.parse_args()
-    ## TODO: Put me somewhere else.
-    os.makedirs(parsed.output, exist_ok=True)
-
+    # Register the constant flags (-e_sync, -t_slot, ...) *before* parsing, otherwise
+    # argparse rejects them as "unrecognized arguments".
     args = parse_command_line_constants(parser)
+    ## TODO: Put me somewhere else.
+    os.makedirs(args.output, exist_ok=True)
@@ -76,6 +76,11 @@ def parse_command_line_constants(parser: argparse.ArgumentParser):
     core.NUM_PORT = args.ports
 
+    # Modules such as _network copy these values at import time (`from ._constants import
+    # E_SYNC`), so also update the source module they read from at construction time.
+    from . import _constants
+    _constants.E_SYNC = args.e_sync
+
     return args
--- a/tsnkit/core/_network.py
+++ b/tsnkit/core/_network.py
@@ -19,6 +19,7 @@
 from ._constants import T_SLOT, E_SYNC, NUM_PORT
+from . import _constants
@@ -43,7 +44,7 @@ class Node(int):
-        self._sync_error = E_SYNC
+        self._sync_error = _constants.E_SYNC  # read at call time
@@ -161,7 +162,7 @@ class Link(int):
-        self._t_sync = E_SYNC
+        self._t_sync = _constants.E_SYNC  # read at call time
```

Checked on a 4-switch, 8-flow instance:
- with the patch, `python -m tsnkit.algorithms.smt_wa task.csv topo.csv -e_sync 100` runs and gives a schedule byte-identical to setting the guard band programmatically;
- with no flags, `smt_wa`, `ls` and `dt` produce schedules byte-identical to unpatched TSNKit.

Happy to open a PR instead if that's easier.

### Questions / related

- `E_SYNC` is used in slots (`T_SLOT` = 100 ns), while `t_proc`/`t_prop` in `topo.csv` are in ns. Should `-e_sync` take ns? This is close to your question in #60 about which value wins when `topo.csv` and the flags disagree.
- `simulation/tas.py` copies `T_PROC` at import time in the same way, assumes 1 Gbps (`size * 8`), and doesn't read `topo.csv` yet (#55). I wrote an event-driven simulator that reads per-link rate, `t_proc` and `t_prop` from `topo.csv` and matches `tas.py` frame for frame on uniform topologies (https://github.com/brkuhgk/tsn-qbv-lab). Glad to help port that if it's useful.
