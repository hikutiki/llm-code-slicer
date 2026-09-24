# llm-code-slicer — feed an AI only the code that matters

[日本語](README.ja.md)

When you ask an AI model to review or fix a change, you usually paste the whole file. For a 5,000-line file that is 100k tokens per round — most of it unrelated to the change. `slice_funcs.py` reads a Python file, builds a map of how its functions connect, starts from the lines you changed, and cuts out only the connected code, with line numbers, ready to paste.

- Pure Python 3.9+, standard library only (`ast`). No install, no network.
- Works on a single file. Output is plain `path:start-end` ranges, or a table of contents, or the edge list.
- Pluggable: each kind of connection is a small plug-in; add one to support a new pattern.

## Relevance selection

With `--select relevance`, every connected function gets a score from pluggable scorers, and the highest scores are added until the budget is used. The changed functions are always included. Large functions are trimmed to the lines that connect to the change (plus their `def` and docstring).

| scorer | higher when |
|---|---|
| `distance` | closer to the changed function (callers and callees counted separately) |
| `edge_kind` | connected by a direct call rather than a mere reference |
| `shared_state` | it reads or writes the same `self.x`, module variables, or constants |
| `diff_terms` | identifiers and strings from the changed lines appear in it |
| `cochange` | git history shows it changing in the same commits (`--git-root`) |

Weights can be changed with `--weights name=value,...`, and a new scorer is one small plug-in.

## What it follows

| kind | example |
|---|---|
| `call` | `f()`, `self.f()`, `cls.f()`, `Class.f()` |
| `ctor` | `Class(...)` → `Class.__init__` |
| `typed_attr` | `x = Runner(); x.run()` (only when the type is known) |
| `ref` | a function passed as a value: callbacks, `{"name": handler}` |
| `alias` | `cmd_sol = cmd_sol_r3` (resolved to the last assignment) |
| `dispatch` | argparse `set_defaults(func=cmd_x)` → `args.func(args)` |
| `nested` | a function defined inside another |
| `decorator` | decorator → decorated function |

Calls into imported modules (`subprocess.run`, `os.path...`) and receivers of unknown type are ignored on purpose, so `obj.run()` does not wrongly connect to your own `run` method.

## Usage

```sh
# Ranges connected to a diff (both directions, callers and callees kept separate)
python3 slice_funcs.py --file app.py --diff change.diff

# Walk down from main and show how far each layer reaches
python3 slice_funcs.py --file app.py --names main --direction down --layers --metric code

# Cap the slice at 10% of the file
python3 slice_funcs.py --file app.py --diff change.diff --max-lines 600

# Pick the most relevant code first, within a 10% budget, and explain the scores
python3 slice_funcs.py --file app.py --diff change.diff --select relevance --budget 10% --git-root . --explain

# Check the static map against real calls recorded while running your tests
python3 trace_calls.py --file app.py -- --self-test > real.tsv
python3 slice_funcs.py --file app.py --compare-trace real.tsv
```

## Measured results (single runs, one codebase; treat as indicative — full tables in [BENCHMARKS.md](BENCHMARKS.md))

**Does the static map match reality?** Real calls were recorded with `sys.settrace` while running each file's self-test.

| file | lines | real call pairs matched |
|---|---|---|
| a CLI with argparse dispatch | 5,811 | 99% (182 / 183) |
| a patch-apply script | 3,351 | 93% (71 / 76) |

**Is a program a pyramid?** Walking only downward from `main` reaches most of the code within a few layers; the middle layers are the thickest.

| file | lines | reached from `main` | layers until saturation |
|---|---|---|---|
| job runner | 10,226 | 90% | 6 |
| CLI | 5,811 | 95% | 5 |
| patch-apply script | 3,351 | 95% | 4 |

**How much code does a reviewer need?** Same 36-line change, same instructions, reviewed by a mid-size model (medium effort), with different amounts of code attached:

| attached | lines | input tokens | verdict | key findings |
|---|---|---|---|---|
| relevance selection | 418 (7%) | 25k | PASS | same core finding, plus one the whole-file run missed |
| 10% slice (line cap) | 606 | 27k | PASS | same core finding |
| 20% slice (line cap) | 1,194 | 37k | PASS | same core finding |
| whole file | 5,811 | 105k | PASS | same core finding |

Relevance selection found the key issue with about a quarter of the whole-file input, and marked code it had not been shown as "unknown" instead of guessing. Differences in severity labels between runs came from the model, not from the amount of code.

## Also included: apply_candidate.py

A companion script for the other end of the loop: once a human has approved a change, [`apply_candidate.py`](APPLY_CANDIDATE.md) applies it safely — verify the receipt and hashes, back up, replace, run the tests in isolation, watch for leftover processes and unexpected file changes, and roll back if anything fails. When it refuses, it lists every problem at once instead of one per run. Environment-specific values (approvers, folders, watched paths, restart command) live in a config file; see `examples/legacy-layout.apply-candidate.json`. Run `python3 apply_candidate.py --self-test`.

## Known limits

- Dynamic calls (functions stored in variables and called later, `getattr`) are only partly visible.
- A single huge function (1,000+ lines) touched by the change pulls in a lot by itself.
- One file at a time.

## Tests

```sh
python3 -m unittest discover -s . -p 'test_*.py'
```

## License

MIT
