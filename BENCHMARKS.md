# Benchmarks

[日本語](BENCHMARKS.ja.md)

All numbers come from one private codebase (three Python files) measured on 2026-09-25. Each review run was done **once**, so treat differences of a few thousand tokens or a single finding as noise. Raw traces are not published because they contain private function names; the tables below are complete for what was measured.

Files used:

| name here | lines | shape |
|---|---|---|
| CLI | 5,811 | argparse sub-commands dispatched with `set_defaults(func=...)` |
| patch-apply script | 3,351 | one large class, a 1,250-line self-test function |
| job runner | 10,226 | 230 functions, one `main` |

## 1. Does the static map match real calls?

Real calls were recorded with `trace_calls.py` (`sys.settrace`) while running each file's own self-test. Only calls from a function in the file to a function in the same file are counted; lambdas, comprehensions, and module-level code are excluded.

| file | version of the tool | real pairs | matched | static-only pairs |
|---|---|---|---|---|
| patch-apply script | calls + known-type receivers only | 89 | 62 (69%) | 3 |
| patch-apply script | current (all edge kinds) | 76 | 71 (93%) | 8 |
| CLI | current (all edge kinds) | 183 | 182 (99%) | 56 |

The earlier version missed constructor calls (`Class(...)` → `__init__`), functions defined inside functions, and functions stored in variables. "Static-only" pairs are mostly paths the self-test never ran.

## 2. How far does `main` reach, going only downward?

`--names main --direction down --layers --metric code` (code lines, blank and comment lines excluded). Share of the file added at each layer:

| file | L0 | L1 | L2 | L3 | L4 | L5 | L6 | total | saturates at |
|---|---|---|---|---|---|---|---|---|---|
| patch-apply script | 2% | 42% | 34% | 17% | 1% | | | 95% | 4 |
| job runner | 1% | 4% | 27% | 38% | 14% | 4% | 1% | 90% | 6 |
| CLI (before `dispatch` edges) | 0% | 2% | | | | | | 2% | 1 |
| CLI (current) | 0% | 2% | 50% | 34% | 8% | 0.3% | | 95% | 5 |

Programs look like a pyramid from the top: the entry reaches 90–95% within 4–6 layers, and the middle layers are the thickest (more like a diamond). A CLI that picks its handler from a table looked unreachable until the table itself became an edge.

Mixing directions (callers of callers, then their callees) makes almost everything look related within a few layers; that is why callers and callees are walked separately.

## 3. How much code does a reviewer need?

One change (36-line diff) to the CLI file. The same instructions each time; only the attached code changed. Reviewer: a mid-tier coding model at medium reasoning effort, read-only, no tools.

| attached | lines | input tokens | output tokens | verdict | main findings |
|---|---|---|---|---|---|
| relevance selection (budget 10%) | 418 | 25,058 | 675 | PASS | core issue; also a gap in the self-test that the whole-file run missed; marked unseen code as unknown |
| 10% line cap | 606 | 27,345 | 512 | PASS | core issue |
| 20% line cap | 1,194 | 36,957 | 486 | PASS | core issue |
| 30% line cap | 1,788 | 47,872 | 522 | FAIL | same issue, labelled HIGH |
| 40% line cap | 2,376 | 61,018 | 743 | FAIL | same issue, labelled HIGH; self-test gap |
| 50% line cap | 2,933 | 70,713 | 561 | PASS | core issue |
| callers 1 layer | 1,550 | 45,356 | 445 | PASS | core issue |
| callers 2 layers | 4,649 | 108,495 | 632 | PASS | core issue |
| whole file | 5,811 | 105,340 | 594 | PASS | core issue |

- Every run found the same core issue. PASS/FAIL flipped only because the model labelled the same finding MED in some runs and HIGH in others; this did not follow the amount of code.
- Slicing by whole layers can balloon when a large caller sits in the next layer (2 layers ≈ the whole file here). Relevance selection trims large functions to the lines that connect to the change.

A second change (to the patch-apply script, reviewed at medium effort) showed the same pattern: whole file 63k input tokens / PASS; slice with callers 49k / PASS. A slice **without** callers could not judge the effect of the change and answered "unknown" — include callers.

## 4. Asking the reviewer which functions it needs

Starting from a deliberately incomplete slice, the reviewer was asked to name missing functions (`NEED: name`), which were then added for a second pass.

| setup | pass 1 | pass 2 | total input |
|---|---|---|---|
| no table of contents | named 3 functions, 1 did not exist | FAIL, asked for another non-existent function | ~91k |
| with a table of contents (`--toc`) | named 1 real function | FAIL (misread code it had been given) | ~91k |

Two passes cost more than one whole-file pass (63k). Give the reviewer a relevance slice with callers in one pass; use `NEED` only as a fallback, always with a table of contents.

## Reproduce on your own code

```sh
# 1. record real calls while running your tests
python3 trace_calls.py --file app.py -- <arguments your file needs to run its tests> > real.tsv
# 2. compare with the static map
python3 slice_funcs.py --file app.py --compare-trace real.tsv
# 3. how far main reaches
python3 slice_funcs.py --file app.py --names main --direction down --layers --metric code
# 4. slices of different sizes for a review
python3 slice_funcs.py --file app.py --diff change.diff --select relevance --budget 10% --git-root . --explain
python3 slice_funcs.py --file app.py --diff change.diff --max-lines 600
```
