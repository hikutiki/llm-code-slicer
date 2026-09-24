# apply_candidate.py

`apply_candidate.py` safely applies an already-approved candidate change to a repository. It is designed for macOS, Python 3.9+, and the Python standard library only.

## Flow

1. Load configuration from `--config PATH`, otherwise `<repo>/.apply-candidate.json`, otherwise repository-agnostic defaults.
2. Preflight the receipt, manifest, IDs, approval metadata, paths, candidate hashes, and current preimage hashes. Independent preflight findings are returned together where the existing validation model permits it.
3. Save preimages and staged candidate copies under the configured evidence directory.
4. Re-check the real target immediately before replacement, reject symlinks, and replace the approved paths.
5. Run each `--test` in isolation using a Python executable only, with timeout/process-group cleanup and process/file monitoring.
6. If tests fail or application fails, roll back from the saved preimages. If tests pass, optionally run the configured restart command, perform post-apply monitoring, write verification evidence, update the project note, and emit flags/order files when needed.

`--restart-listener` is retained for compatibility. It now means “run `restart_command` from configuration”. If `restart_command` is empty, restart is disabled. `{uid}` and `{repo}` may be used as placeholders in restart command arguments.

## CLI

```text
python3 orchestration/claude/bin/apply_candidate.py \
  --repo /path/to/repo \
  --config /path/to/config.json \
  --receipt /path/to/repo/.apply-candidate/receipts/APR-001.json \
  --test 'python3::-m unittest tests.test_example'
```

`--config` is optional. `--repo` defaults to the current directory. `--self-test` is fully self-contained: it does not load the packaged example, and it runs the same built-in checks once with repository-agnostic defaults and once with an embedded neutral legacy-style layout. A comparison with `apply_candidate.orig.py` remains optional and is reported as SKIP when that file is absent.

## Receipt format

The receipt is a JSON object stored under the configured `receipts_dir`. Hash values below are illustrative placeholders.

```json
{
  "approval_id": "APR-001",
  "project_id": "PROJECT-001",
  "approved_at": "2026-09-25T00:00:00+00:00",
  "expires_at": "2026-09-25T02:00:00+00:00",
  "approver_identity": "reviewer-a",
  "explicit_approval_quote": "approved",
  "candidate_sha256": "<sha256-of-manifest>",
  "candidate_path": ".apply-candidate/candidates/PROJECT-001--manifest.json",
  "allowed_paths": ["src/example.py"],
  "audit_status": "pass"
}
```

If `approvers` is empty, `approver_identity` is not name-filtered. `explicit_approval_quote`, expiry, `audit_status`, manifest hash, allowed paths, and the remaining safety checks still apply.

## Manifest format

```json
{
  "project_id": "PROJECT-001",
  "verification": ["reports/test-baseline.log"],
  "files": [
    {
      "path": "src/example.py",
      "candidate_path": ".apply-candidate/candidates/PROJECT-001--example.py",
      "preimage_sha256": "<sha256-of-current-file>",
      "postimage_sha256": "<sha256-of-candidate-file>"
    },
    {
      "path": "src/new_file.py",
      "candidate_path": ".apply-candidate/candidates/PROJECT-001--new_file.py",
      "preimage_sha256": null,
      "postimage_sha256": "<sha256-of-candidate-file>"
    }
  ]
}
```

`preimage_sha256: null` means the target must not already exist. Candidate files and the manifest must resolve inside the configured candidates directory and must not traverse symlinks.

## Configuration

All paths are repository-relative. `candidates_dir`, `receipts_dir`, `evidence_dir`, and `notes_dir` are interpreted under `workspace_dir`. Unknown keys produce a warning and are ignored. A wrong value type, invalid regular expression, escaping path, or invalid structure is refused with exit code 3.

| Key | Type | Repository-agnostic default | Meaning |
|---|---|---|---|
| `approvers` | array of strings | `[]` | Allowed `approver_identity` values. Empty disables name checking. |
| `workspace_dir` | string | `.apply-candidate` | Base directory for candidate workflow data. |
| `candidates_dir` | string | `candidates` | Candidate/manifest directory below `workspace_dir`. |
| `receipts_dir` | string | `receipts` | Receipt directory below `workspace_dir`. |
| `evidence_dir` | string | `evidence` | Evidence, backup, staged, logs, flags, and verification directory below `workspace_dir`. |
| `notes_dir` | string | `notes` | Project-note directory below `workspace_dir`; `.` means the workspace root. |
| `project_id_pattern` | string regex | generic safe ID regex | Full-match regex for project IDs. |
| `approval_id_pattern` | string regex | generic safe ID regex | Full-match regex for approval IDs. |
| `watch_dirs` | array of strings | `[]` | Extra repository-relative directories monitored in addition to parents of `allowed_paths`. |
| `ignore_files` | array of `[dir, pattern]` | `[]` | Direct-child file patterns ignored by file monitoring. An `allowed_paths` entry is never ignored. |
| `restart_command` | argv array | `[]` | Command executed when `--restart-listener` is supplied. Empty disables restart. Supports `{uid}` and `{repo}`. |
| `restart_check_log` | string or `null` | `null` | Optional repository-relative log. Newly appended `ERROR`/`Traceback` content is flagged after restart. |
| `os_process_names` | array of strings | current macOS Spotlight names | System process names excluded from orphan-process warnings only when the executable is under `/System/Library/`. |
| `flags_order_dir` | string | `.apply-candidate/orders` | Destination for `flags-<project_id>.md`. |

A neutral legacy-layout example is provided at `examples/legacy-layout.apply-candidate.json`. It uses `work/pending-changes`, reviewer `Alice`, `app/runtime` and `.claude` watch roots, a `service-*.log` exclusion under `app/runtime`, and the harmless restart command `["true"]`.

## Safety properties retained

- Manifest/receipt and allowed-path consistency checks.
- SHA-256 preimage and candidate checks.
- Immediate pre-replacement hash verification.
- Repository-bound relative-path validation and symlink rejection.
- Preimage backup and staged candidate evidence.
- Python-only isolated test execution, timeouts, process-group cleanup, and descendant/orphan process monitoring.
- Git-status and direct filesystem monitoring, including ignored-file exceptions that cannot override `allowed_paths`.
- Post-test detection of writes to approved targets.
- Rollback on application/test failure and explicit `ROLLBACK_FAILED` reporting when restoration cannot complete.
- Aggregation of independent preflight findings rather than hiding later findings behind the first non-fatal mismatch.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | `APPLIED` or `DRY_RUN_OK` |
| `1` | CLI/input error before the apply workflow |
| `2` | `ROLLED_BACK` |
| `3` | `REFUSED`, including invalid configuration |
| `5` | `ROLLBACK_FAILED` |
| `6` | unhandled `ERROR` |
| `10` | `APPLIED_WITH_FLAGS` |

## Known limitations

The design is cooperative rather than adversarial. It does not attempt to defend against a malicious local process intentionally racing the tool by replacing repository directories/files, candidate/evidence locations, or test inputs at carefully chosen instants. The monitoring and hash checks are intended to catch accidental or ordinary unexpected changes, not hostile same-host interference.

The launchd PID check is retained only when `restart_command` has the legacy `launchctl kickstart ... <label>` form. Other restart commands are checked by their exit code plus optional `restart_check_log` growth.
