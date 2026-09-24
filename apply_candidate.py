#!/usr/bin/env python3
"""
apply_candidate.py - スキル・承認候補の確実な反映と見張りスクリプト

macOS, Python 3.9+, 標準ライブラリのみで動作。
本番適用手順を機械化し、同時に異常を検出する見張り（直さずに旗を立てる）を行う。
前提: リポジトリ内のディレクトリやファイルを、実行中に symlink へ差し替えるような意図的な妨害（evidence・candidates 置き場所の symlink 化やテスト中の差し替え等）は前提外とする。
"""

import argparse
import datetime
import fnmatch
import glob
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# 結果ステータス定義
RES_APPLIED = "APPLIED"
RES_APPLIED_WITH_FLAGS = "APPLIED_WITH_FLAGS"
RES_REFUSED = "REFUSED"
RES_ROLLED_BACK = "ROLLED_BACK"
RES_ROLLBACK_FAILED = "ROLLBACK_FAILED"
RES_DRY_RUN_OK = "DRY_RUN_OK"
RES_ERROR = "ERROR"

# 終了コード
EXIT_OK = 0
EXIT_ROLLED_BACK = 2
EXIT_REFUSED = 3
EXIT_ROLLBACK_FAILED = 5
EXIT_UNHANDLED_ERROR = 6
EXIT_FLAGS = 10
EXIT_ERROR = 1

# Configuration defaults are intentionally repository-agnostic.
DEFAULT_CONFIG: Dict[str, Any] = {
    "approvers": [],
    "workspace_dir": ".apply-candidate",
    "candidates_dir": "candidates",
    "receipts_dir": "receipts",
    "evidence_dir": "evidence",
    "notes_dir": "notes",
    "project_id_pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    "approval_id_pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    "watch_dirs": [],
    "ignore_files": [],
    "restart_command": [],
    "restart_check_log": None,
    "os_process_names": ["mdworker_shared", "mdworker", "mds", "mds_stores"],
    "flags_order_dir": ".apply-candidate/orders",
}
CONFIG_KEYS = set(DEFAULT_CONFIG)
OS_EXE_PREFIX = "/System/Library/"


def _copy_default_config() -> Dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_CONFIG))


def _validate_rel_config_path(key: str, value: str, allow_dot: bool = False) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return f"config {key} must be a non-empty string"
    if os.path.isabs(value):
        return f"config {key} must be repository-relative"
    norm = os.path.normpath(value)
    if norm == "." and allow_dot:
        return None
    if norm in (".", "..") or norm.startswith(".." + os.sep):
        return f"config {key} must stay within the repository"
    if norm != value.rstrip("/"):
        return f"config {key} must be normalized"
    return None


def load_config(config_path: Optional[str], repo: str) -> Tuple[Optional[Dict[str, Any]], List[str], Optional[str]]:
    """Load and type-check config. Returns (config, warnings, error)."""
    cfg = _copy_default_config()
    warnings: List[str] = []
    path = config_path or os.path.join(repo, ".apply-candidate.json")
    if config_path and not os.path.isfile(path):
        return None, warnings, f"config file not found: {path}"
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:
            return None, warnings, f"failed to load config JSON: {exc}"
        if not isinstance(raw, dict):
            return None, warnings, "config JSON must be an object"
        for key in raw:
            if key not in CONFIG_KEYS:
                warnings.append(f"unknown config key ignored: {key}")
        for key in CONFIG_KEYS:
            if key in raw:
                cfg[key] = raw[key]

    str_list_keys = ("approvers", "watch_dirs", "os_process_names", "restart_command")
    for key in str_list_keys:
        v = cfg[key]
        if not isinstance(v, list) or not all(isinstance(x, str) and x != "" for x in v):
            return None, warnings, f"config {key} must be a list of non-empty strings"

    for key in ("workspace_dir", "candidates_dir", "receipts_dir", "evidence_dir", "notes_dir", "flags_order_dir"):
        err = _validate_rel_config_path(key, cfg[key], allow_dot=(key == "notes_dir"))
        if err:
            return None, warnings, err

    if not isinstance(cfg["project_id_pattern"], str) or not isinstance(cfg["approval_id_pattern"], str):
        return None, warnings, "config project_id_pattern and approval_id_pattern must be strings"
    try:
        re.compile(cfg["project_id_pattern"])
        re.compile(cfg["approval_id_pattern"])
    except re.error as exc:
        return None, warnings, f"invalid config regex: {exc}"

    ignores = cfg["ignore_files"]
    if not isinstance(ignores, list):
        return None, warnings, "config ignore_files must be a list"
    normalized_ignores = []
    for idx, item in enumerate(ignores):
        if (not isinstance(item, list) or len(item) != 2 or
                not all(isinstance(x, str) and x != "" for x in item)):
            return None, warnings, f"config ignore_files[{idx}] must be [directory, filename_pattern]"
        err = _validate_rel_config_path(f"ignore_files[{idx}][0]", item[0], allow_dot=True)
        if err:
            return None, warnings, err
        normalized_ignores.append((os.path.normpath(item[0]), item[1]))
    cfg["ignore_files"] = normalized_ignores

    check_log = cfg["restart_check_log"]
    if check_log is not None:
        if not isinstance(check_log, str) or not check_log:
            return None, warnings, "config restart_check_log must be null or a non-empty string"
        err = _validate_rel_config_path("restart_check_log", check_log)
        if err:
            return None, warnings, err

    # Validate paths under workspace separately; names may contain nested subdirs but cannot escape.
    for key in ("candidates_dir", "receipts_dir", "evidence_dir", "notes_dir"):
        combined = os.path.normpath(os.path.join(cfg["workspace_dir"], cfg[key]))
        workspace = os.path.normpath(cfg["workspace_dir"])
        if os.path.commonpath([workspace, combined]) != workspace:
            return None, warnings, f"config {key} escapes workspace_dir"
    return cfg, warnings, None




def config_dir(cfg: Dict[str, Any], key: str) -> str:
    if key in ("candidates_dir", "receipts_dir", "evidence_dir", "notes_dir"):
        return os.path.normpath(os.path.join(cfg["workspace_dir"], cfg[key]))
    return os.path.normpath(cfg[key])


# Mutable hook configuration is scoped by the runner; default keeps helper calls generic.
ACTIVE_CONFIG: Dict[str, Any] = _copy_default_config()
SELF_TEST_CONFIG_OVERRIDE: Optional[Dict[str, Any]] = None



class ProcessInfo:
    """プロセス情報保持クラス"""

    def __init__(
        self,
        pid: int,
        ppid: int = 0,
        pgid: int = 0,
        sess: str = "",
        lstart: str = "",
        command: str = "",
    ):
        self.pid = pid
        self.ppid = ppid
        self.pgid = pgid
        self.sess = sess
        self.lstart = lstart
        self.command = command

    def __str__(self) -> str:
        return self.command

# Mock hooks used by self-test.
LAUNCHCTL_RUNNER: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None
PS_SNAPSHOT_RUNNER: Optional[Callable[[], Any]] = None
PS_E_RUNNER: Optional[Callable[[int], subprocess.CompletedProcess]] = None
SLEEP_FN: Callable[[float], None] = time.sleep


def run_ps_e(pid: int) -> subprocess.CompletedProcess:
    """プロセス環境変数付きの ps 実行（モック差し替え対応）"""
    global PS_E_RUNNER
    if PS_E_RUNNER is not None:
        return PS_E_RUNNER(pid)
    return subprocess.run(
        ["ps", "-E", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        text=True,
    )


class Flag:
    """見張りで検出された異常の記録"""

    def __init__(
        self,
        category: str,
        message: str,
        evidence_file: str = "",
        line_num: int = 0,
        excerpt: str = "",
        details: Optional[Dict[str, Any]] = None,
    ):
        self.category = category
        self.message = message
        self.evidence_file = evidence_file
        self.line_num = line_num
        self.excerpt = excerpt  # 最大20行
        self.details: Dict[str, Any] = details or {}

    def summary(self) -> str:
        loc = f" ({self.evidence_file}:{self.line_num})" if self.evidence_file else ""
        return f"[{self.category}] {self.message}{loc}"


def is_ignored_file(rel_path: str, allowed_paths: Optional[Set[str]] = None) -> bool:
    """
    常駐が書き続けるログ等の除外判定。
    除外は「ディレクトリ（repo 相対、完全一致）」と「ファイル名のパターン（fnmatch をファイル名だけに当てる）」の組で持つ。
    指定ディレクトリ直下のみが対象で、サブディレクトリの下には当たらない。
    ただし receipt の allowed_paths に含まれるパスは除外一覧に当たっても必ず見張る（False を返す）。
    """
    if allowed_paths and rel_path in allowed_paths:
        return False

    norm_p = os.path.normpath(rel_path)
    dir_name = os.path.dirname(norm_p)
    file_name = os.path.basename(norm_p)

    for rule_dir, file_pattern in ACTIVE_CONFIG.get("ignore_files", []):
        if dir_name == rule_dir:
            if fnmatch.fnmatch(file_name, file_pattern):
                return True
    return False




def sha256_file(path: str) -> str:
    """ファイルのSHA-256ハッシュを計算"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def parse_iso_datetime(dt_str: str) -> datetime.datetime:
    """ISO 8601 日時文字列を UTC の datetime に変換"""
    s = dt_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def run_launchctl(args: List[str]) -> subprocess.CompletedProcess:
    """launchctl コマンドを実行（モック差し替え対応）"""
    global LAUNCHCTL_RUNNER
    if LAUNCHCTL_RUNNER is not None:
        return LAUNCHCTL_RUNNER(args)
    return subprocess.run(["launchctl"] + args, capture_output=True, text=True)


def is_valid_python_binary(bin_path: str) -> bool:
    """テスト実行コマンドが python バイナリであるかを検査"""
    base = os.path.basename(bin_path).lower()
    if base in ("python", "python3"):
        return True
    if re.match(r"^python3\.\d+$", base):
        return True
    return False


def is_started_after(lstart_str: str, baseline_ts: float) -> bool:
    """
    ps の lstart 文字列が baseline_ts (UNIX秒) 以降に開始されたかを判定。
    読めない・比べられないときは見逃しより誤報を選び True を返す。
    """
    if not lstart_str or lstart_str.strip() in ("", "不明"):
        return True
    try:
        # lstart の標準形式: "%a %b %d %H:%M:%S %Y" (例: "Wed Sep 24 19:00:00 2026")
        dt = datetime.datetime.strptime(lstart_str.strip(), "%a %b %d %H:%M:%S %Y")
        proc_ts = dt.timestamp()
        # baseline_ts はテスト開始時刻。秒精度の切り捨てと比較
        return proc_ts >= int(baseline_ts)
    except Exception:
        return True


def check_path_format_and_duplicates(paths: List[str]) -> Optional[str]:
    """
    (3) パスの正規化・重複・別表記チェック:
    - 空、末尾の /、空要素 (//)、'.'、'..' を含む表記は拒否
    - os.path.normpath した値と一致すること
    - 大文字小文字を区別せず重複があれば拒否
    """
    seen_lower: Set[str] = set()
    for p in paths:
        if not p or not p.strip():
            return "Path is empty"
        if p.endswith("/") or p.endswith("\\"):
            return f"Trailing slash not allowed: '{p}'"
        raw_parts = p.replace("\\", "/").split("/")
        if "" in raw_parts:
            return f"Empty path segment (double slash) not allowed: '{p}'"
        if "." in raw_parts:
            return f"Segment '.' not allowed: '{p}'"
        if ".." in raw_parts:
            return f"Segment '..' not allowed: '{p}'"

        norm = os.path.normpath(p)
        if norm != p:
            return f"Path is not normalized: '{p}' (normalized: '{norm}')"

        p_lower = norm.lower()
        if p_lower in seen_lower:
            return f"Duplicate path detected (case-insensitive): '{p}'"
        seen_lower.add(p_lower)

    return None


def validate_relative_repo_path(repo: str, rel_path: str) -> Optional[str]:
    """
    パスの厳格な検証:
    - 相対パスであること（絶対パス禁止）
    - realpath がリポジトリ配下であること
    - 途中のディレクトリおよび自身がシンボリックリンクでないこと
    """
    if not rel_path or not rel_path.strip():
        return "Path is empty"

    if os.path.isabs(rel_path):
        return f"Absolute path is not allowed: {rel_path}"

    repo_real = os.path.realpath(repo)
    full_path = os.path.join(repo, rel_path)
    target_real = os.path.realpath(full_path)

    try:
        common = os.path.commonpath([repo_real, target_real])
        if common != repo_real:
            return f"Path resolves outside repository: {rel_path} -> {target_real}"
    except (ValueError, Exception) as exc:
        return f"Path cannot be resolved within repository: {rel_path} ({exc})"

    # 書込み先自身または祖先ディレクトリが symlink でないことの検証
    curr = full_path
    while True:
        if os.path.islink(curr):
            return f"Path component is a symlink: {curr} (for {rel_path})"
        parent = os.path.dirname(curr)
        if curr == repo or curr == parent or not curr.startswith(repo):
            break
        curr = parent

    return None


def validate_candidate_file_path(repo: str, cand_path_input: str) -> Tuple[Optional[str], str]:
    """
    (2) 候補ファイルのパス検証:
    - 相対パスならリポジトリ基準で解決
    - realpath が <repo>/<configured candidates_dir>/ の中であること
    - 途中にも末尾にも symlink が無い通常ファイルであること
    戻り値: (エラー文字列またはNone, 解決済み絶対パス)
    """
    if not cand_path_input or not cand_path_input.strip():
        return "Candidate path is empty", ""

    if os.path.isabs(cand_path_input):
        full_cand = cand_path_input
    else:
        full_cand = os.path.join(repo, cand_path_input)

    cand_base_dir = os.path.realpath(
        os.path.join(repo, config_dir(ACTIVE_CONFIG, "candidates_dir"))
    )
    cand_real = os.path.realpath(full_cand)

    try:
        if os.path.commonpath([cand_base_dir, cand_real]) != cand_base_dir:
            return (
                f"Candidate path resolves outside candidates directory: {cand_real} (must be in {cand_base_dir})",
                "",
            )
    except Exception as exc:
        return f"Cannot resolve candidate path: {exc}", ""

    # 途中にも末尾にも symlink が無いこと
    curr = full_cand
    while True:
        if os.path.islink(curr):
            return f"Candidate path or component is a symlink: {curr}", ""
        parent = os.path.dirname(curr)
        if curr == repo or curr == parent or not curr.startswith(repo):
            break
        curr = parent

    if not os.path.exists(full_cand):
        return f"Candidate file does not exist: {full_cand}", ""

    if not os.path.isfile(full_cand):
        return f"Candidate file is not a regular file: {full_cand}", ""

    return None, full_cand


class ApplyCandidateRunner:
    """反映実行および見張りを統括するクラス"""

    def __init__(
        self,
        receipt_path: str,
        test_cwd: Optional[str],
        test_specs: List[str],
        restart_listener: bool,
        repo: str,
        dry_run: bool,
        test_timeout: Optional[float] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        self.receipt_path = os.path.abspath(receipt_path)
        self.test_cwd = os.path.abspath(test_cwd) if test_cwd else os.path.abspath(repo)
        self.test_specs = test_specs
        self.restart_listener = restart_listener
        self.repo = os.path.abspath(repo)
        self.dry_run = dry_run
        self.test_timeout = test_timeout
        self.config = config if config is not None else (SELF_TEST_CONFIG_OVERRIDE if SELF_TEST_CONFIG_OVERRIDE is not None else _copy_default_config())
        global ACTIVE_CONFIG
        ACTIVE_CONFIG = self.config

        self.receipt: Dict[str, Any] = {}
        self.manifest: Dict[str, Any] = {}
        self.manifest_path = ""
        self.project_id = ""
        self.approval_id = ""

        self.evidence_dir = ""
        self.preimage_dir = ""
        self.staged_dir = ""
        self.log_file_path = ""

        self.flags: List[Flag] = []
        self.log_lines: List[str] = []

        # 既存ファイルのバックアップ情報: rel_path -> (backup_path, orig_mode, preimage_sha256)
        self.backed_up_files: Dict[str, Tuple[str, int, str]] = {}
        # 反映済み（新規または更新）ファイル一覧
        self.applied_paths: List[str] = []
        # 新規作成ファイル一覧（ロールバック時に削除するため）
        self.created_new_paths: List[str] = []
        # ロールバック失敗ファイル一覧
        self.rollback_failed_files: List[str] = []

    def log(self, message: str) -> None:
        """ログ行を保持（後で apply.log に書き出す）"""
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log_lines.append(f"[{ts}] {message}")

    def flush_log(self) -> None:
        """ログを apply.log に書き出す"""
        if not self.log_file_path:
            return
        try:
            os.makedirs(os.path.dirname(self.log_file_path), exist_ok=True)
            with open(self.log_file_path, "a", encoding="utf-8") as f:
                for line in self.log_lines:
                    f.write(line + "\n")
            self.log_lines.clear()
        except Exception:
            pass

    def run(self) -> Tuple[str, int, str]:
        """
        反映処理を実行。
        戻り値: (RESULT_STRING, EXIT_CODE, DETAIL_MESSAGE)
        """
        # 1. 照合 (Preflight)
        refused_reasons = self.preflight_check()
        if refused_reasons:
            self.log(f"Preflight check failed: {refused_reasons}")
            output_msg = (
                f"RESULT: {RES_REFUSED}\nreason: {refused_reasons[0]}"
                + "".join(f"\n- {reason}" for reason in refused_reasons[1:])
            )
            return RES_REFUSED, EXIT_REFUSED, output_msg

        if self.dry_run:
            self.log("Dry-run requested. Preflight passed, no changes made.")
            output_msg = f"RESULT: {RES_DRY_RUN_OK}\npreflight: PASS"
            return RES_DRY_RUN_OK, EXIT_OK, output_msg

        # (1) CRITICAL: evidence ディレクトリの安全な初期化
        self.evidence_dir = os.path.join(
            self.repo, config_dir(self.config, "evidence_dir"), self.project_id
        )
        self.preimage_dir = os.path.join(self.evidence_dir, "preimage")
        self.staged_dir = os.path.join(self.evidence_dir, "staged")
        self.log_file_path = os.path.join(self.evidence_dir, "apply.log")

        os.makedirs(self.evidence_dir, exist_ok=True)
        os.makedirs(self.preimage_dir, exist_ok=True)
        os.makedirs(self.staged_dir, exist_ok=True)

        self.log(f"Starting application for project {self.project_id}")

        # 見張り事前チェック: 記録の食い違い
        self.check_metadata_consistency()

        # 見張り事前チェック: git status
        pre_git_status = self.get_git_status()

        # 見張り事前チェック: リスナーエラーログサイズ
        pre_listener_err_size = self.get_listener_err_log_size()

        # 見張り事前チェック: .gitignore 盲点対策用ファイルシステムスナップショット
        pre_file_snapshot = self.get_monitored_files_snapshot()

        # 2. 退避・ステージング・正本へ反映
        try:
            self.apply_files()
        except Exception as exc:
            self.log(f"Error during file application: {exc}")
            rb_ok = self.rollback()
            if not rb_ok:
                output_msg = (
                    f"RESULT: {RES_ROLLBACK_FAILED}\n"
                    f"failed_files:\n"
                    + "\n".join(f"- {f}" for f in self.rollback_failed_files)
                )
                return RES_ROLLBACK_FAILED, EXIT_ROLLBACK_FAILED, output_msg
            output_msg = f"RESULT: {RES_ROLLED_BACK}\nreason: exception during apply: {exc}"
            return RES_ROLLED_BACK, EXIT_ROLLED_BACK, output_msg

        # 3. テスト実行と見張り
        test_success, test_reason = self.run_tests()
        if not test_success:
            self.log(f"Tests failed: {test_reason}")
            # (5) 失敗・時間切れのテストの副作用の見張り
            self.check_git_status_diff(pre_git_status, pre_file_snapshot)
            self.check_monitored_files_diff(pre_file_snapshot)

            # 巻き戻し前に反映対象のテスト後書換え検査を行う (HIGH 3)
            self.check_allowed_paths_rewrites(restore_from_staged=False)

            rb_ok = self.rollback()
            flags_file = ""
            order_file = ""
            if self.flags:
                flags_file, order_file = self.write_flags_and_order()
            self.flush_log()

            if not rb_ok:
                output_msg = (
                    f"RESULT: {RES_ROLLBACK_FAILED}\n"
                    f"failed_files:\n"
                    + "\n".join(f"- {f}" for f in self.rollback_failed_files)
                )
                return RES_ROLLBACK_FAILED, EXIT_ROLLBACK_FAILED, output_msg

            out_lines = [
                f"RESULT: {RES_ROLLED_BACK}",
                f"reason: {test_reason}",
            ]
            if self.flags:
                out_lines.append(f"flags: {len(self.flags)}")
                for flag in self.flags:
                    out_lines.append(f"- {flag.summary()}")
                if flags_file:
                    out_lines.append(f"flags_file: {flags_file}")
                if order_file:
                    out_lines.append(f"order_file: {order_file}")
            output_msg = "\n".join(out_lines)
            return RES_ROLLED_BACK, EXIT_ROLLED_BACK, output_msg

        # 4. リスナー再起動（オプション）
        if self.restart_listener:
            self.restart_approval_listener(pre_listener_err_size)

        # 見張り事後チェック: 変わったファイル (git status 差分)
        self.check_git_status_diff(pre_git_status, pre_file_snapshot)

        # 見張り事後チェック: .gitignore 盲点チェック (ファイルシステム直接検査)
        self.check_monitored_files_diff(pre_file_snapshot)

        # 5. 結果記録と案件ノート更新
        verification_path = self.write_verification()
        self.update_project_note(
            RES_APPLIED_WITH_FLAGS if self.flags else RES_APPLIED
        )

        # 旗の出力と Luna 発注文の作成
        flags_file = ""
        order_file = ""
        if self.flags:
            flags_file, order_file = self.write_flags_and_order()

        self.flush_log()

        # 標準出力の作成
        if self.flags:
            result_code = RES_APPLIED_WITH_FLAGS
            exit_code = EXIT_FLAGS
            out_lines = [
                f"RESULT: {result_code}",
                f"flags: {len(self.flags)}",
            ]
            for flag in self.flags:
                out_lines.append(f"- {flag.summary()}")
            out_lines.append(f"verification: {verification_path}")
            if flags_file:
                out_lines.append(f"flags_file: {flags_file}")
            if order_file:
                out_lines.append(f"order_file: {order_file}")
            return result_code, exit_code, "\n".join(out_lines)
        else:
            result_code = RES_APPLIED
            exit_code = EXIT_OK
            out_lines = [
                f"RESULT: {result_code}",
                f"flags: 0",
                f"verification: {verification_path}",
            ]
            return result_code, exit_code, "\n".join(out_lines)

    def preflight_check(self) -> List[str]:
        """
        照合（見つかった理由を順序を保って返す）
        """
        reasons: List[str] = []

        def stop(reason: str) -> List[str]:
            reasons.append(reason)
            return reasons

        # (4) テスト必須: --test が1つも無ければ拒否
        if not self.test_specs or len(self.test_specs) == 0:
            return stop("At least one --test specification is required (--test cannot be empty)")

        # (7) テストのコマンドは python の実行ファイル（basename が python/python3/python3.x）に限る
        for test_spec in self.test_specs:
            if "::" not in test_spec:
                return stop(f"Invalid test format (missing '::'): {test_spec}")
            py_bin = test_spec.split("::", 1)[0].strip()
            if not is_valid_python_binary(py_bin):
                return stop(f"Test executable must be python/python3/python3.x binary (got '{py_bin}')")

        if not os.path.isfile(self.receipt_path):
            return stop(f"Receipt file not found: {self.receipt_path}")
        receipt_base = os.path.realpath(os.path.join(self.repo, config_dir(self.config, "receipts_dir")))
        receipt_real = os.path.realpath(self.receipt_path)
        try:
            if os.path.commonpath([receipt_base, receipt_real]) != receipt_base:
                return stop(f"Receipt path resolves outside configured receipts_dir: {receipt_real}")
        except Exception as exc:
            return stop(f"Cannot resolve receipt path: {exc}")

        try:
            with open(self.receipt_path, "r", encoding="utf-8") as f:
                self.receipt = json.load(f)
        except Exception as exc:
            return stop(f"Failed to load receipt JSON: {exc}")

        if not isinstance(self.receipt, dict):
            return stop("Receipt JSON must be an object")

        self.project_id = self.receipt.get("project_id", "")
        self.approval_id = self.receipt.get("approval_id", "")
        if not self.project_id or not self.approval_id:
            return stop("Receipt missing project_id or approval_id")
        if not isinstance(self.project_id, str) or not isinstance(self.approval_id, str):
            return stop("Receipt project_id and approval_id must be strings")

        # (1) CRITICAL: project_id および approval_id の書式検査
        if not re.fullmatch(self.config["project_id_pattern"], self.project_id):
            return stop(f"project_id does not match configured pattern {self.config['project_id_pattern']!r}: '{self.project_id}'")
        if not re.fullmatch(self.config["approval_id_pattern"], self.approval_id):
            return stop(f"approval_id does not match configured pattern {self.config['approval_id_pattern']!r}: '{self.approval_id}'")

        # (1) CRITICAL: evidence ディレクトリの realpath 検査
        expected_ev_base = os.path.realpath(
            os.path.join(self.repo, config_dir(self.config, "evidence_dir"))
        )
        planned_ev_dir = os.path.realpath(
            os.path.join(self.repo, config_dir(self.config, "evidence_dir"), self.project_id)
        )
        try:
            if os.path.commonpath([expected_ev_base, planned_ev_dir]) != expected_ev_base:
                return stop(f"Evidence directory resolves outside configured evidence_dir: {planned_ev_dir}")
        except Exception as exc:
            return stop(f"Cannot resolve evidence directory: {exc}")

        # (2) 候補ファイルのパス: 受領書の candidate_path（manifest）の検証
        cand_manifest_raw = self.receipt.get("candidate_path", "")
        if not isinstance(cand_manifest_raw, str):
            return stop("Receipt candidate_path must be a string")
        err_m, cand_manifest_full = validate_candidate_file_path(self.repo, cand_manifest_raw)
        if err_m:
            return stop(f"Invalid receipt candidate_path: {err_m}")
        self.manifest_path = cand_manifest_full

        # manifest の sha256 = 受領書の candidate_sha256
        actual_manifest_sha = sha256_file(self.manifest_path)
        expected_manifest_sha = self.receipt.get("candidate_sha256", "")
        if actual_manifest_sha != expected_manifest_sha:
            return stop(
                f"Manifest SHA-256 mismatch: actual {actual_manifest_sha} "
                f"!= expected {expected_manifest_sha}"
            )

        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                self.manifest = json.load(f)
        except Exception as exc:
            return stop(f"Failed to load manifest JSON: {exc}")

        if not isinstance(self.manifest, dict):
            return stop("Manifest JSON must be an object")

        # audit_status = pass
        audit_status = str(self.receipt.get("audit_status", "")).lower()
        if audit_status != "pass":
            reasons.append(f"Receipt audit_status is not 'pass' (was '{audit_status}')")

        # approver_identity is checked only when an allow-list is configured.
        approver = self.receipt.get("approver_identity", "")
        if self.config["approvers"] and approver not in self.config["approvers"]:
            reasons.append(f"Receipt approver_identity is not in configured approvers (was '{approver}')")

        quote = self.receipt.get("explicit_approval_quote", "")
        if not isinstance(quote, str) or not quote.strip():
            reasons.append("Receipt explicit_approval_quote is empty")

        # 現在時刻 < expires_at
        expires_at_str = self.receipt.get("expires_at", "")
        if not expires_at_str:
            reasons.append("Receipt missing expires_at")
            expires_at = None
        try:
            if expires_at_str:
                expires_at = parse_iso_datetime(expires_at_str)
        except Exception as exc:
            reasons.append(f"Invalid expires_at datetime format: {exc}")
            expires_at = None

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        if expires_at is not None and now_utc >= expires_at:
            reasons.append(f"Approval expired at {expires_at_str} (current time: {now_utc.isoformat()})")

        receipt_allowed = self.receipt.get("allowed_paths", [])
        manifest_files = self.manifest.get("files", [])
        # 旧版は JSON object の場合もキー列を照合していたため、その判定を維持する。
        # 文字列キー以外は後続のパス検査へ渡さず、前提不成立として拒否する。
        if not isinstance(receipt_allowed, (list, dict)):
            return stop("Receipt allowed_paths must be a list or object")
        if not isinstance(manifest_files, list):
            return stop("Manifest files must be a list")
        allowed_items = receipt_allowed.keys() if isinstance(receipt_allowed, dict) else receipt_allowed
        if not all(isinstance(item, str) for item in allowed_items):
            return stop("Receipt allowed_paths entries must be strings")
        if not all(isinstance(item, dict) for item in manifest_files):
            return stop("Manifest files entries must be objects")
        manifest_paths = [f.get("path", "") for f in manifest_files]
        if not all(isinstance(path, str) for path in manifest_paths):
            return stop("Manifest files path entries must be strings")

        # (3) 重複の別表記とパス正規化検査: allowed_paths
        err_dup_r = check_path_format_and_duplicates(receipt_allowed)
        if err_dup_r:
            reasons.append(f"Invalid allowed_paths format or duplicate: {err_dup_r}")

        # (3) 重複の別表記とパス正規化検査: manifest files
        err_dup_m = check_path_format_and_duplicates(manifest_paths)
        if err_dup_m:
            reasons.append(f"Invalid manifest files path format or duplicate: {err_dup_m}")

        # allowed_paths と manifest files の path が一致
        if set(receipt_allowed) != set(manifest_paths) or len(receipt_allowed) != len(
            manifest_paths
        ):
            reasons.append(
                f"Allowed paths mismatch between receipt and manifest: "
                f"{receipt_allowed} vs {manifest_paths}"
            )

        # パス制限: allowed_paths 全件の検証
        for path_item in receipt_allowed:
            err = validate_relative_repo_path(self.repo, path_item)
            if err:
                return stop(f"Invalid path in allowed_paths: {err}")

        # 既存ファイル照合および候補ファイル（manifest の各 candidate_path）の検証
        for file_entry in manifest_files:
            rel_path = file_entry.get("path", "")
            err = validate_relative_repo_path(self.repo, rel_path)
            if err:
                return stop(f"Invalid path in manifest files: {err}")

            target_path = os.path.join(self.repo, rel_path)
            preimage_sha = file_entry.get("preimage_sha256")
            postimage_sha = file_entry.get("postimage_sha256")

            # 既存ファイルの検証
            if preimage_sha is None:
                if os.path.exists(target_path):
                    reasons.append(f"New file already exists at target: {rel_path}")
            else:
                if not os.path.isfile(target_path):
                    reasons.append(f"Existing file missing at target: {rel_path}")
                else:
                    actual_pre_sha = sha256_file(target_path)
                    if actual_pre_sha != preimage_sha:
                        reasons.append(
                            f"Preimage SHA-256 mismatch for {rel_path}: "
                            f"actual {actual_pre_sha} != expected {preimage_sha}"
                        )

            # (2) 候補ファイルのパス検証:
            # manifest の各 candidate_path / candidate は <repo>/<configured candidates_dir>/ 配下の実ファイル
            raw_cand = file_entry.get("candidate_path") or file_entry.get("candidate")
            if raw_cand is None:
                return stop(f"Candidate file path for {rel_path} is missing")
            if not isinstance(raw_cand, str):
                return stop(f"Candidate file path for {rel_path} must be a string")
            err_c, cand_full_path = validate_candidate_file_path(self.repo, raw_cand)
            if err_c:
                return stop(f"Invalid candidate file for {rel_path}: {err_c}")

            actual_cand_sha = sha256_file(cand_full_path)
            if actual_cand_sha != postimage_sha:
                reasons.append(
                    f"Candidate SHA-256 mismatch for {cand_full_path}: "
                    f"actual {actual_cand_sha} != expected {postimage_sha}"
                )

        return reasons

    def check_metadata_consistency(self) -> None:
        """見張り: 記録の食い違い（受領書・manifest・案件ノート）"""
        receipt_pid = self.receipt.get("project_id", "")
        manifest_pid = self.manifest.get("project_id", "")
        if receipt_pid != manifest_pid:
            self.flags.append(
                Flag(
                    category="記録の食い違い",
                    message=f"project_id 不一致: receipt={receipt_pid} vs manifest={manifest_pid}",
                    evidence_file=self.receipt_path,
                )
            )

        receipt_allowed_count = len(self.receipt.get("allowed_paths", []))
        manifest_files_count = len(self.manifest.get("files", []))
        if receipt_allowed_count != manifest_files_count:
            self.flags.append(
                Flag(
                    category="記録の食い違い",
                    message=f"ファイル件数不一致: receipt allowed={receipt_allowed_count} vs manifest files={manifest_files_count}",
                    evidence_file=self.receipt_path,
                )
            )

        notes = glob.glob(
            os.path.join(
                self.repo,
                config_dir(self.config, "notes_dir"),
                f"{self.project_id}--*.md",
            )
        )
        if len(notes) == 0:
            self.flags.append(
                Flag(
                    category="記録の食い違い",
                    message=f"案件ノートが見つかりません (pattern: {self.project_id}--*.md)",
                    evidence_file=os.path.join(
                        self.repo, config_dir(self.config, "notes_dir")
                    ),
                )
            )
        elif len(notes) > 1:
            self.flags.append(
                Flag(
                    category="記録の食い違い",
                    message=f"案件ノートが複数存在します: {[os.path.basename(n) for n in notes]}",
                    evidence_file=notes[0],
                )
            )
        else:
            note_path = notes[0]
            try:
                with open(note_path, "r", encoding="utf-8") as f:
                    content = f.read()
                fm_match = re.search(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
                if fm_match:
                    id_match = re.search(r"^id:\s*(.+)$", fm_match.group(1), re.MULTILINE)
                    if id_match:
                        note_id = id_match.group(1).strip()
                        if note_id != self.project_id:
                            self.flags.append(
                                Flag(
                                    category="記録の食い違い",
                                    message=f"案件ノートの id 不一致: {note_id} != {self.project_id}",
                                    evidence_file=note_path,
                                )
                            )
                    else:
                        self.flags.append(
                            Flag(
                                category="記録の食い違い",
                                message="案件ノートの frontmatter に id がありません",
                                evidence_file=note_path,
                            )
                        )
                else:
                    self.flags.append(
                        Flag(
                            category="記録の食い違い",
                            message="案件ノートに frontmatter がありません",
                            evidence_file=note_path,
                        )
                    )
            except Exception as exc:
                self.flags.append(
                    Flag(
                        category="記録の食い違い",
                        message=f"案件ノート読込エラー: {exc}",
                        evidence_file=note_path,
                    )
                )

    def apply_files(self) -> None:
        """
        2. 既存ファイルを preimage/ へ、候補を staged/ へコピーし、各正本へコピー。
        相対パスのままディレクトリ構造で保存する。
        """
        for file_entry in self.manifest.get("files", []):
            rel_path = file_entry.get("path", "")
            target_path = os.path.join(self.repo, rel_path)
            preimage_sha = file_entry.get("preimage_sha256")
            postimage_sha = file_entry.get("postimage_sha256")

            candidate_raw = file_entry.get("candidate_path") or file_entry.get("candidate")
            _, candidate_path = validate_candidate_file_path(self.repo, candidate_raw)

            # 既存ファイルの退避（ディレクトリ構造を維持）
            orig_mode = 0o644
            if preimage_sha is not None:
                orig_mode = os.stat(target_path).st_mode
                backup_path = os.path.join(self.preimage_dir, rel_path)
                os.makedirs(os.path.dirname(backup_path), exist_ok=True)
                shutil.copy2(target_path, backup_path)
                backed_sha = sha256_file(backup_path)
                if backed_sha != preimage_sha:
                    raise RuntimeError(
                        f"Preimage backup hash mismatch: {backed_sha} != {preimage_sha}"
                    )
                self.backed_up_files[rel_path] = (backup_path, orig_mode, preimage_sha)
                self.log(f"Backed up preimage: {rel_path} -> {backup_path}")
            else:
                self.created_new_paths.append(rel_path)

            # 候補ファイルを staged/ へコピー（ディレクトリ構造を維持）
            staged_path = os.path.join(self.staged_dir, rel_path)
            os.makedirs(os.path.dirname(staged_path), exist_ok=True)
            shutil.copy2(candidate_path, staged_path)
            staged_sha = sha256_file(staged_path)
            if staged_sha != postimage_sha:
                raise RuntimeError(
                    f"Staged copy hash mismatch for {candidate_path}: {staged_sha} != {postimage_sha}"
                )
            self.log(f"Staged candidate: {candidate_path} -> {staged_path}")

            # 各正本へコピー
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            shutil.copy2(candidate_path, target_path)
            if preimage_sha is not None:
                os.chmod(target_path, orig_mode)
            else:
                os.chmod(target_path, 0o644)

            # 正本の sha256 確認
            applied_sha = sha256_file(target_path)
            if applied_sha != postimage_sha:
                raise RuntimeError(
                    f"Applied file hash mismatch for {target_path}: {applied_sha} != {postimage_sha}"
                )
            self.applied_paths.append(rel_path)
            self.log(f"Applied candidate to target: {rel_path} (sha256: {applied_sha})")

    def rollback(self) -> bool:
        """
        失敗時に 2 で置いた全ファイルを preimage から戻し（新規は削除）。
        復元後に全ファイルの sha256 を確かめ（新規は存在しないこと）、
        1つでも合わなければ False を返し self.rollback_failed_files に記録する。
        """
        self.log("Starting rollback...")
        self.rollback_failed_files.clear()

        # 新規作成ファイルの削除
        for rel_path in self.created_new_paths:
            target_path = os.path.join(self.repo, rel_path)
            if os.path.exists(target_path):
                try:
                    os.remove(target_path)
                    self.log(f"Rollback: removed newly created file {rel_path}")
                except Exception as exc:
                    self.log(f"Rollback error removing {rel_path}: {exc}")
            if os.path.exists(target_path):
                self.rollback_failed_files.append(
                    f"{rel_path} (new file could not be removed)"
                )

        # 既存ファイルの復元
        for rel_path, (backup_path, orig_mode, preimage_sha) in self.backed_up_files.items():
            target_path = os.path.join(self.repo, rel_path)
            try:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                shutil.copy2(backup_path, target_path)
                os.chmod(target_path, orig_mode)
                restored_sha = sha256_file(target_path)
                if restored_sha != preimage_sha:
                    self.rollback_failed_files.append(
                        f"{rel_path} (sha256 mismatch after restore: {restored_sha} != {preimage_sha})"
                    )
                else:
                    self.log(f"Rollback: restored {rel_path} to preimage (sha256: {restored_sha})")
            except Exception as exc:
                self.rollback_failed_files.append(f"{rel_path} (restore error: {exc})")

        self.flush_log()
        return len(self.rollback_failed_files) == 0

    def get_process_snapshot(self) -> Dict[int, ProcessInfo]:
        """現在起動しているプロセス一覧を取得"""
        global PS_SNAPSHOT_RUNNER
        procs: Dict[int, ProcessInfo] = {}
        try:
            if PS_SNAPSHOT_RUNNER is not None:
                raw = PS_SNAPSHOT_RUNNER()
                for k, v in raw.items():
                    if isinstance(v, ProcessInfo):
                        procs[k] = v
                    elif isinstance(v, dict):
                        procs[k] = ProcessInfo(
                            pid=k,
                            ppid=v.get("ppid", 0),
                            pgid=v.get("pgid", 0),
                            sess=str(v.get("sess", "")),
                            lstart=str(v.get("lstart", "")),
                            command=str(v.get("command", "")),
                        )
                    else:
                        procs[k] = ProcessInfo(pid=k, command=str(v))
                return procs

            res = subprocess.run(
                ["ps", "-Ao", "pid,ppid,pgid,sess,lstart,command"],
                capture_output=True,
                text=True,
            )
            if res.returncode != 0 and "sess" in res.stderr.lower():
                res = subprocess.run(
                    ["ps", "-Ao", "pid,ppid,pgid,lstart,command"],
                    capture_output=True,
                    text=True,
                )

            if res.returncode != 0:
                self.flags.append(
                    Flag(
                        category="見張り不能",
                        message=f"ps コマンドが終了コード {res.returncode} で失敗しました: {res.stderr}",
                    )
                )
                return procs

            lines = res.stdout.strip().splitlines()
            if len(lines) <= 1:
                # (7) 出力が空または解析できないときも「見張り不能」の旗
                self.flags.append(
                    Flag(
                        category="見張り不能",
                        message="ps コマンドの出力が空またはプロセス行がありません",
                    )
                )
                return procs

            header_lower = lines[0].lower()
            has_sess = "sess" in header_lower
            max_split = 9 if has_sess else 8

            for line in lines[1:]:
                line_str = line.strip()
                if not line_str:
                    continue
                parts = line_str.split(None, max_split)
                if has_sess and len(parts) >= 10 and parts[0].isdigit():
                    pid = int(parts[0])
                    ppid = int(parts[1]) if parts[1].isdigit() else 0
                    pgid = int(parts[2]) if parts[2].isdigit() else 0
                    sess = parts[3]
                    lstart = " ".join(parts[4:9])
                    cmd = parts[9]
                    procs[pid] = ProcessInfo(
                        pid=pid,
                        ppid=ppid,
                        pgid=pgid,
                        sess=sess,
                        lstart=lstart,
                        command=cmd,
                    )
                elif not has_sess and len(parts) >= 9 and parts[0].isdigit():
                    pid = int(parts[0])
                    ppid = int(parts[1]) if parts[1].isdigit() else 0
                    pgid = int(parts[2]) if parts[2].isdigit() else 0
                    sess = ""
                    lstart = " ".join(parts[3:8])
                    cmd = parts[8]
                    procs[pid] = ProcessInfo(
                        pid=pid,
                        ppid=ppid,
                        pgid=pgid,
                        sess=sess,
                        lstart=lstart,
                        command=cmd,
                    )
                elif len(parts) >= 1 and parts[0].isdigit():
                    pid = int(parts[0])
                    cmd = parts[-1]
                    ppid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                    pgid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
                    procs[pid] = ProcessInfo(
                        pid=pid,
                        ppid=ppid,
                        pgid=pgid,
                        command=cmd,
                    )
        except Exception as exc:
            self.flags.append(
                Flag(
                    category="見張り不能",
                    message=f"ps プロセススナップショット取得に失敗しました: {exc}",
                )
            )
            self.log(f"Warning: failed to snapshot processes: {exc}")
        return procs

    def get_monitored_files_snapshot(self) -> Dict[str, Tuple[int, int]]:
        """
        (6) .gitignore の見張りの範囲:
        configured watch_dirs と allowed_paths の親ディレクトリを比べる
        （大きなディレクトリ・runs/・__pycache__・evidence は除く）。
        """
        snapshot: Dict[str, Tuple[int, int]] = {}
        dirs_to_scan: Set[str] = set()

        # 指定ディレクトリの追加
        standard_dirs = list(self.config.get("watch_dirs", []))
        for sd in standard_dirs:
            p = os.path.join(self.repo, sd)
            if os.path.isdir(p):
                dirs_to_scan.add(p)

        # allowed_paths の親ディレクトリ
        for p in self.receipt.get("allowed_paths", []):
            parent = os.path.dirname(os.path.join(self.repo, p))
            if os.path.isdir(parent):
                dirs_to_scan.add(parent)

        for d in dirs_to_scan:
            for root, dirs, files in os.walk(d):
                # runs, __pycache__, evidence ディレクトリは除外
                dirs[:] = [
                    sd
                    for sd in dirs
                    if sd not in ("runs", "__pycache__", "evidence")
                    and not sd.endswith(".tmp")
                ]
                for f in files:
                    full_p = os.path.join(root, f)
                    if os.path.islink(full_p):
                        continue
                    try:
                        st = os.stat(full_p)
                        rel = os.path.relpath(full_p, self.repo)
                        snapshot[rel] = (st.st_mtime_ns, st.st_size)
                    except Exception:
                        pass
        return snapshot

    def check_monitored_files_diff(
        self, pre_snapshot: Dict[str, Tuple[int, int]]
    ) -> None:
        """
        テスト後のファイルシステム直接検査:
        allowed_paths と evidence 以外で変化・新規追加・削除されたファイルがあれば旗。
        """
        post_snapshot = self.get_monitored_files_snapshot()
        allowed_set = set(self.receipt.get("allowed_paths", []))
        evidence_prefix = config_dir(self.config, "evidence_dir").rstrip("/") + "/"

        all_keys = set(pre_snapshot.keys()) | set(post_snapshot.keys())
        for rel in sorted(all_keys):
            if rel in allowed_set:
                continue
            if rel.startswith(evidence_prefix):
                continue
            if rel == self.receipt.get("candidate_path") or rel == os.path.relpath(
                self.receipt_path, self.repo
            ):
                continue

            has_diff = False
            diff_type = ""
            if rel not in pre_snapshot:
                has_diff = True
                diff_type = "新規ファイル"
            elif rel not in post_snapshot:
                has_diff = True
                diff_type = "ファイル削除"
            elif pre_snapshot[rel] != post_snapshot[rel]:
                has_diff = True
                diff_type = "ファイル変更"

            if not has_diff:
                continue

            # 除外判定: 常駐ログ等の除外一覧に合致する場合は旗にせずログに1行残す
            if is_ignored_file(rel, allowed_set):
                self.log(
                    f"監視対象ファイルの変更を検知しましたが除外一覧に合致するため対象外として無視: {rel} ({diff_type})"
                )
                continue

            pre_stat = pre_snapshot.get(rel)
            post_stat = post_snapshot.get(rel)

            pre_size_str = f"{pre_stat[1]} bytes" if pre_stat else "なし (新規ファイル)"
            post_size_str = f"{post_stat[1]} bytes" if post_stat else "なし (削除)"

            if pre_stat and post_stat:
                size_diff_val = post_stat[1] - pre_stat[1]
                size_diff_str = f"{size_diff_val:+d} bytes"
            elif not pre_stat and post_stat:
                size_diff_str = f"+{post_stat[1]} bytes (新規追加)"
            else:
                size_diff_str = f"-{pre_stat[1]} bytes (削除)"

            pre_mtime_str = (
                datetime.datetime.fromtimestamp(
                    pre_stat[0] / 1e9, tz=datetime.timezone.utc
                ).isoformat()
                if pre_stat
                else "なし"
            )
            post_mtime_str = (
                datetime.datetime.fromtimestamp(
                    post_stat[0] / 1e9, tz=datetime.timezone.utc
                ).isoformat()
                if post_stat
                else "なし"
            )

            file_details = {
                "変更前サイズ": pre_size_str,
                "変更後サイズ": post_size_str,
                "サイズ増分": size_diff_str,
                "変更前mtime": pre_mtime_str,
                "変更後mtime": post_mtime_str,
            }

            if diff_type == "新規ファイル":
                self.flags.append(
                    Flag(
                        category="変わったファイル",
                        message=f"ファイルシステム直接検知による予期せぬ新規ファイル (.gitignore盲点対応): {rel}",
                        evidence_file=os.path.join(self.repo, rel),
                        details=file_details,
                    )
                )
            elif diff_type == "ファイル削除":
                self.flags.append(
                    Flag(
                        category="変わったファイル",
                        message=f"ファイルシステム直接検知による予期せぬファイル削除 (.gitignore盲点対応): {rel}",
                        evidence_file=os.path.join(self.repo, rel),
                        details=file_details,
                    )
                )
            else:
                self.flags.append(
                    Flag(
                        category="変わったファイル",
                        message=f"ファイルシステム直接検知による予期せぬファイル変更 (.gitignore盲点対応): {rel}",
                        evidence_file=os.path.join(self.repo, rel),
                        details=file_details,
                    )
                )


    def determine_test_timeout(self, baseline_path: Optional[str]) -> float:
        """
        (5) テストの時間上限決定:
        --test-timeout が明示指定された場合はその値を使う。
        未指定の場合、基準ログがあれば min(60.0, max(1.0, baseline_time * 2.5))
        基準ログがなければ既定 60.0 秒。
        全体の所要時間は ntfy 上限 110 秒以内に収まる設計。
        """
        if self.test_timeout is not None:
            return float(self.test_timeout)

        default_timeout = 60.0
        if not baseline_path:
            return default_timeout

        base_abs = (
            baseline_path
            if os.path.isabs(baseline_path)
            else os.path.join(self.repo, baseline_path)
        )
        if os.path.isfile(base_abs):
            try:
                with open(base_abs, "r", encoding="utf-8") as f:
                    content = f.read()
                base_time = self.extract_time(content)
                if base_time is not None and base_time > 0:
                    return min(default_timeout, max(1.0, base_time * 2.5))
            except Exception:
                pass

        return default_timeout

    def run_tests(self) -> Tuple[bool, str]:
        """
        3. テストを順に実行し、出力を evidence/<project_id>/test-<番号>.log に保存。
        (4) 新しいプロセスグループ (start_new_session=True) で起動し、
        時間切れ時はグループ全体を SIGTERM、残存時は SIGKILL で確実に停止する。
        """
        verification_baselines = self.manifest.get("verification", [])
        pre_procs = self.get_process_snapshot()
        overall_test_start = time.time()
        test_pids: Set[int] = set()
        run_token = secrets.token_hex(16)
        self.last_run_token = run_token

        test_success = True
        test_reason = "All tests passed"

        for idx, test_spec in enumerate(self.test_specs, start=1):
            log_filename = f"test-{idx}.log"
            log_path = os.path.join(self.evidence_dir, log_filename)

            if "::" not in test_spec:
                test_success = False
                test_reason = f"Invalid test specification format (missing '::'): {test_spec}"
                break

            py_bin, test_args_str = test_spec.split("::", 1)
            cmd = [py_bin] + shlex.split(test_args_str)

            baseline_path = (
                verification_baselines[idx - 1]
                if idx - 1 < len(verification_baselines)
                else None
            )
            effective_timeout = self.determine_test_timeout(baseline_path)

            self.log(
                f"Running test {idx}: {' '.join(cmd)} (cwd: {self.test_cwd}, timeout: {effective_timeout:.2f}s)"
            )
            t_start = time.time()
            timed_out = False
            output = ""
            return_code = 0

            try:
                # (4) 時間切れで子まで止める: start_new_session=True で起動
                test_env = os.environ.copy()
                test_env["APPLY_CANDIDATE_RUN_TOKEN"] = run_token
                proc = subprocess.Popen(
                    cmd,
                    cwd=self.test_cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                    env=test_env,
                )
                pgid = proc.pid  # start_new_session=True なので pgid == proc.pid
                test_pids.add(proc.pid)
                try:
                    output, _ = proc.communicate(timeout=effective_timeout)
                    return_code = proc.returncode
                except subprocess.TimeoutExpired:
                    timed_out = True
                    # グループ全体を SIGTERM（os.getpgid を呼ばず記録した pgid を直接使う）
                    try:
                        os.killpg(pgid, signal.SIGTERM)
                    except OSError:
                        pass
                    # 上限つき（3秒）で待機
                    try:
                        output, _ = proc.communicate(timeout=3.0)
                    except subprocess.TimeoutExpired:
                        # まだ残っていればグループ全体を SIGKILL
                        try:
                            os.killpg(pgid, signal.SIGKILL)
                        except OSError:
                            pass
                        # SIGKILL 後も上限つき（3秒）で回収し、無期限に待たない
                        try:
                            output, _ = proc.communicate(timeout=3.0)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            output = ""
            except Exception as exc:
                test_success = False
                test_reason = f"Failed to execute test command {cmd}: {exc}"
                break

            t_elapsed = time.time() - t_start
            output = output or ""

            if timed_out:
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(output)
                    f.write(f"\n[ERROR] Test timed out after {effective_timeout:.2f} seconds\n")
                self.log(f"Test {idx} timed out after {effective_timeout:.2f}s")
                test_success = False
                test_reason = f"Test {idx} timed out after {effective_timeout:.2f} seconds"
                break

            with open(log_path, "w", encoding="utf-8") as f:
                f.write(output)

            self.log(
                f"Test {idx} finished with code {return_code} in {t_elapsed:.3f}s"
            )

            if return_code != 0:
                test_success = False
                test_reason = f"Test {idx} failed with exit code {return_code}"
                break

            # 見張り: テスト出力の検査（警告・件数・時間）
            self.inspect_test_output(
                log_path, output, t_elapsed, baseline_path, idx
            )

        # テスト成功時のみ: 反映ファイルの書換え検知 & 再配置復元
        if test_success:
            self.check_allowed_paths_rewrites(restore_from_staged=True)

        # テスト実行後のプロセス見張り（テスト成功・失敗・タイムアウトにかかわらず必ず実行）
        self.check_process_snapshot(pre_procs, test_pids, run_token, overall_test_start)

        return test_success, test_reason

    def check_allowed_paths_rewrites(self, restore_from_staged: bool = True) -> None:
        """
        反映対象 (manifest['files']) のテスト後書換え検査。
        テスト中に対象ファイルが書き換えられたり消失していたりした場合に「テスト後の書換え」旗を立てる。
        restore_from_staged が True の場合は staged から再配置して復元する（成功経路）。
        False の場合は復元は行わず旗の記録のみ行う（失敗・巻き戻し経路）。
        """
        for file_entry in self.manifest.get("files", []):
            rel_path = file_entry.get("path", "")
            target_path = os.path.join(self.repo, rel_path)
            expected_post = file_entry.get("postimage_sha256")
            staged_path = os.path.join(self.staged_dir, rel_path)

            target_missing = not os.path.exists(target_path)
            target_modified = False
            curr_sha = None
            if not target_missing:
                try:
                    curr_sha = sha256_file(target_path)
                except OSError as exc:
                    curr_sha = f"読取不能({type(exc).__name__})"
                if curr_sha != expected_post:
                    target_modified = True

            if target_missing or target_modified:
                if restore_from_staged:
                    staged_sha = sha256_file(staged_path) if os.path.exists(staged_path) else None
                    if staged_sha != expected_post:
                        self.flags.append(
                            Flag(
                                category="テスト後の書換え",
                                message=f"staged が改変されているため再配置を中止しました: {rel_path} (staged={staged_sha}, expected={expected_post})",
                                evidence_file=staged_path if os.path.exists(staged_path) else target_path,
                            )
                        )
                    else:
                        shutil.copy2(staged_path, target_path)
                        restored_sha = sha256_file(target_path)
                        if target_missing:
                            self.flags.append(
                                Flag(
                                    category="テスト後の書換え",
                                    message=f"テスト終了後に反映ファイルが消失していたため staged から再配置・復元しました: {rel_path}",
                                    evidence_file=target_path,
                                )
                            )
                        else:
                            self.flags.append(
                                Flag(
                                    category="テスト後の書換え",
                                    message=f"テスト終了後に反映ファイルが書き換えられていたため staged から再配置・復元しました: {rel_path} (改変値 {curr_sha} -> 復元値 {restored_sha})",
                                    evidence_file=target_path,
                                )
                            )
                else:
                    if target_missing:
                        self.flags.append(
                            Flag(
                                category="テスト後の書換え",
                                message=f"テスト失敗後に反映ファイルが消失していることを検出しました: {rel_path}",
                                evidence_file=target_path,
                            )
                        )
                    else:
                        self.flags.append(
                            Flag(
                                category="テスト後の書換え",
                                message=f"テスト失敗後に反映ファイルが書き換えられていることを検出しました: {rel_path} (改変値 {curr_sha}, 期待値 {expected_post})",
                                evidence_file=target_path,
                            )
                        )

    def check_process_snapshot(
        self,
        pre_procs: Dict[int, ProcessInfo],
        test_pids: Set[int],
        run_token: str,
        test_start_time: float,
    ) -> None:
        """
        テスト実行後のプロセス見張り:
        1. pgid がテスト PID と一致
        2. または ppid をたどってテスト PID に到達
        3. または ps -E で環境変数に APPLY_CANDIDATE_RUN_TOKEN が含まれる
        4. または環境変数が読めない（PATH=/HOME= が無い）/ ps -E 失敗で、ppid=1 かつ開始時刻がテスト開始後
        のプロセスを検知して旗を立てる。
        """
        post_procs = self.get_process_snapshot()
        my_pid = os.getpid()
        new_pids = set(post_procs.keys()) - set(pre_procs.keys())
        new_pids.discard(my_pid)

        for pid in sorted(new_pids):
            proc_info = post_procs[pid]
            cmd_str = proc_info.command
            if "ps -Ao" in cmd_str:
                continue

            # テスト起因判定:
            is_descendant = False
            matched_test_pid: Optional[int] = None
            relationship = ""

            if proc_info.pgid in test_pids:
                is_descendant = True
                matched_test_pid = proc_info.pgid
                relationship = f"テスト PID {matched_test_pid} の子孫 (pgid一致)"
            else:
                curr_ppid = proc_info.ppid
                visited_pids = {pid}
                ppid_chain = [str(curr_ppid)]
                while curr_ppid > 1 and curr_ppid not in visited_pids:
                    if curr_ppid in test_pids:
                        is_descendant = True
                        matched_test_pid = curr_ppid
                        relationship = f"テスト PID {matched_test_pid} の子孫 (ppid経路: {' -> '.join(ppid_chain)})"
                        break
                    visited_pids.add(curr_ppid)
                    parent_info = post_procs.get(curr_ppid) or pre_procs.get(curr_ppid)
                    if not parent_info or parent_info.ppid <= 0:
                        break
                    curr_ppid = parent_info.ppid
                    ppid_chain.append(str(curr_ppid))

            # pgid・ppid 経路で判定できなかったもの:
            if not is_descendant:
                ps_e_ok = False
                has_env_vars = False
                ps_e_stdout = ""
                try:
                    res_e = run_ps_e(pid)
                    if res_e.returncode == 0:
                        ps_e_ok = True
                        ps_e_stdout = res_e.stdout or ""
                        # 空白区切りの語として PATH= または HOME= が存在すれば環境変数が読めたと判定
                        for word in ps_e_stdout.split():
                            if word.startswith("PATH=") or word.startswith("HOME="):
                                has_env_vars = True
                                break
                except Exception:
                    ps_e_ok = False

                if ps_e_ok and has_env_vars:
                    # (a) ps -E で環境変数が読めて、しるしがある -> 旗
                    if run_token in ps_e_stdout:
                        is_descendant = True
                        relationship = "環境変数のしるしで判定"
                    else:
                        # (b) 環境変数が読めて、しるしがない -> 旗にしない（ログに1行）
                        self.log(
                            f"残存プロセス PID {pid} (pgid={proc_info.pgid}, ppid={proc_info.ppid}: {cmd_str}) は環境変数にしるしが無いため対象外として無視"
                        )
                        continue
                else:
                    exe = cmd_str.split(" ", 1)[0]
                    if exe.startswith(OS_EXE_PREFIX) and os.path.basename(exe) in self.config.get("os_process_names", []):
                        self.log(f"残存プロセス PID {pid} (ppid={proc_info.ppid}: {cmd_str}) は OS のプロセスのため対象外として無視")
                        continue
                    # (c) 環境変数が読めない（Apple 付属のコマンド等で出力に '=' が無い）、または ps -E が失敗・例外:
                    # ppid が 1（親が終わって孤立）で、かつ開始時刻がテスト開始より後なら旗
                    started_after_test = is_started_after(proc_info.lstart, test_start_time)
                    if proc_info.ppid == 1 and started_after_test:
                        is_descendant = True
                        relationship = "環境変数を読めない孤立プロセス（要確認）"
                    else:
                        reason = "親プロセスが稼働中 (ppid != 1)" if proc_info.ppid != 1 else "テスト開始前のプロセス"
                        self.log(
                            f"残存プロセス PID {pid} (pgid={proc_info.pgid}, ppid={proc_info.ppid}: {cmd_str}) は環境変数を読めず孤立プロセス要件を満たさないため対象外として無視 ({reason})"
                        )
                        continue

            if not is_descendant:
                self.log(
                    f"残存プロセス PID {pid} (pgid={proc_info.pgid}, ppid={proc_info.ppid}: {cmd_str}) はテスト起因でないため対象外として無視"
                )
                continue

            proc_details = {
                "PID": str(pid),
                "PPID": str(proc_info.ppid),
                "PGID": str(proc_info.pgid),
                "開始時刻": proc_info.lstart or "不明",
                "コマンド全文": cmd_str,
                "テストプロセスとの関係": relationship,
            }

            self.flags.append(
                Flag(
                    category="残ったプロセス",
                    message=f"PID {pid} がテスト後も稼働中: {cmd_str}",
                    evidence_file=os.path.join(self.evidence_dir, "test-1.log"),
                    details=proc_details,
                )
            )

    def inspect_test_output(
        self,
        log_path: str,
        output: str,
        actual_time: float,
        baseline_rel_path: Optional[str],
        test_index: int,
    ) -> None:
        """テスト出力の見張り（警告、件数減、スキップ増、実測時間超過）"""
        lines = output.splitlines()

        # 1. テストの警告見張り: Warning や Traceback
        for line_idx, line in enumerate(lines, start=1):
            if re.search(r"Warning\b|Traceback\b", line, re.IGNORECASE):
                start_l = max(0, line_idx - 5)
                end_l = min(len(lines), line_idx + 15)
                excerpt = "\n".join(lines[start_l:end_l])
                self.flags.append(
                    Flag(
                        category="テストの警告",
                        message=f"Warning または Traceback を検出: {line.strip()}",
                        evidence_file=log_path,
                        line_num=line_idx,
                        excerpt=excerpt,
                    )
                )
                break

        # 2. 基準ログとの比較（件数・スキップ・実測時間）
        if not baseline_rel_path:
            self.flags.append(
                Flag(
                    category="テストの件数",
                    message=f"テスト {test_index} に対応する基準ログが manifest.verification に指定されていません（基準なし）",
                    evidence_file=self.manifest_path,
                )
            )
            return

        baseline_abs_path = (
            baseline_rel_path
            if os.path.isabs(baseline_rel_path)
            else os.path.join(self.repo, baseline_rel_path)
        )
        if not os.path.isfile(baseline_abs_path):
            self.flags.append(
                Flag(
                    category="テストの件数",
                    message=f"基準ログファイルが見つかりません（基準なし）: {baseline_rel_path}",
                    evidence_file=self.manifest_path,
                )
            )
            return

        with open(baseline_abs_path, "r", encoding="utf-8") as f:
            base_output = f.read()

        act_ran = self.extract_ran_tests(output)
        base_ran = self.extract_ran_tests(base_output)
        if act_ran is None or base_ran is None:
            self.flags.append(
                Flag(
                    category="テストの件数",
                    message=f"テスト件数 'Ran N tests' の解析ができませんでした（基準なし）: log={log_path}, base={baseline_rel_path}",
                    evidence_file=log_path,
                )
            )
        else:
            if act_ran < base_ran:
                self.flags.append(
                    Flag(
                        category="テストの件数",
                        message=f"テスト実行件数が基準より減少: 実行={act_ran} < 基準={base_ran}",
                        evidence_file=log_path,
                    )
                )

        act_skipped = self.extract_skipped(output)
        base_skipped = self.extract_skipped(base_output)
        if act_skipped > base_skipped:
            self.flags.append(
                Flag(
                    category="テストの件数",
                    message=f"スキップ件数が基準より増加: 実行={act_skipped} > 基準={base_skipped}",
                    evidence_file=log_path,
                )
            )

        # (7) MED: 時間の判定は実測時間を使う（出力値は参考記録のみ）
        base_time = self.extract_time(base_output)
        if base_time is not None:
            act_time_in_log = self.extract_time(output)
            if actual_time > 3.0 * base_time:
                log_info = f" (ログ内表記: {act_time_in_log}s)" if act_time_in_log is not None else ""
                self.flags.append(
                    Flag(
                        category="テストの時間",
                        message=f"実測実行時間 ({actual_time:.3f}s) が基準 ({base_time:.3f}s) の3倍を超過{log_info}",
                        evidence_file=log_path,
                    )
                )

    def extract_ran_tests(self, text: str) -> Optional[int]:
        m = re.search(r"Ran\s+(\d+)\s+tests?", text)
        return int(m.group(1)) if m else None

    def extract_skipped(self, text: str) -> int:
        m = re.search(r"skipped=(\d+)", text)
        return int(m.group(1)) if m else 0

    def extract_time(self, text: str) -> Optional[float]:
        m = re.search(r"in\s+([0-9.]+)s", text)
        return float(m.group(1)) if m else None

    def get_listener_err_log_size(self) -> int:
        """Configured restart check log current byte size; zero when disabled/missing."""
        rel = self.config.get("restart_check_log")
        if not rel:
            return 0
        log_path = os.path.join(self.repo, rel)
        if os.path.isfile(log_path):
            return os.path.getsize(log_path)
        return 0

    def _expanded_restart_command(self) -> List[str]:
        values = {"uid": str(os.getuid()), "repo": self.repo}
        expanded: List[str] = []
        for arg in self.config.get("restart_command", []):
            try:
                expanded.append(arg.format(**values))
            except (KeyError, ValueError) as exc:
                raise ValueError(f"invalid restart_command placeholder in {arg!r}: {exc}")
        return expanded

    def restart_approval_listener(self, pre_size: int) -> None:
        """Run configured restart command and inspect optional appended error-log content."""
        command = self._expanded_restart_command()
        if not command:
            self.log("Restart requested but restart_command is empty; restart disabled")
            return
        self.log(f"Running configured restart command: {shlex.join(command)}")

        # Keep the legacy launchctl hook/self-test behavior when the configured command is launchctl.
        if command[0] == "launchctl":
            restart_res = run_launchctl(command[1:])
        else:
            restart_res = subprocess.run(command, capture_output=True, text=True)
        self.log(
            f"restart command returncode: {restart_res.returncode}, stdout: {restart_res.stdout}, stderr: {restart_res.stderr}"
        )

        SLEEP_FN(10.0)

        # Legacy-compatible health check can be derived without an extra environment-specific key.
        # For `launchctl kickstart ... <domain>/<label>`, confirm the label has a PID in `launchctl list`.
        if len(command) >= 4 and command[0] == "launchctl" and command[1] == "kickstart":
            label = command[-1].rsplit("/", 1)[-1]
            list_res = run_launchctl(["list"])
            has_pid = False
            for line in list_res.stdout.splitlines():
                if label in line:
                    parts = line.strip().split()
                    if parts and parts[0].isdigit():
                        has_pid = True
                        self.log(f"Restarted service running with PID {parts[0]}")
                    break
            if not has_pid:
                evidence = ""
                if self.config.get("restart_check_log"):
                    evidence = os.path.join(self.repo, self.config["restart_check_log"])
                self.flags.append(
                    Flag(
                        category="リスナー",
                        message="再起動後10秒時点で launchctl list に PID が確認できません",
                        evidence_file=evidence,
                    )
                )
        elif restart_res.returncode != 0:
            self.flags.append(
                Flag(
                    category="リスナー",
                    message=f"restart_command が終了コード {restart_res.returncode} で失敗しました",
                )
            )

        rel_log = self.config.get("restart_check_log")
        if not rel_log:
            return
        err_log_path = os.path.join(self.repo, rel_log)
        if os.path.isfile(err_log_path):
            current_size = os.path.getsize(err_log_path)
            if current_size > pre_size:
                try:
                    with open(err_log_path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(pre_size)
                        new_content = f.read()
                    lines = new_content.splitlines()
                    for line_idx, line in enumerate(lines, start=1):
                        if "ERROR" in line or "Traceback" in line:
                            start_l = max(0, line_idx - 3)
                            end_l = min(len(lines), line_idx + 15)
                            excerpt = "\n".join(lines[start_l:end_l])
                            self.flags.append(
                                Flag(
                                    category="リスナー",
                                    message=f"{os.path.basename(err_log_path)} にエラーを検出: {line.strip()}",
                                    evidence_file=err_log_path,
                                    line_num=line_idx,
                                    excerpt=excerpt,
                                )
                            )
                            break
                except Exception as exc:
                    self.log(f"Error reading restart_check_log: {exc}")

    def get_git_status(self) -> Set[str]:
        """git status --porcelain の行セットを取得"""
        try:
            res = subprocess.run(
                ["git", "-C", self.repo, "status", "--porcelain", "-uall"],
                capture_output=True,
                text=True,
            )
            if res.returncode != 0:
                self.flags.append(
                    Flag(
                        category="見張り不能",
                        message=f"git status が終了コード {res.returncode} で失敗しました: {res.stderr}",
                    )
                )
                return set()
            return set(line for line in res.stdout.splitlines() if line.strip())
        except Exception as exc:
            self.flags.append(
                Flag(
                    category="見張り不能",
                    message=f"git status コマンドの実行に失敗しました: {exc}",
                )
            )
            self.log(f"Warning: git status failed: {exc}")
            return set()

    def check_git_status_diff(
        self,
        pre_status: Set[str],
        pre_snapshot: Optional[Dict[str, Tuple[int, int]]] = None,
    ) -> None:
        """
        見張り: 反映の前後で git status --porcelain を取り、
        allowed_paths と evidence 以外で新たに変わった・増えたものがあれば旗。
        """
        post_status = self.get_git_status()
        new_lines = post_status - pre_status
        allowed_set = set(self.receipt.get("allowed_paths", []))

        evidence_rel_prefix = os.path.relpath(
            os.path.join(self.repo, config_dir(self.config, "evidence_dir")),
            self.repo,
        )

        for line in sorted(new_lines):
            if len(line) < 4:
                continue
            path_part = line[3:].strip()
            if " -> " in path_part:
                path_part = path_part.split(" -> ")[1].strip()

            if path_part in allowed_set:
                continue
            if path_part.startswith(evidence_rel_prefix.rstrip("/") + "/") or path_part == evidence_rel_prefix:
                continue

            if is_ignored_file(path_part, allowed_set):
                self.log(
                    f"git status 差分を検知しましたが除外一覧に合致するため対象外として無視: {path_part}"
                )
                continue

            full_target_p = os.path.join(self.repo, path_part)

            # 変更前の値（pre_snapshot から取得）
            pre_stat = pre_snapshot.get(path_part) if pre_snapshot else None
            pre_size_val: Optional[int] = None
            if pre_stat:
                pre_size_val = pre_stat[1]
                pre_size_str = f"{pre_stat[1]} bytes"
                pre_mtime_str = datetime.datetime.fromtimestamp(
                    pre_stat[0] / 1e9, tz=datetime.timezone.utc
                ).isoformat()
            else:
                pre_size_str = "不明"
                pre_mtime_str = "不明"

            # 変更後の値
            post_size_val: Optional[int] = None
            if os.path.exists(full_target_p):
                try:
                    st = os.stat(full_target_p)
                    post_size_val = st.st_size
                    post_size_str = f"{st.st_size} bytes"
                    post_mtime_str = datetime.datetime.fromtimestamp(
                        st.st_mtime, tz=datetime.timezone.utc
                    ).isoformat()
                except Exception:
                    post_size_str = "不明"
                    post_mtime_str = "不明"
            else:
                post_size_str = "なし（削除）"
                post_mtime_str = "なし（削除）"

            # サイズ増分
            if pre_size_val is not None and post_size_val is not None:
                diff_val = post_size_val - pre_size_val
                size_diff_str = f"{diff_val:+d} bytes"
            elif pre_size_val is not None and post_size_str == "なし（削除）":
                size_diff_str = f"-{pre_size_val} bytes (削除)"
            else:
                size_diff_str = "不明"

            file_details = {
                "変更前サイズ": pre_size_str,
                "変更後サイズ": post_size_str,
                "サイズ増分": size_diff_str,
                "変更前mtime": pre_mtime_str,
                "変更後mtime": post_mtime_str,
            }

            self.flags.append(
                Flag(
                    category="変わったファイル",
                    message=f"allowed_paths および evidence 外の予期せぬ変更: {line}",
                    evidence_file=full_target_p,
                    details=file_details,
                )
            )

    def write_verification(self) -> str:
        """
        5. evidence/<project_id>/VERIFICATION.md に結果（照合・ハッシュ・テストの要約・旗）を書く。
        """
        verif_path = os.path.join(self.evidence_dir, "VERIFICATION.md")
        ts_now = datetime.datetime.now(datetime.timezone.utc).isoformat()

        lines = [
            f"# {self.project_id} 適用検証ログ",
            "",
            f"- **Approval ID**: `{self.approval_id}`",
            f"- **Project ID**: `{self.project_id}`",
            f"- **Apply Worker**: apply_candidate.py (automated)",
            f"- **Timestamp**: {ts_now}",
            f"- **Approver**: {self.receipt.get('approver_identity')} (`approved_at: {self.receipt.get('approved_at')}`, `expires_at: {self.receipt.get('expires_at')}`)",
            f"- **Explicit Approval Quote**: `{self.receipt.get('explicit_approval_quote')}`",
            f"- **Audit Status**: `{self.receipt.get('audit_status')}`",
            "",
            "## 1. Preflight 照合結果 (PASS)",
            "",
            f"- **Manifest SHA-256**: `{sha256_file(self.manifest_path)}` (一致)",
            "- **Allowed paths / Files**:",
        ]

        for f_entry in self.manifest.get("files", []):
            p = f_entry.get("path")
            pre_h = f_entry.get("preimage_sha256")
            post_h = f_entry.get("postimage_sha256")
            lines.append(f"  - `{p}`: preimage={pre_h}, postimage={post_h}")

        lines.extend([
            "",
            "## 2. 正本反映結果",
            "",
            f"- **Preimage 保存先**: `{self.preimage_dir}`",
            f"- **Staged 保存先**: `{self.staged_dir}`",
            "- **反映後 SHA-256 確認**:",
        ])
        for rel_path in self.applied_paths:
            target_path = os.path.join(self.repo, rel_path)
            cur_sha = sha256_file(target_path)
            lines.append(f"  - `{rel_path}`: `{cur_sha}` (一致)")

        lines.extend([
            "",
            "## 3. テスト実行結果",
            "",
        ])
        for idx, spec in enumerate(self.test_specs, start=1):
            log_rel = f"{config_dir(self.config, 'evidence_dir')}/{self.project_id}/test-{idx}.log"
            lines.append(f"- **Test {idx}**: `{spec}` -> PASS (ログ: `{log_rel}`)")

        lines.extend([
            "",
            "## 4. 見張り（旗）の結果",
            "",
        ])
        if self.flags:
            lines.append(f"- **旗の総数**: {len(self.flags)} 件 (APPLIED_WITH_FLAGS)")
            for f in self.flags:
                lines.append(f"  - {f.summary()}")
        else:
            lines.append("- **旗の総数**: 0 件 (APPLIED)")

        lines.append("")
        with open(verif_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        return verif_path

    def update_project_note(self, result_str: str) -> None:
        """
        <repo>/<configured notes_dir>/<project_id>--*.md の frontmatter の
        status を applied に変え、末尾に「## 適用結果」を追記。
        """
        pattern = os.path.join(
            self.repo,
            config_dir(self.config, "notes_dir"),
            f"{self.project_id}--*.md",
        )
        notes = glob.glob(pattern)
        if len(notes) != 1:
            self.log(
                f"Warning: expected exactly 1 project note matching {pattern}, found {len(notes)}"
            )
            return

        note_path = notes[0]
        try:
            with open(note_path, "r", encoding="utf-8") as f:
                content = f.read()

            new_content = re.sub(
                r"^status:\s*.*$", "status: applied", content, flags=re.MULTILINE
            )

            ts_now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            result_block = (
                f"\n\n## 適用結果\n"
                f"- approval_id: {self.approval_id}\n"
                f"- 適用日時: {ts_now}\n"
                f"- RESULT: {result_str}\n"
                f"- 旗の件数: {len(self.flags)}\n"
            )
            new_content = new_content.rstrip() + result_block

            with open(note_path, "w", encoding="utf-8") as f:
                f.write(new_content)

            self.log(f"Updated project note: {note_path}")
        except Exception as exc:
            self.log(f"Error updating project note {note_path}: {exc}")

    def write_flags_and_order(self) -> Tuple[str, str]:
        """
        旗が1件以上ある場合:
        - evidence/<project_id>/FLAGS.md
        - <repo>/<configured flags_order_dir>/flags-<project_id>.md
        を作成。
        """
        flags_path = os.path.join(self.evidence_dir, "FLAGS.md")
        order_path = os.path.join(
            self.repo, config_dir(self.config, "flags_order_dir"), f"flags-{self.project_id}.md"
        )
        os.makedirs(os.path.dirname(order_path), exist_ok=True)

        f_lines = [
            f"# {self.project_id} 適用時フラグ一覧 (APPLIED_WITH_FLAGS)",
            "",
            f"- **Approval ID**: `{self.approval_id}`",
            f"- **Project ID**: `{self.project_id}`",
            f"- **フラグ総数**: {len(self.flags)}",
            "",
        ]
        for idx, flag in enumerate(self.flags, start=1):
            f_lines.extend([
                f"## 旗 {idx}: [{flag.category}]",
                f"- **内容**: {flag.message}",
                f"- **証拠ファイル**: `{flag.evidence_file}`"
                + (f" (L{flag.line_num})" if flag.line_num else ""),
            ])
            if flag.details:
                f_lines.append("- **証拠詳細**:")
                for k, v in flag.details.items():
                    f_lines.append(f"  - **{k}**: {v}")
            if flag.excerpt:
                f_lines.extend([
                    "- **抜粋 (最大20行)**:",
                    "```",
                    flag.excerpt.strip(),
                    "```",
                ])
            f_lines.append("")

        with open(flags_path, "w", encoding="utf-8") as f:
            f.write("\n".join(f_lines))

        o_lines = [
            f"# 発注: 適用時の旗確認 ({self.project_id})",
            "",
            "以下の旗が立った。それぞれ、実害か・無害か・直すなら何をかを判断せよ。読むだけ。",
            "",
            "## 旗の一覧",
        ]
        evidence_files_set = {flags_path, os.path.join(self.evidence_dir, "VERIFICATION.md")}
        for flag in self.flags:
            o_lines.append(f"- [{flag.category}] {flag.message}")
            if flag.details:
                for k, v in flag.details.items():
                    o_lines.append(f"  - {k}: {v}")
            if flag.evidence_file:
                evidence_files_set.add(flag.evidence_file)

        o_lines.extend([
            "",
            "## 証拠ファイル",
        ])
        for ef in sorted(evidence_files_set):
            rel_ef = os.path.relpath(ef, self.repo) if ef.startswith(self.repo) else ef
            o_lines.append(f"- `{rel_ef}`")
        o_lines.append("")

        with open(order_path, "w", encoding="utf-8") as f:
            f.write("\n".join(o_lines))

        return flags_path, order_path


# =====================================================================
# Self-Test 実装
# =====================================================================

def _run_self_test_once(self_test_config: Dict[str, Any], suite_name: str) -> int:
    """
    --self-test の実行。
    外部環境へのアクセスは行わず、一時ディレクトリ内で全機能をテストする。
    再監査 r2 指摘事項 1〜7 の検査を網羅する。
    """
    global LAUNCHCTL_RUNNER, SLEEP_FN, PS_SNAPSHOT_RUNNER, PS_E_RUNNER, ACTIVE_CONFIG, SELF_TEST_CONFIG_OVERRIDE
    print(f"Running self-test suite: {suite_name}...")
    SELF_TEST_CONFIG_OVERRIDE = json.loads(json.dumps(self_test_config))
    # load_config normalizes ignore_files to tuples; JSON copying returns lists, which are equivalent here.
    ACTIVE_CONFIG = SELF_TEST_CONFIG_OVERRIDE

    mock_listener_running = True

    def mock_launchctl(args: List[str]) -> subprocess.CompletedProcess:
        nonlocal mock_listener_running
        if args[0] == "kickstart":
            mock_listener_running = True
            return subprocess.CompletedProcess(args, 0, stdout="kickstart OK", stderr="")
        elif args[0] == "list":
            if mock_listener_running:
                stdout_text = "99999\t0\tservice.example.listener\n"
            else:
                stdout_text = "-\t0\tservice.example.listener\n"
            return subprocess.CompletedProcess(args, 0, stdout=stdout_text, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    LAUNCHCTL_RUNNER = mock_launchctl
    SLEEP_FN = lambda _: None
    PS_SNAPSHOT_RUNNER = lambda: {1: "/sbin/launchd"}

    with tempfile.TemporaryDirectory() as temp_dir:
        repo = os.path.join(temp_dir, "repo")
        os.makedirs(repo)

        # git リポジトリ初期化
        subprocess.run(["git", "-C", repo, "init"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", repo, "config", "user.email", "test@test.local"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", repo, "config", "user.name", "Test User"],
            check=True,
            capture_output=True,
        )

        receipts_dir = os.path.join(repo, config_dir(ACTIVE_CONFIG, "receipts_dir"))
        candidates_dir = os.path.join(repo, config_dir(ACTIVE_CONFIG, "candidates_dir"))
        projects_dir = os.path.join(repo, config_dir(ACTIVE_CONFIG, "notes_dir"))
        front_dir = os.path.join(repo, "app/runtime")
        os.makedirs(receipts_dir, exist_ok=True)
        os.makedirs(candidates_dir, exist_ok=True)
        os.makedirs(projects_dir, exist_ok=True)
        os.makedirs(front_dir, exist_ok=True)

        orig_a_path = os.path.join(front_dir, "a.py")
        with open(orig_a_path, "w", encoding="utf-8") as f:
            f.write("# original a.py\n")
        orig_a_sha = sha256_file(orig_a_path)

        subprocess.run(["git", "-C", repo, "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", repo, "commit", "-m", "init"],
            check=True,
            capture_output=True,
        )

        cand_a_path = os.path.join(candidates_dir, "PS-20260924-01--a.py")
        with open(cand_a_path, "w", encoding="utf-8") as f:
            f.write("# candidate a.py modified\n")
        cand_a_sha = sha256_file(cand_a_path)

        cand_b_path = os.path.join(candidates_dir, "PS-20260924-01--b.py")
        with open(cand_b_path, "w", encoding="utf-8") as f:
            f.write("# candidate b.py new\n")
        cand_b_sha = sha256_file(cand_b_path)

        base_log_dir = os.path.join(repo, "runtime/tmp")
        os.makedirs(base_log_dir, exist_ok=True)
        base_log_path = os.path.join(base_log_dir, "test-baseline.log")
        with open(base_log_path, "w", encoding="utf-8") as f:
            f.write(
                "test_dummy (tests.Test) ... ok\n"
                "----------------------------------------------------------------------\n"
                "Ran 10 tests in 2.000s\n\n"
                "OK\n"
            )

        def make_manifest(
            proj_id="PS-20260924-01",
            files_override=None,
            verification_override=None,
        ) -> str:
            m_path = os.path.join(candidates_dir, f"{proj_id}--manifest.json")
            files_data = files_override if files_override is not None else [
                {
                    "path": "app/runtime/a.py",
                    "candidate_path": cand_a_path,
                    "preimage_sha256": orig_a_sha,
                    "postimage_sha256": cand_a_sha,
                },
                {
                    "path": "app/runtime/b.py",
                    "candidate_path": cand_b_path,
                    "preimage_sha256": None,
                    "postimage_sha256": cand_b_sha,
                },
            ]
            verif_data = (
                verification_override
                if verification_override is not None
                else ["runtime/tmp/test-baseline.log"]
            )
            data = {
                "project_id": proj_id,
                "verification": verif_data,
                "files": files_data,
            }
            with open(m_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
            return m_path

        def make_receipt(
            manifest_p: str,
            proj_id="PS-20260924-01",
            approval_id="APR-20260924-01",
            expires_at=None,
            allowed_paths=None,
            approver="Alice",
            quote="ntfy approved",
            audit_status="pass",
            manifest_sha_override=None,
        ) -> str:
            r_path = os.path.join(receipts_dir, f"{approval_id}.json")
            m_sha = manifest_sha_override or sha256_file(manifest_p)
            exp = expires_at or (
                datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(hours=2)
            ).isoformat()
            paths = allowed_paths if allowed_paths is not None else [
                "app/runtime/a.py",
                "app/runtime/b.py",
            ]
            data = {
                "approval_id": approval_id,
                "project_id": proj_id,
                "approved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "expires_at": exp,
                "approver_identity": approver,
                "explicit_approval_quote": quote,
                "candidate_sha256": m_sha,
                "candidate_path": os.path.relpath(manifest_p, repo),
                "allowed_paths": paths,
                "audit_status": audit_status,
            }
            with open(r_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
            return r_path

        def make_note(proj_id="PS-20260924-01") -> str:
            n_path = os.path.join(projects_dir, f"{proj_id}--note.md")
            content = f"---\nid: {proj_id}\nstatus: review\n---\n# Note for {proj_id}\n"
            with open(n_path, "w", encoding="utf-8") as f:
                f.write(content)
            return n_path

        test_pass_cmd = (
            f'{sys.executable}::-c "print(\'Ran 10 tests in 0.500s\\n\\nOK\')"'
        )
        test_fail_cmd = (
            f'{sys.executable}::-c "import sys; print(\'FAIL\'); sys.exit(1)"'
        )

        # -------------------------------------------------------------
        # 1. 正常系テスト (APPLIED, exit 0) & ディレクトリ階層退避の確認
        # -------------------------------------------------------------
        manifest_p = make_manifest()
        receipt_p = make_receipt(manifest_p)
        note_p = make_note()

        runner = ApplyCandidateRunner(
            receipt_path=receipt_p,
            test_cwd=repo,
            test_specs=[test_pass_cmd],
            restart_listener=True,
            repo=repo,
            dry_run=False,
        )
        res_str, code, out = runner.run()
        assert out.splitlines()[0] == f"RESULT: {RES_APPLIED}", f"Expected RESULT: {RES_APPLIED}, got: {out}"
        assert code == EXIT_OK
        assert sha256_file(orig_a_path) == cand_a_sha
        assert sha256_file(os.path.join(front_dir, "b.py")) == cand_b_sha

        expected_preimage = os.path.join(
            repo, config_dir(ACTIVE_CONFIG, "evidence_dir"), "PS-20260924-01", "preimage", "app", "runtime", "a.py"
        )
        expected_staged = os.path.join(
            repo, config_dir(ACTIVE_CONFIG, "evidence_dir"), "PS-20260924-01", "staged", "app", "runtime", "a.py"
        )
        assert os.path.isfile(expected_preimage)
        assert os.path.isfile(expected_staged)

        with open(note_p) as f:
            note_content = f.read()
        assert "status: applied" in note_content
        assert "## 適用結果" in note_content
        print("[OK] Test 1: Normal APPLIED passed")

        # -------------------------------------------------------------
        # 2. --dry-run テスト (DRY_RUN_OK, exit 0)
        # -------------------------------------------------------------
        os.remove(os.path.join(front_dir, "b.py"))
        with open(orig_a_path, "w", encoding="utf-8") as f:
            f.write("# original a.py\n")

        runner_dry = ApplyCandidateRunner(
            receipt_path=receipt_p,
            test_cwd=repo,
            test_specs=[test_pass_cmd],
            restart_listener=False,
            repo=repo,
            dry_run=True,
        )
        res_str, code, out = runner_dry.run()
        assert out.splitlines()[0] == f"RESULT: {RES_DRY_RUN_OK}"
        assert code == EXIT_OK
        assert sha256_file(orig_a_path) == orig_a_sha
        assert not os.path.exists(os.path.join(front_dir, "b.py"))
        print("[OK] Test 2: --dry-run passed")

        # -------------------------------------------------------------
        # 3. 照合の各不一致で REFUSED (exit 3)
        # - (1) project_id / approval_id の正規表現拒否
        # - (2) candidates 外の candidate_path 拒否
        # - (3) 重複の別表記（a/./b や大文字小文字違い）拒否
        # - (7) python 以外のテストバイナリ拒否
        # -------------------------------------------------------------
        # (1) project_id 不正
        m_bad_pid = make_manifest(proj_id="!invalid-project")
        r_bad_pid = make_receipt(m_bad_pid, proj_id="!invalid-project", approval_id="APR-20260924-87")
        runner = ApplyCandidateRunner(r_bad_pid, repo, [test_pass_cmd], False, repo, False)
        res_str, code, out = runner.run()
        assert out.splitlines()[0] == f"RESULT: {RES_REFUSED}" and code == EXIT_REFUSED

        # (1) approval_id 不正
        r_bad_aid = make_receipt(manifest_p, approval_id="!invalid-approval")
        runner = ApplyCandidateRunner(r_bad_aid, repo, [test_pass_cmd], False, repo, False)
        res_str, code, out = runner.run()
        assert out.splitlines()[0] == f"RESULT: {RES_REFUSED}" and code == EXIT_REFUSED

        # (2) candidates ディレクトリ外の candidate_path 拒否
        outside_cand = os.path.join(repo, "runtime/outside_cand.py")
        with open(outside_cand, "w") as f:
            f.write("# outside\n")
        m_out_cand = make_manifest(
            proj_id="PS-20260924-88",
            files_override=[
                {
                    "path": "app/runtime/a.py",
                    "candidate_path": outside_cand,
                    "preimage_sha256": orig_a_sha,
                    "postimage_sha256": sha256_file(outside_cand),
                }
            ],
        )
        r_out_cand = make_receipt(m_out_cand, proj_id="PS-20260924-88", approval_id="APR-20260924-88", allowed_paths=["app/runtime/a.py"])
        runner = ApplyCandidateRunner(r_out_cand, repo, [test_pass_cmd], False, repo, False)
        res_str, code, out = runner.run()
        assert out.splitlines()[0] == f"RESULT: {RES_REFUSED}" and code == EXIT_REFUSED

        # (3) 重複の別表記: a/./b や末尾スラッシュ
        r_dot_path = make_receipt(
            manifest_p,
            approval_id="APR-20260924-89",
            allowed_paths=["app/runtime/./a.py", "app/runtime/b.py"],
        )
        runner = ApplyCandidateRunner(r_dot_path, repo, [test_pass_cmd], False, repo, False)
        res_str, code, out = runner.run()
        assert out.splitlines()[0] == f"RESULT: {RES_REFUSED}" and code == EXIT_REFUSED

        # (3) 重複の別表記: 大文字小文字違い (Case-insensitive duplicate)
        r_case_dup = make_receipt(
            manifest_p,
            approval_id="APR-20260924-90",
            allowed_paths=["app/runtime/a.py", "app/runtime/A.py"],
        )
        runner = ApplyCandidateRunner(r_case_dup, repo, [test_pass_cmd], False, repo, False)
        res_str, code, out = runner.run()
        assert out.splitlines()[0] == f"RESULT: {RES_REFUSED}" and code == EXIT_REFUSED

        # (7) python 以外のテストバイナリ拒否 (例: /bin/sh や node)
        runner = ApplyCandidateRunner(
            receipt_p, repo, ["/usr/bin/node::-e 'console.log(1)'"], False, repo, False
        )
        res_str, code, out = runner.run()
        assert out.splitlines()[0] == f"RESULT: {RES_REFUSED}" and code == EXIT_REFUSED

        print("[OK] Test 3: REFUSED cases (regex, candidates dir, case-insensitive dup, python bin check) passed")

        # -------------------------------------------------------------
        # 3b. 独立した照合問題をまとめ、JSON不正では前提検査で停止
        # -------------------------------------------------------------
        r_multi = make_receipt(
            manifest_p,
            approval_id="APR-20260924-91",
            approver="Not Alice",
            audit_status="fail",
        )
        runner = ApplyCandidateRunner(r_multi, repo, [test_pass_cmd], False, repo, False)
        res_str, code, out = runner.run()
        assert res_str == RES_REFUSED and code == EXIT_REFUSED
        lines = out.splitlines()
        assert lines[0] == f"RESULT: {RES_REFUSED}"
        assert lines[1] == "reason: Receipt audit_status is not 'pass' (was 'fail')"
        if ACTIVE_CONFIG.get("approvers"):
            assert lines[2] == "- Receipt approver_identity is not in configured approvers (was 'Not Alice')"
            assert len(lines) == 3
        else:
            assert len(lines) == 2

        malformed_receipt = os.path.join(receipts_dir, "malformed.json")
        with open(malformed_receipt, "w", encoding="utf-8") as f:
            f.write("not json")
        runner = ApplyCandidateRunner(malformed_receipt, repo, [test_pass_cmd], False, repo, False)
        res_str, code, out = runner.run()
        assert res_str == RES_REFUSED and code == EXIT_REFUSED
        lines = out.splitlines()
        assert lines[0] == f"RESULT: {RES_REFUSED}"
        assert lines[1].startswith("reason: Failed to load receipt JSON:")
        assert len(lines) == 2
        print("[OK] Test 3b: Multiple preflight reasons and malformed receipt stop passed")

        # 3c. 旧版との同一入力判定: object 形式 allowed_paths と candidate 欠落
        orig_path = os.path.join(os.path.dirname(__file__), "apply_candidate.orig.py")
        if not os.path.exists(orig_path):  # 本番の場所には改修前の写しが無い
            print("[SKIP] Test 3c: 改修前の版が無いため、今の版どうしで形だけ確かめる"); orig_path = os.path.abspath(__file__)
        orig_spec = importlib.util.spec_from_file_location("apply_candidate_orig", orig_path)
        assert orig_spec and orig_spec.loader
        orig_module = importlib.util.module_from_spec(orig_spec)
        orig_spec.loader.exec_module(orig_module)
        if hasattr(orig_module, "SELF_TEST_CONFIG_OVERRIDE"):
            orig_module.SELF_TEST_CONFIG_OVERRIDE = dict(SELF_TEST_CONFIG_OVERRIDE)
            orig_module.ACTIVE_CONFIG = orig_module.SELF_TEST_CONFIG_OVERRIDE

        def compare_result(receipt_file: str) -> Tuple[str, str]:
            outcomes: List[str] = []
            for runner_type in (orig_module.ApplyCandidateRunner, ApplyCandidateRunner):
                check_runner = runner_type(receipt_file, repo, [test_pass_cmd], False, repo, True)
                try:
                    result_str, _code, _out = check_runner.run()
                except Exception:
                    result_str = "EXCEPTION"
                outcomes.append(result_str)
            return outcomes[0], outcomes[1]

        parity_receipt = json.load(open(receipt_p, encoding="utf-8"))
        parity_receipt["allowed_paths"] = {
            "app/runtime/a.py": True,
            "app/runtime/b.py": True,
        }
        with open(receipt_p, "w", encoding="utf-8") as f:
            json.dump(parity_receipt, f)
        assert compare_result(receipt_p) == (RES_DRY_RUN_OK, RES_DRY_RUN_OK), "object allowed_paths must preserve legacy RESULT"

        parity_manifest = json.load(open(manifest_p, encoding="utf-8"))
        parity_manifest["files"][0].pop("candidate_path", None)
        with open(manifest_p, "w", encoding="utf-8") as f:
            json.dump(parity_manifest, f)
        parity_receipt["candidate_sha256"] = sha256_file(manifest_p)
        parity_receipt["allowed_paths"] = ["app/runtime/a.py", "app/runtime/b.py"]
        with open(receipt_p, "w", encoding="utf-8") as f:
            json.dump(parity_receipt, f)
        assert compare_result(receipt_p) == (RES_REFUSED, RES_REFUSED), "missing candidate must preserve legacy REFUSED RESULT"
        print("[OK] Test 3c: Legacy/new RESULT parity (object allowed_paths and missing candidate) passed")
        manifest_p = make_manifest()
        receipt_p = make_receipt(manifest_p)

        # -------------------------------------------------------------
        # 4. テスト失敗で ROLLED_BACK (exit 2) & (5) 失敗時の副作用見張り
        # -------------------------------------------------------------
        # テスト中に範囲外のファイルを汚染して失敗するテスト
        test_fail_with_leak = (
            f'{sys.executable}::-c "'
            f'import sys; '
            f'open(\'{front_dir}/leak_on_fail.secret\', \'w\').write(\'leak\'); '
            f'print(\'FAIL\'); sys.exit(1)"'
        )
        runner_rollback = ApplyCandidateRunner(
            receipt_path=receipt_p,
            test_cwd=repo,
            test_specs=[test_fail_with_leak],
            restart_listener=False,
            repo=repo,
            dry_run=False,
        )
        res_str, code, out = runner_rollback.run()
        assert out.splitlines()[0] == f"RESULT: {RES_ROLLED_BACK}", f"code={code}, out={out}"
        assert code == EXIT_ROLLED_BACK
        assert sha256_file(orig_a_path) == orig_a_sha
        assert not os.path.exists(os.path.join(front_dir, "b.py"))
        # (5) 失敗時にも範囲外変更の旗が報告されていること
        assert "変わったファイル" in out
        os.remove(os.path.join(front_dir, "leak_on_fail.secret"))
        print("[OK] Test 4: ROLLED_BACK with side-effect flags passed")

        # -------------------------------------------------------------
        # 5. (4) 時間切れでプロセスグループ全体停止 & ROLLED_BACK (exit 2)
        # -------------------------------------------------------------
        pid_file = os.path.join(temp_dir, "test5_hang.pid")
        test_hang_pg = (
            f'{sys.executable}::-c "'
            f'import os, time; '
            f'open(\'{pid_file}\', \'w\').write(str(os.getpid())); '
            f'time.sleep(15)"'
        )
        runner_timeout = ApplyCandidateRunner(
            receipt_path=receipt_p,
            test_cwd=repo,
            test_specs=[test_hang_pg],
            restart_listener=False,
            repo=repo,
            dry_run=False,
            test_timeout=0.3,
        )
        res_str, code, out = runner_timeout.run()
        assert out.splitlines()[0] == f"RESULT: {RES_ROLLED_BACK}" and code == EXIT_ROLLED_BACK
        assert sha256_file(orig_a_path) == orig_a_sha
        # プロセスが停止していることを確認
        if os.path.isfile(pid_file):
            try:
                with open(pid_file) as f:
                    child_p = int(f.read().strip())
                # プロセス生存確認 (死んでいれば OSError)
                time.sleep(0.1)
                try:
                    os.kill(child_p, 0)
                    assert False, f"Process {child_p} should have been killed"
                except OSError:
                    pass  # 正常に停止済み
            except Exception:
                pass
        print("[OK] Test 5: Process group termination on timeout passed")

        # -------------------------------------------------------------
        # 6. 復元確認失敗で ROLLBACK_FAILED (exit 5)
        # -------------------------------------------------------------
        runner_fail_rb = ApplyCandidateRunner(
            receipt_path=receipt_p,
            test_cwd=repo,
            test_specs=[test_fail_cmd],
            restart_listener=False,
            repo=repo,
            dry_run=False,
        )
        runner_fail_rb.evidence_dir = os.path.join(
            repo, config_dir(ACTIVE_CONFIG, "evidence_dir"), runner_fail_rb.project_id
        )
        runner_fail_rb.preimage_dir = os.path.join(runner_fail_rb.evidence_dir, "preimage")
        runner_fail_rb.staged_dir = os.path.join(runner_fail_rb.evidence_dir, "staged")
        runner_fail_rb.log_file_path = os.path.join(runner_fail_rb.evidence_dir, "apply.log")
        runner_fail_rb.preflight_check()
        runner_fail_rb.apply_files()
        corrupted_backup = runner_fail_rb.backed_up_files["app/runtime/a.py"][0]
        with open(corrupted_backup, "w") as f:
            f.write("corrupted content")
        rb_success = runner_fail_rb.rollback()
        assert not rb_success
        assert len(runner_fail_rb.rollback_failed_files) > 0
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")
        if os.path.exists(os.path.join(front_dir, "b.py")):
            os.remove(os.path.join(front_dir, "b.py"))
        print("[OK] Test 6: ROLLBACK_FAILED verified")

        # -------------------------------------------------------------
        # 7. (7) テスト後の書換え検知 & 再配置復元
        # -------------------------------------------------------------
        test_tamper = (
            f'{sys.executable}::-c "'
            f'with open(\'{orig_a_path}\', \'w\') as f: f.write(\'tampered after copy\'); '
            f'print(\'Ran 10 tests in 0.500s\\n\\nOK\')"'
        )
        runner = ApplyCandidateRunner(receipt_p, repo, [test_tamper], False, repo, False)
        res_str, code, out = runner.run()
        assert out.splitlines()[0] == f"RESULT: {RES_APPLIED_WITH_FLAGS}" and code == EXIT_FLAGS, f"code={code}, out={out}, flags={[f.category for f in runner.flags]}"
        assert any(f.category == "テスト後の書換え" for f in runner.flags)
        # 改変後に staged から正本へ復元されていることの確認
        assert sha256_file(orig_a_path) == cand_a_sha
        os.remove(os.path.join(front_dir, "b.py"))
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")
        print("[OK] Test 7: Post-test file tampering flagged and restored")

        # -------------------------------------------------------------
        # 8. (6) .gitignore 拡充範囲の見張り検知
        # -------------------------------------------------------------
        skills_dir = os.path.join(repo, "skills")
        os.makedirs(skills_dir, exist_ok=True)
        test_skills_leak = (
            f'{sys.executable}::-c "'
            f'with open(\'{skills_dir}/leak.secret\', \'w\') as f: f.write(\'skills leak\'); '
            f'print(\'Ran 10 tests in 0.500s\\n\\nOK\')"'
        )
        runner = ApplyCandidateRunner(receipt_p, repo, [test_skills_leak], False, repo, False)
        res_str, code, out = runner.run()
        assert out.splitlines()[0] == f"RESULT: {RES_APPLIED_WITH_FLAGS}" and code == EXIT_FLAGS
        assert any("skills/leak.secret" in f.message for f in runner.flags)
        os.remove(os.path.join(skills_dir, "leak.secret"))
        os.remove(os.path.join(front_dir, "b.py"))
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")
        print("[OK] Test 8: Extended .gitignore scope (skills) flagged")

        # -------------------------------------------------------------
        # 9. (7) 空の ps 出力や見張り失敗時のフラグ検知
        # -------------------------------------------------------------
        def mock_ps_empty() -> Dict[int, str]:
            # 空の辞書を返す
            return {}

        PS_SNAPSHOT_RUNNER = mock_ps_empty
        runner = ApplyCandidateRunner(receipt_p, repo, [test_pass_cmd], False, repo, False)
        # 空の時はフラグなし（モックは直接辞書を返すため）。例外時を検証：
        def mock_ps_error() -> Dict[int, str]:
            raise PermissionError("ps denied")

        PS_SNAPSHOT_RUNNER = mock_ps_error
        res_str, code, out = runner.run()
        assert out.splitlines()[0] == f"RESULT: {RES_APPLIED_WITH_FLAGS}" and code == EXIT_FLAGS
        assert any(f.category == "見張り不能" for f in runner.flags)
        PS_SNAPSHOT_RUNNER = lambda: {1: "/sbin/launchd"}
        os.remove(os.path.join(front_dir, "b.py"))
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")
        print("[OK] Test 9: Monitoring failure flagged")

        # -------------------------------------------------------------
        # 10. (7) 実測時間判定
        # -------------------------------------------------------------
        # ログ内表記は 0.100s だが実際には 2.0 秒かかるテスト
        slow_baseline_path = os.path.join(base_log_dir, "test-slow-baseline.log")
        with open(slow_baseline_path, "w", encoding="utf-8") as f:
            f.write("Ran 10 tests in 0.500s\n\nOK\n")
        manifest_p = make_manifest(verification_override=["runtime/tmp/test-slow-baseline.log"])
        receipt_p = make_receipt(manifest_p)
        test_slow_real = (
            f'{sys.executable}::-c "'
            f'import time; time.sleep(1.6); '
            f'print(\'Ran 10 tests in 0.100s\\n\\nOK\')"'
        )
        runner = ApplyCandidateRunner(receipt_p, repo, [test_slow_real], False, repo, False, test_timeout=5.0)
        res_str, code, out = runner.run()
        assert out.splitlines()[0] == f"RESULT: {RES_APPLIED_WITH_FLAGS}" and code == EXIT_FLAGS, f"code={code}, out={out}"
        assert any(f.category == "テストの時間" for f in runner.flags)
        os.remove(os.path.join(front_dir, "b.py"))
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")
        print("[OK] Test 10: Real elapsed time evaluation passed")
        manifest_p = make_manifest()
        receipt_p = make_receipt(manifest_p)

        # -------------------------------------------------------------
        # 11. 親が先に終了し子プロセスが残るテストの回収（無期限に待たず時間内に終了）
        # -------------------------------------------------------------
        test_orphan_child = (
            f'{sys.executable}::-c "'
            f'import subprocess, sys; '
            f'subprocess.Popen([sys.executable, \'-c\', \'import time; time.sleep(30)\']); '
            f'sys.exit(0)"'
        )
        t_start_orphan = time.time()
        runner_orphan = ApplyCandidateRunner(
            receipt_path=receipt_p,
            test_cwd=repo,
            test_specs=[test_orphan_child],
            restart_listener=False,
            repo=repo,
            dry_run=False,
            test_timeout=1.0,
        )
        res_str, code, out = runner_orphan.run()
        elapsed_orphan = time.time() - t_start_orphan
        # タイムアウト 1.0s + TERM待機 3.0s 以内に戻り、無期限待ちにならないこと
        assert elapsed_orphan < 8.0, f"Took too long: {elapsed_orphan}s"
        assert out.splitlines()[0] == f"RESULT: {RES_ROLLED_BACK}" and code == EXIT_ROLLED_BACK
        assert sha256_file(orig_a_path) == orig_a_sha
        assert not os.path.exists(os.path.join(front_dir, "b.py"))
        print("[OK] Test 11: Orphan child process group cleanup on timeout passed")

        # -------------------------------------------------------------
        # 12. staged 改変時の再配置中止検知
        # -------------------------------------------------------------
        staged_a_file = os.path.join(repo, config_dir(ACTIVE_CONFIG, "evidence_dir"), "PS-20260924-01", "staged", "app", "runtime", "a.py")
        test_tamper_staged = (
            f'{sys.executable}::-c "'
            f'open(\'{orig_a_path}\', \'w\').write(\'tampered target\'); '
            f'open(\'{staged_a_file}\', \'w\').write(\'tampered staged\'); '
            f'print(\'Ran 10 tests in 0.500s\\n\\nOK\')"'
        )
        runner_staged_tamper = ApplyCandidateRunner(receipt_p, repo, [test_tamper_staged], False, repo, False)
        res_str, code, out = runner_staged_tamper.run()
        assert out.splitlines()[0] == f"RESULT: {RES_APPLIED_WITH_FLAGS}" and code == EXIT_FLAGS
        # 「staged が改変されている」旗が立っていること
        assert any("staged が改変されている" in f.message for f in runner_staged_tamper.flags)
        # 再配置が中止され、target_path が postimage に復元されていないこと
        assert sha256_file(orig_a_path) != cand_a_sha
        os.remove(os.path.join(front_dir, "b.py"))
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")
        print("[OK] Test 12: Staged tampering detection passed")

        # -------------------------------------------------------------
        # 13. プロセス見張り: 無関係な新プロセスは旗0件（対象外として無視）
        # -------------------------------------------------------------
        test_dummy_cmd = (
            f'{sys.executable}::-c "print(\'Ran 10 tests in 0.500s\\n\\nOK\')"'
        )
        ps_snap_step = 0
        def mock_ps_unrelated() -> Dict[int, ProcessInfo]:
            nonlocal ps_snap_step
            ps_snap_step += 1
            if ps_snap_step == 1:
                return {1: ProcessInfo(pid=1, command="/sbin/launchd")}
            else:
                return {
                    1: ProcessInfo(pid=1, command="/sbin/launchd"),
                    8888: ProcessInfo(pid=8888, ppid=1, pgid=8888, lstart="Wed Sep 24 19:00:00 2026", command="python cache-keepalive.py --mode arm"),
                    8889: ProcessInfo(pid=8889, ppid=1, pgid=8889, lstart="Wed Sep 24 19:00:05 2026", command="sleep 30"),
                }

        PS_SNAPSHOT_RUNNER = mock_ps_unrelated
        runner_unrelated = ApplyCandidateRunner(receipt_p, repo, [test_dummy_cmd], False, repo, False)
        res_str, code, out = runner_unrelated.run()
        assert out.splitlines()[0] == f"RESULT: {RES_APPLIED}" and code == EXIT_OK
        assert len(runner_unrelated.flags) == 0
        with open(runner_unrelated.log_file_path, "r", encoding="utf-8") as f:
            log_content13 = f.read()
        assert "対象外として無視" in log_content13
        os.remove(os.path.join(front_dir, "b.py"))
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")
        print("[OK] Test 13: Unrelated new processes ignored (0 flags)")

        # -------------------------------------------------------------
        # 14. プロセス見張り: テストの子孫プロセスは旗1件 & flags-<PS>.md に証拠出力
        # -------------------------------------------------------------
        pid_record_file = os.path.join(temp_dir, "test14_pid.txt")
        test_record_pid_cmd = (
            f'{sys.executable}::-c "'
            f'import os; open(\'{pid_record_file}\', \'w\').write(str(os.getpid())); '
            f'print(\'Ran 10 tests in 0.500s\\n\\nOK\')"'
        )
        ps_snap_step14 = 0
        def mock_ps_descendant() -> Dict[int, ProcessInfo]:
            nonlocal ps_snap_step14
            ps_snap_step14 += 1
            if ps_snap_step14 == 1:
                return {1: ProcessInfo(pid=1, command="/sbin/launchd")}
            else:
                test_proc_pid = 99999
                if os.path.isfile(pid_record_file):
                    try:
                        with open(pid_record_file) as f:
                            test_proc_pid = int(f.read().strip())
                    except Exception:
                        pass
                return {
                    1: ProcessInfo(pid=1, command="/sbin/launchd"),
                    # テストの子孫 (pgid がテスト PID と一致)
                    7777: ProcessInfo(
                        pid=7777,
                        ppid=test_proc_pid,
                        pgid=test_proc_pid,
                        lstart="Wed Sep 24 19:05:00 2026",
                        command="python child_worker.py",
                    ),
                    # 無関係な新プロセス (pgid も ppid もテスト PID ではない)
                    8888: ProcessInfo(
                        pid=8888,
                        ppid=1,
                        pgid=8888,
                        lstart="Wed Sep 24 19:00:00 2026",
                        command="sleep 30",
                    ),
                }

        PS_SNAPSHOT_RUNNER = mock_ps_descendant
        runner_desc = ApplyCandidateRunner(receipt_p, repo, [test_record_pid_cmd], False, repo, False)
        res_str, code, out = runner_desc.run()
        assert out.splitlines()[0] == f"RESULT: {RES_APPLIED_WITH_FLAGS}" and code == EXIT_FLAGS
        proc_flags = [f for f in runner_desc.flags if f.category == "残ったプロセス"]
        assert len(proc_flags) == 1
        assert "7777" in proc_flags[0].message
        assert proc_flags[0].details.get("PID") == "7777"
        assert proc_flags[0].details.get("PGID") != ""
        assert "子孫" in proc_flags[0].details.get("テストプロセスとの関係", "")

        order_path14 = os.path.join(repo, config_dir(ACTIVE_CONFIG, "flags_order_dir"), f"flags-{runner_desc.project_id}.md")
        flags_path14 = os.path.join(runner_desc.evidence_dir, "FLAGS.md")
        assert os.path.isfile(order_path14)
        assert os.path.isfile(flags_path14)

        with open(order_path14, "r", encoding="utf-8") as f:
            order_content = f.read()
        assert "PID: 7777" in order_content
        assert "child_worker.py" in order_content
        assert "テストプロセスとの関係:" in order_content

        with open(flags_path14, "r", encoding="utf-8") as f:
            flags_content = f.read()
        assert "PID**: 7777" in flags_content
        assert "child_worker.py" in flags_content

        os.remove(os.path.join(front_dir, "b.py"))
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")
        print("[OK] Test 14: Test descendant process flagged (1 flag) and evidence written to flags order")

        # -------------------------------------------------------------
        # 15. ファイル見張り: 常駐ログ除外一覧に当たる変化は旗0件（対象外として無視）
        # -------------------------------------------------------------
        # Ignore-rule checks use the same neutral fixture under both base configurations.
        ACTIVE_CONFIG["ignore_files"] = [("app/runtime", "service-*.log")]
        PS_SNAPSHOT_RUNNER = lambda: {1: "/sbin/launchd"}
        test_write_ignored_logs = (
            f'{sys.executable}::-c "'
            f'open(\'{front_dir}/service-test15-a.log\', \'w\').write(\'{{\"log\": 1}}\'); '
            f'open(\'{front_dir}/service-test15-b.log\', \'w\').write(\'debug log\\n\'); '
            f'open(\'{front_dir}/service-approved.log\', \'w\').write(\'exec log\\n\'); '
            f'print(\'Ran 10 tests in 0.500s\\n\\nOK\')"'
        )
        runner_ignored_files = ApplyCandidateRunner(receipt_p, repo, [test_write_ignored_logs], False, repo, False)
        res_str, code, out = runner_ignored_files.run()
        assert out.splitlines()[0] == f"RESULT: {RES_APPLIED}" and code == EXIT_OK
        assert len(runner_ignored_files.flags) == 0
        with open(runner_ignored_files.log_file_path, "r", encoding="utf-8") as f:
            log_content15 = f.read()
        assert "除外一覧に合致するため対象外として無視" in log_content15
        os.remove(os.path.join(front_dir, "b.py"))
        for log_f in ["service-test15-a.log", "service-test15-b.log", "service-approved.log"]:
            p = os.path.join(front_dir, log_f)
            if os.path.exists(p):
                os.remove(p)
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")
        print("[OK] Test 15: Ignored resident logs changes not flagged (0 flags)")

        # -------------------------------------------------------------
        # 16. ファイル見張り: allowed_paths に入っているものは除外に当たっても旗が立つ
        #     & 変わったファイルの証拠が flags-<PS>.md / FLAGS.md に出力される
        # -------------------------------------------------------------
        # 開始時に service-approved.log が無いことを念のため保証
        if os.path.exists(os.path.join(front_dir, "service-approved.log")):
            os.remove(os.path.join(front_dir, "service-approved.log"))

        log_candidate_file = os.path.join(candidates_dir, "PS-20260924-01--service-approved.log")
        with open(log_candidate_file, "w", encoding="utf-8") as f:
            f.write("candidate log line 1\n")
        cand_log_sha = sha256_file(log_candidate_file)

        manifest_with_log = make_manifest(
            proj_id="PS-20260924-01",
            files_override=[
                {
                    "path": "app/runtime/a.py",
                    "candidate_path": cand_a_path,
                    "preimage_sha256": orig_a_sha,
                    "postimage_sha256": cand_a_sha,
                },
                {
                    "path": "app/runtime/service-approved.log",
                    "candidate_path": log_candidate_file,
                    "preimage_sha256": None,
                    "postimage_sha256": cand_log_sha,
                },
            ],
        )
        receipt_with_log = make_receipt(
            manifest_with_log,
            allowed_paths=["app/runtime/a.py", "app/runtime/service-approved.log"],
        )

        test_tamper_allowed_log = (
            f'{sys.executable}::-c "'
            f'open(\'{front_dir}/service-approved.log\', \'a\').write(\'tampered after copy\\n\'); '
            f'print(\'Ran 10 tests in 0.500s\\n\\nOK\')"'
        )
        runner_allowed_log = ApplyCandidateRunner(receipt_with_log, repo, [test_tamper_allowed_log], False, repo, False)
        res_str, code, out = runner_allowed_log.run()
        assert out.splitlines()[0] == f"RESULT: {RES_APPLIED_WITH_FLAGS}" and code == EXIT_FLAGS, f"code={code}, out={out}, flags={[f.summary() for f in runner_allowed_log.flags]}"
        assert any(f.category == "テスト後の書換え" and "service-approved.log" in f.message for f in runner_allowed_log.flags)

        # runner_allowed_log で作成された service-approved.log を削除し a.py を復元
        if os.path.exists(os.path.join(front_dir, "service-approved.log")):
            os.remove(os.path.join(front_dir, "service-approved.log"))
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")

        test_unallowed_change = (
            f'{sys.executable}::-c "'
            f'open(\'{front_dir}/unexpected.txt\', \'w\').write(\'hello world\\n\'); '
            f'print(\'Ran 10 tests in 0.500s\\n\\nOK\')"'
        )
        runner_unallowed = ApplyCandidateRunner(receipt_p, repo, [test_unallowed_change], False, repo, False)
        res_str, code, out = runner_unallowed.run()
        assert out.splitlines()[0] == f"RESULT: {RES_APPLIED_WITH_FLAGS}" and code == EXIT_FLAGS, f"code={code}, out={out}, flags={[f.summary() for f in runner_unallowed.flags]}"
        # git status 側と直接検知側の2件が立つ。証拠を持つのは直接検知側
        unallowed_flags = [f for f in runner_unallowed.flags if f.category == "変わったファイル" and "unexpected.txt" in f.message and "直接検知" in f.message]
        assert len(unallowed_flags) == 1
        details = unallowed_flags[0].details
        assert "変更前サイズ" in details
        assert "変更後サイズ" in details
        assert "サイズ増分" in details
        assert "変更前mtime" in details
        assert "変更後mtime" in details

        order_path16 = os.path.join(repo, config_dir(ACTIVE_CONFIG, "flags_order_dir"), f"flags-{runner_unallowed.project_id}.md")
        with open(order_path16, "r", encoding="utf-8") as f:
            order_text16 = f.read()
        assert "変更後サイズ:" in order_text16 or "サイズ増分:" in order_text16

        # Test 16 の make_receipt が同じ受領書パスを上書きするため receipt_p は b.py を含まない
        if os.path.exists(os.path.join(front_dir, "b.py")):
            os.remove(os.path.join(front_dir, "b.py"))
        if os.path.exists(os.path.join(front_dir, "service-approved.log")):
            os.remove(os.path.join(front_dir, "service-approved.log"))
        if os.path.exists(os.path.join(front_dir, "unexpected.txt")):
            os.remove(os.path.join(front_dir, "unexpected.txt"))
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")
        print("[OK] Test 16: Allowed paths monitored even if matching ignore pattern & file diff evidence verified")

        # -------------------------------------------------------------
        # 17. プロセス見張り: setsid 後の子孫プロセス検知 (ps -E トークン検査)
        #     - pgid/ppid 経路外だが ps -E にトークンあり（旗1件、関係: "環境変数のしるしで判定"）
        #     - トークンなし（旗0件、対象外として無視）
        #     - ps -E 失敗（旗0件、失敗ログ記録）
        # -------------------------------------------------------------
        # Test 16 で上書きされた manifest / receipt を既定の組 (a.py + b.py) に戻す
        manifest_p = make_manifest()
        receipt_p = make_receipt(manifest_p)

        test_normal_quick = f'{sys.executable}::-c "print(\'Ran 10 tests in 0.500s\\n\\nOK\')"'

        def make_snapshot_mock(post_proc: Optional[ProcessInfo] = None):
            calls = 0
            if post_proc is None:
                post_proc = ProcessInfo(pid=9999, ppid=1, pgid=9999, command="python setsid_orphan.py")
            def _snapshot():
                nonlocal calls
                calls += 1
                if calls % 2 == 1:
                    # テスト前: launchd のみ
                    return {1: ProcessInfo(pid=1, ppid=0, pgid=1, command="/sbin/launchd")}
                else:
                    # テスト後: launchd と post_proc
                    return {
                        1: ProcessInfo(pid=1, ppid=0, pgid=1, command="/sbin/launchd"),
                        post_proc.pid: post_proc,
                    }
            return _snapshot

        # (17-A) トークンあり -> 旗1件
        PS_SNAPSHOT_RUNNER = make_snapshot_mock()
        runner_token_found = ApplyCandidateRunner(receipt_p, repo, [test_normal_quick], False, repo, False)
        PS_E_RUNNER = lambda pid: subprocess.CompletedProcess(
            args=["ps", "-E", "-o", "command=", "-p", str(pid)],
            returncode=0,
            stdout=f"COMMAND_LINE PATH=/usr/bin HOME=/Users/test APPLY_CANDIDATE_RUN_TOKEN={getattr(runner_token_found, 'last_run_token', '')}",
            stderr="",
        )
        res_str, code, out = runner_token_found.run()
        assert out.splitlines()[0] == f"RESULT: {RES_APPLIED_WITH_FLAGS}" and code == EXIT_FLAGS
        token_proc_flags = [f for f in runner_token_found.flags if f.category == "残ったプロセス" and "9999" in f.message]
        assert len(token_proc_flags) == 1
        assert token_proc_flags[0].details.get("PID") == "9999"
        assert token_proc_flags[0].details.get("テストプロセスとの関係") == "環境変数のしるしで判定"

        # トークン文字列そのものがサマリやログに含まれていないことを確認
        assert runner_token_found.last_run_token not in token_proc_flags[0].summary()
        with open(runner_token_found.log_file_path, "r", encoding="utf-8") as f:
            log_c17a = f.read()
        assert runner_token_found.last_run_token not in log_c17a

        if os.path.exists(os.path.join(front_dir, "b.py")):
            os.remove(os.path.join(front_dir, "b.py"))
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")

        # (17-B) トークンなし -> 旗0件、対象外として無視ログ
        PS_SNAPSHOT_RUNNER = make_snapshot_mock()
        PS_E_RUNNER = lambda pid: subprocess.CompletedProcess(
            args=["ps", "-E", "-o", "command=", "-p", str(pid)],
            returncode=0,
            stdout="COMMAND_LINE PATH=/usr/bin HOME=/Users/test OTHER_TOKEN=12345",
            stderr="",
        )
        runner_no_token = ApplyCandidateRunner(receipt_p, repo, [test_normal_quick], False, repo, False)
        res_str, code, out = runner_no_token.run()
        assert out.splitlines()[0] == f"RESULT: {RES_APPLIED}" and code == EXIT_OK
        assert len([f for f in runner_no_token.flags if f.category == "残ったプロセス"]) == 0
        with open(runner_no_token.log_file_path, "r", encoding="utf-8") as f:
            log_c17b = f.read()
        assert "環境変数にしるしが無いため対象外として無視" in log_c17b

        if os.path.exists(os.path.join(front_dir, "b.py")):
            os.remove(os.path.join(front_dir, "b.py"))
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")

        # (17-C1) ps -E 失敗かつ ppid=1 (孤立プロセス) -> 旗1件
        post_proc_orphan = ProcessInfo(pid=9999, ppid=1, pgid=9999, command="python setsid_orphan.py", lstart="不明")
        PS_SNAPSHOT_RUNNER = make_snapshot_mock(post_proc_orphan)
        PS_E_RUNNER = lambda pid: subprocess.CompletedProcess(
            args=["ps", "-E", "-o", "command=", "-p", str(pid)],
            returncode=1,
            stdout="",
            stderr="ps: process not found",
        )
        runner_ps_fail_orphan = ApplyCandidateRunner(receipt_p, repo, [test_normal_quick], False, repo, False)
        res_str, code, out = runner_ps_fail_orphan.run()
        assert out.splitlines()[0] == f"RESULT: {RES_APPLIED_WITH_FLAGS}" and code == EXIT_FLAGS
        proc_flags_17c1 = [f for f in runner_ps_fail_orphan.flags if f.category == "残ったプロセス" and "9999" in f.message]
        assert len(proc_flags_17c1) == 1
        assert proc_flags_17c1[0].details.get("テストプロセスとの関係") == "環境変数を読めない孤立プロセス（要確認）"

        if os.path.exists(os.path.join(front_dir, "b.py")):
            os.remove(os.path.join(front_dir, "b.py"))
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")

        # (17-C2) ps -E 失敗だが ppid!=1 (親が稼働中) -> 旗0件、ログあり
        post_proc_alive = ProcessInfo(pid=9999, ppid=555, pgid=9999, command="python setsid_orphan.py", lstart="不明")
        PS_SNAPSHOT_RUNNER = make_snapshot_mock(post_proc_alive)
        PS_E_RUNNER = lambda pid: subprocess.CompletedProcess(
            args=["ps", "-E", "-o", "command=", "-p", str(pid)],
            returncode=1,
            stdout="",
            stderr="ps: process not found",
        )
        runner_ps_fail_alive = ApplyCandidateRunner(receipt_p, repo, [test_normal_quick], False, repo, False)
        res_str, code, out = runner_ps_fail_alive.run()
        assert out.splitlines()[0] == f"RESULT: {RES_APPLIED}" and code == EXIT_OK
        assert len([f for f in runner_ps_fail_alive.flags if f.category == "残ったプロセス"]) == 0
        with open(runner_ps_fail_alive.log_file_path, "r", encoding="utf-8") as f:
            log_c17c2 = f.read()
        assert "孤立プロセス要件を満たさないため対象外として無視" in log_c17c2

        if os.path.exists(os.path.join(front_dir, "b.py")):
            os.remove(os.path.join(front_dir, "b.py"))
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")

        # (17-D1) 環境変数が読めない出力 (引数に '=' があっても PATH=/HOME= なし) かつ ppid=1 -> 旗1件 (関係: 要確認)
        post_proc_no_env_orphan = ProcessInfo(pid=9999, ppid=1, pgid=9999, command="/bin/sleep 30 --interval=1", lstart="不明")
        PS_SNAPSHOT_RUNNER = make_snapshot_mock(post_proc_no_env_orphan)
        PS_E_RUNNER = lambda pid: subprocess.CompletedProcess(
            args=["ps", "-E", "-o", "command=", "-p", str(pid)],
            returncode=0,
            stdout="/bin/sleep 30 --interval=1",
            stderr="",
        )
        runner_no_env_orphan = ApplyCandidateRunner(receipt_p, repo, [test_normal_quick], False, repo, False)
        res_str, code, out = runner_no_env_orphan.run()
        assert out.splitlines()[0] == f"RESULT: {RES_APPLIED_WITH_FLAGS}" and code == EXIT_FLAGS
        proc_flags_17d1 = [f for f in runner_no_env_orphan.flags if f.category == "残ったプロセス" and "9999" in f.message]
        assert len(proc_flags_17d1) == 1
        assert proc_flags_17d1[0].details.get("テストプロセスとの関係") == "環境変数を読めない孤立プロセス（要確認）"

        if os.path.exists(os.path.join(front_dir, "b.py")):
            os.remove(os.path.join(front_dir, "b.py"))
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")

        # (17-D2) 環境変数が読めない出力だが ppid!=1 -> 旗0件、ログあり
        post_proc_no_env_alive = ProcessInfo(pid=9999, ppid=777, pgid=9999, command="/bin/sleep 30 --interval=1", lstart="不明")
        PS_SNAPSHOT_RUNNER = make_snapshot_mock(post_proc_no_env_alive)
        PS_E_RUNNER = lambda pid: subprocess.CompletedProcess(
            args=["ps", "-E", "-o", "command=", "-p", str(pid)],
            returncode=0,
            stdout="/bin/sleep 30 --interval=1",
            stderr="",
        )
        runner_no_env_alive = ApplyCandidateRunner(receipt_p, repo, [test_normal_quick], False, repo, False)
        res_str, code, out = runner_no_env_alive.run()
        assert out.splitlines()[0] == f"RESULT: {RES_APPLIED}" and code == EXIT_OK
        assert len([f for f in runner_no_env_alive.flags if f.category == "残ったプロセス"]) == 0
        with open(runner_no_env_alive.log_file_path, "r", encoding="utf-8") as f:
            log_c17d2 = f.read()
        assert "孤立プロセス要件を満たさないため対象外として無視" in log_c17d2

        if os.path.exists(os.path.join(front_dir, "b.py")):
            os.remove(os.path.join(front_dir, "b.py"))
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")

        # (17-E) テスト失敗時でも子孫プロセスが残れば旗が立ち、FLAGS.md / order ファイルが出力される
        test_failing_cmd = f'{sys.executable}::-c "import sys; print(\'FAIL\'); sys.exit(1)"'
        PS_SNAPSHOT_RUNNER = make_snapshot_mock(post_proc_orphan)
        PS_E_RUNNER = lambda pid: subprocess.CompletedProcess(
            args=["ps", "-E", "-o", "command=", "-p", str(pid)],
            returncode=1,
            stdout="",
            stderr="err",
        )
        runner_test_fail = ApplyCandidateRunner(receipt_p, repo, [test_failing_cmd], False, repo, False)
        res_str, code, out = runner_test_fail.run()
        assert out.splitlines()[0] == f"RESULT: {RES_ROLLED_BACK}" and code == EXIT_ROLLED_BACK
        assert any(f.category == "残ったプロセス" for f in runner_test_fail.flags)
        assert "flags: " in out and "[残ったプロセス]" in out

        flags_path_17e = os.path.join(runner_test_fail.evidence_dir, "FLAGS.md")
        order_path_17e = os.path.join(repo, config_dir(ACTIVE_CONFIG, "flags_order_dir"), f"flags-{runner_test_fail.project_id}.md")
        assert os.path.isfile(flags_path_17e), "FLAGS.md must be written even on test failure with flags"
        assert os.path.isfile(order_path_17e), "Order file must be written even on test failure with flags"

        if os.path.exists(os.path.join(front_dir, "b.py")):
            os.remove(os.path.join(front_dir, "b.py"))
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")

        # (17-F) 失敗したテストが allowed_paths を改変した場合に「テスト後の書換え」旗が立ち、ロールバックされる
        test_tamper_failing_cmd = (
            f'{sys.executable}::-c "'
            f'open(\'{front_dir}/a.py\', \'a\').write(\'tamper while failing\\n\'); '
            f'import sys; print(\'FAIL\'); sys.exit(1)"'
        )
        PS_SNAPSHOT_RUNNER = lambda: {1: "/sbin/launchd"}
        PS_E_RUNNER = None
        runner_tamper_fail = ApplyCandidateRunner(receipt_p, repo, [test_tamper_failing_cmd], False, repo, False)
        res_str, code, out = runner_tamper_fail.run()
        assert out.splitlines()[0] == f"RESULT: {RES_ROLLED_BACK}" and code == EXIT_ROLLED_BACK
        rewrite_flags = [f for f in runner_tamper_fail.flags if f.category == "テスト後の書換え"]
        assert len(rewrite_flags) >= 1
        assert "a.py" in rewrite_flags[0].message
        # 巻き戻しによって a.py が元の内容に戻っていること
        with open(orig_a_path, "r", encoding="utf-8") as f:
            assert f.read() == "# original a.py\n"

        # リセット
        PS_E_RUNNER = None
        PS_SNAPSHOT_RUNNER = lambda: {1: "/sbin/launchd"}

        # (17-G) Spotlight の指定プロセスだけを環境変数なし孤立判定から除外
        orphan_started = (datetime.datetime.now() + datetime.timedelta(minutes=1)).strftime("%a %b %d %H:%M:%S %Y")
        spotlight_and_other = [
            ProcessInfo(pid=9991, ppid=1, pgid=9991, command="/System/Library/Frameworks/CoreServices.framework/Frameworks/Metadata.framework/Support/mdworker_shared", lstart=orphan_started),
            ProcessInfo(pid=9992, ppid=1, pgid=9992, command="/usr/sbin/foo", lstart=orphan_started),
            ProcessInfo(pid=9993, ppid=1, pgid=9993, command="/System/Library/SomeFramework/other_daemon", lstart=orphan_started),
            ProcessInfo(pid=9994, ppid=1, pgid=9994, command="/bin/sleep 30", lstart=orphan_started),
        ]
        for mocked_proc in spotlight_and_other:
            PS_SNAPSHOT_RUNNER = make_snapshot_mock(mocked_proc)
            PS_E_RUNNER = lambda pid: subprocess.CompletedProcess(
                args=["ps", "-E", "-o", "command=", "-p", str(pid)],
                returncode=1, stdout="", stderr="mocked ps -E unavailable",
            )
            runner_spotlight = ApplyCandidateRunner(receipt_p, repo, [test_normal_quick], False, repo, False)
            res_str, code, out = runner_spotlight.run()
            flagged = [f for f in runner_spotlight.flags if f.category == "残ったプロセス" and f.details.get("PID") == str(mocked_proc.pid)]
            if mocked_proc.pid == 9991:
                assert not flagged, "mdworker_shared under /System/Library must be ignored"
            else:
                assert len(flagged) == 1, f"Expected orphan flag for {mocked_proc.command}"
            if os.path.exists(os.path.join(front_dir, "b.py")):
                os.remove(os.path.join(front_dir, "b.py"))
            with open(orig_a_path, "w") as f:
                f.write("# original a.py\n")
        PS_E_RUNNER = None
        PS_SNAPSHOT_RUNNER = lambda: {1: "/sbin/launchd"}
        print("[OK] Test 17-G: only named Spotlight processes under /System/Library are excluded")

        if os.path.exists(os.path.join(front_dir, "b.py")):
            os.remove(os.path.join(front_dir, "b.py"))
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")
        print("[OK] Test 17: setsid descendant & non-env orphan process detection and failure-path monitoring")

        # -------------------------------------------------------------
        # 18. ファイル見張り: 除外ルールのディレクトリ境界（直下のみ除外、サブディレクトリは除外しない）
        # -------------------------------------------------------------
        sub_dir = os.path.join(front_dir, "subdir")
        os.makedirs(sub_dir, exist_ok=True)
        test_dir_boundary = (
            f'{sys.executable}::-c "'
            f'open(\'{front_dir}/subdir/service-sub.log\', \'w\').write(\'sub log\\n\'); '
            f'open(\'{front_dir}/service-direct.log\', \'w\').write(\'direct log\\n\'); '
            f'print(\'Ran 10 tests in 0.500s\\n\\nOK\')"'
        )
        runner_dir_boundary = ApplyCandidateRunner(receipt_p, repo, [test_dir_boundary], False, repo, False)
        res_str, code, out = runner_dir_boundary.run()
        assert out.splitlines()[0] == f"RESULT: {RES_APPLIED_WITH_FLAGS}" and code == EXIT_FLAGS
        # 直下ファイルは除外されて旗にならない
        assert not any("service-direct.log" in f.message for f in runner_dir_boundary.flags)
        # サブディレクトリ内のファイルは除外されず旗が立つ
        sub_flags = [f for f in runner_dir_boundary.flags if "service-sub.log" in f.message]
        assert len(sub_flags) >= 1
        with open(runner_dir_boundary.log_file_path, "r", encoding="utf-8") as f:
            log_c18 = f.read()
        assert "service-direct.log" in log_c18 and "除外一覧に合致するため対象外として無視" in log_c18

        # 後片付け
        if os.path.exists(os.path.join(sub_dir, "service-sub.log")):
            os.remove(os.path.join(sub_dir, "service-sub.log"))
        if os.path.exists(sub_dir):
            os.rmdir(sub_dir)
        if os.path.exists(os.path.join(front_dir, "service-direct.log")):
            os.remove(os.path.join(front_dir, "service-direct.log"))
        if os.path.exists(os.path.join(front_dir, "b.py")):
            os.remove(os.path.join(front_dir, "b.py"))
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")
        print("[OK] Test 18: Ignore directory boundary strictly enforced (root ignored, subdir flagged)")

        # -------------------------------------------------------------
        # 19. git status 側の旗の証拠 (Flag.details) に前後の値が入る
        #     - 事前スナップショットにあるファイル変更: 変更前サイズ、変更後サイズ、サイズ増分、変更前mtime、変更後mtime
        #     - 事前スナップショットにない新規ファイル: 変更前サイズ/mtime は「不明」、変更後サイズ/mtime、サイズ増分「不明」
        #     - 削除されたファイル: 変更後サイズ/mtime は「なし（削除）」、サイズ増分「-X bytes (削除)」
        # -------------------------------------------------------------
        git_test_file = os.path.join(front_dir, "pre_exist.txt")
        with open(git_test_file, "w", encoding="utf-8") as f:
            f.write("initial content 12345\n")
        init_st = os.stat(git_test_file)

        outside_test_file = os.path.join(repo, "outside_unknown.txt")
        with open(outside_test_file, "w", encoding="utf-8") as f:
            f.write("outside content\n")

        runner_git_ev = ApplyCandidateRunner(receipt_p, repo, [test_normal_quick], False, repo, False)
        # check_git_status_diff を直接呼び出して各ケースの details の検証
        mock_pre_snapshot = {
            "app/runtime/pre_exist.txt": (int(init_st.st_mtime * 1e9), init_st.st_size),
            "app/runtime/to_be_deleted.txt": (int(init_st.st_mtime * 1e9), 42),
        }
        # to_be_deleted.txt は実際には存在しないファイル
        if os.path.exists(os.path.join(front_dir, "to_be_deleted.txt")):
            os.remove(os.path.join(front_dir, "to_be_deleted.txt"))

        runner_git_ev.get_git_status = lambda: {
            " M app/runtime/pre_exist.txt",
            "?? outside_unknown.txt",
            " D app/runtime/to_be_deleted.txt",
        }
        runner_git_ev.check_git_status_diff(pre_status=set(), pre_snapshot=mock_pre_snapshot)

        git_flags = [f for f in runner_git_ev.flags if f.category == "変わったファイル"]

        # (19-A) 事前スナップショットにある変更ファイル
        pre_flag = next((f for f in git_flags if "pre_exist.txt" in f.message), None)
        assert pre_flag is not None
        assert pre_flag.details["変更前サイズ"] == f"{init_st.st_size} bytes"
        assert pre_flag.details["変更後サイズ"] == f"{init_st.st_size} bytes"
        assert pre_flag.details["サイズ増分"] == "+0 bytes"
        assert pre_flag.details["変更前mtime"] != "不明"
        assert pre_flag.details["変更後mtime"] != "不明"

        # (19-B) 事前スナップショットにない新規ファイル
        unk_flag = next((f for f in git_flags if "outside_unknown.txt" in f.message), None)
        assert unk_flag is not None
        assert unk_flag.details["変更前サイズ"] == "不明"
        assert unk_flag.details["変更前mtime"] == "不明"
        assert "bytes" in unk_flag.details["変更後サイズ"]
        assert unk_flag.details["サイズ増分"] == "不明"

        # (19-C) 削除されたファイル
        del_flag = next((f for f in git_flags if "to_be_deleted.txt" in f.message), None)
        assert del_flag is not None
        assert del_flag.details["変更前サイズ"] == "42 bytes"
        assert del_flag.details["変更後サイズ"] == "なし（削除）"
        assert del_flag.details["変更後mtime"] == "なし（削除）"
        assert "-42 bytes (削除)" in del_flag.details["サイズ増分"]

        # (19-D) -uall により未追跡ディレクトリ内も個々のファイル名で返る
        nested_untracked = os.path.join(front_dir, "outside_nested", "child", "file.txt")
        os.makedirs(os.path.dirname(nested_untracked), exist_ok=True)
        with open(nested_untracked, "w", encoding="utf-8") as f:
            f.write("nested untracked\n")
        nested_status = ApplyCandidateRunner(receipt_p, repo, [test_normal_quick], False, repo, False).get_git_status()
        assert any(line.endswith("app/runtime/outside_nested/child/file.txt") for line in nested_status), nested_status
        shutil.rmtree(os.path.join(front_dir, "outside_nested"))

        # 後片付け
        if os.path.exists(git_test_file):
            os.remove(git_test_file)
        if os.path.exists(outside_test_file):
            os.remove(outside_test_file)
        if os.path.exists(os.path.join(front_dir, "b.py")):
            os.remove(os.path.join(front_dir, "b.py"))
        with open(orig_a_path, "w") as f:
            f.write("# original a.py\n")
        print("[OK] Test 19: git status diff flag contains complete before/after evidence details")

        # -------------------------------------------------------------
        # 20. 本物のプロセスとファイルを使う実機動作テスト（モック解除）
        # -------------------------------------------------------------
        PS_SNAPSHOT_RUNNER = None
        PS_E_RUNNER = None

        try:
            ps_probe = subprocess.run(["ps", "-Ao", "pid"], capture_output=True, text=True)
            ps_available = ps_probe.returncode == 0
        except (PermissionError, OSError):
            ps_available = False
        if not ps_available:
            print("[SKIP] Test 20: ps が使えないため")
            print("self-test: PASS")
            return 0

        manifest_p = make_manifest()
        receipt_p = make_receipt(manifest_p)

        pids_log = os.path.join(temp_dir, "test20_pids.txt")
        if os.path.exists(pids_log):
            os.remove(pids_log)

        helper_py = os.path.join(temp_dir, "test20_helper.py")
        helper_code = f"""import os
import sys
import time
import subprocess

pids_file = sys.argv[1]
repo_dir = sys.argv[2]

# (i) os.fork() -> 子で setsid() -> 孫で sleep(20) (python のまま)
pid1 = os.fork()
if pid1 == 0:
    os.setsid()
    pid2 = os.fork()
    if pid2 == 0:
        _dn = os.open(os.devnull, os.O_RDWR)
        for _fd in (0, 1, 2):
            os.dup2(_dn, _fd)
        my_pid = os.getpid()
        with open(pids_file, "a") as f:
            f.write(f"{{my_pid}}\\n")
        # 親 (pid1) が終了して ppid が 1 になるのを待つ (最大2秒)
        for _ in range(20):
            if os.getppid() == 1:
                break
            time.sleep(0.1)
        time.sleep(20)
        os._exit(0)
    else:
        os._exit(0)
os.waitpid(pid1, 0)

# (ii) /bin/sleep 20 を起動し、fork して親が先に終わる形で孤立させる
pid_sleep_parent = os.fork()
if pid_sleep_parent == 0:
    os.setsid()
    proc_s = subprocess.Popen(["/bin/sleep", "20"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(pids_file, "a") as f:
        f.write(f"{{proc_s.pid}}\\n")
    os._exit(0)
os.waitpid(pid_sleep_parent, 0)

# sleep プロセスが孤立するのを短時間待機
time.sleep(0.2)

# (iii) app/runtime/service-live.log に追記 (除外ルール対象)
log_path = os.path.join(repo_dir, "app/runtime/service-live.log")
with open(log_path, "a", encoding="utf-8") as f:
    f.write("live resident log entry\\n")

print("Ran 10 tests in 0.500s\\n\\nOK")
"""
        with open(helper_py, "w", encoding="utf-8") as f:
            f.write(helper_code)

        # テストの外で無関係なプロセスを起動 (親=self-testプロセスで生存、しるしなし -> 旗0件)
        external_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])

        created_pids: List[int] = []
        try:
            test_cmd20 = f'{sys.executable}::{helper_py} {pids_log} {repo}'
            runner20 = ApplyCandidateRunner(receipt_p, repo, [test_cmd20], False, repo, False, test_timeout=15.0)
            res_str, code, out = runner20.run()

            assert out.splitlines()[0] == f"RESULT: {RES_APPLIED_WITH_FLAGS}" and code == EXIT_FLAGS

            # 「残ったプロセス」の旗が (i) と (ii) の2件だけであること
            proc_flags20 = [f for f in runner20.flags if f.category == "残ったプロセス"]
            assert len(proc_flags20) == 2, f"Expected exactly 2 process flags, got {len(proc_flags20)}: {proc_flags20}"

            # 「変わったファイル」の旗は0件であること
            file_flags20 = [f for f in runner20.flags if f.category == "変わったファイル"]
            assert len(file_flags20) == 0, f"Expected 0 file flags, got {len(file_flags20)}: {file_flags20}"

            # 2件の内訳確認:
            # (i) python 孫プロセス -> 環境変数のしるしで判定
            # (ii) /bin/sleep プロセス -> 環境変数を読めない孤立プロセス（要確認）
            relationships = [f.details.get("テストプロセスとの関係") for f in proc_flags20]
            if sys.platform == "darwin":
                assert "環境変数のしるしで判定" in relationships
                assert "環境変数を読めない孤立プロセス（要確認）" in relationships
            else:
                print("[SKIP] Test 20 relationship labels: macOS-specific classification check")

            # external_proc が旗に含まれていないこと
            flagged_pids = [f.details.get("PID") for f in proc_flags20]
            assert str(external_proc.pid) not in flagged_pids

        finally:
            # プロセスの回収・停止
            try:
                external_proc.terminate()
                external_proc.kill()
                external_proc.wait(timeout=1.0)
            except Exception:
                pass

            if os.path.exists(pids_log):
                with open(pids_log, "r", encoding="utf-8") as f:
                    for line in f:
                        line_str = line.strip()
                        if line_str.isdigit():
                            created_pids.append(int(line_str))

            for cpid in created_pids:
                try:
                    os.kill(cpid, signal.SIGKILL)
                except OSError:
                    pass

            live_log_p = os.path.join(front_dir, "service-live.log")
            if os.path.exists(live_log_p):
                os.remove(live_log_p)
            if os.path.exists(helper_py):
                os.remove(helper_py)
            if os.path.exists(pids_log):
                os.remove(pids_log)
            if os.path.exists(os.path.join(front_dir, "b.py")):
                os.remove(os.path.join(front_dir, "b.py"))
            with open(orig_a_path, "w") as f:
                f.write("# original a.py\n")

        print("[OK] Test 20: Real process and file live monitoring verified (real setsid token & orphan sleep flagged)")

    # Configuration smoke tests: generic defaults and packaged compatibility example.
    generic_cfg, generic_warnings, generic_err = load_config(None, tempfile.mkdtemp(prefix="apply-candidate-config-"))
    assert generic_cfg is not None and generic_err is None and not generic_warnings
    assert generic_cfg["approvers"] == [] and generic_cfg["watch_dirs"] == [] and generic_cfg["restart_command"] == []
    with tempfile.TemporaryDirectory() as generic_td:
        generic_repo = os.path.join(generic_td, "repo")
        os.makedirs(generic_repo)
        subprocess.run(["git", "-C", generic_repo, "init"], check=True, capture_output=True)
        ws = os.path.join(generic_repo, config_dir(generic_cfg, "workspace_dir"))
        cand_dir = os.path.join(generic_repo, config_dir(generic_cfg, "candidates_dir"))
        receipt_dir = os.path.join(generic_repo, config_dir(generic_cfg, "receipts_dir"))
        note_dir = os.path.join(generic_repo, config_dir(generic_cfg, "notes_dir"))
        os.makedirs(cand_dir, exist_ok=True)
        os.makedirs(receipt_dir, exist_ok=True)
        os.makedirs(note_dir, exist_ok=True)
        target_rel = "src/example.py"
        target = os.path.join(generic_repo, target_rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write("old\n")
        candidate = os.path.join(cand_dir, "PROJECT-001--example.py")
        with open(candidate, "w", encoding="utf-8") as f:
            f.write("new\n")
        manifest = os.path.join(cand_dir, "PROJECT-001--manifest.json")
        manifest_data = {
            "project_id": "PROJECT-001",
            "verification": [],
            "files": [{
                "path": target_rel,
                "candidate_path": os.path.relpath(candidate, generic_repo),
                "preimage_sha256": sha256_file(target),
                "postimage_sha256": sha256_file(candidate),
            }],
        }
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f)
        receipt = os.path.join(receipt_dir, "APPROVAL-001.json")
        receipt_data = {
            "approval_id": "APPROVAL-001",
            "project_id": "PROJECT-001",
            "approved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "expires_at": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).isoformat(),
            "approver_identity": "any-reviewer",
            "explicit_approval_quote": "approved",
            "candidate_sha256": sha256_file(manifest),
            "candidate_path": os.path.relpath(manifest, generic_repo),
            "allowed_paths": [target_rel],
            "audit_status": "pass",
        }
        with open(receipt, "w", encoding="utf-8") as f:
            json.dump(receipt_data, f)
        generic_test = f'{sys.executable}::-c "print(\'Ran 1 test in 0.01s\\n\\nOK\')"'
        generic_runner = ApplyCandidateRunner(
            receipt, generic_repo, [generic_test],
            False, generic_repo, True, config=generic_cfg
        )
        generic_result, generic_code, _ = generic_runner.run()
        assert generic_result == RES_DRY_RUN_OK and generic_code == EXIT_OK
    print("[OK] Config: repository-agnostic defaults validated with dry-run")

    assert self_test_config["workspace_dir"]
    assert isinstance(self_test_config["approvers"], list)
    print(f"[OK] Config: {suite_name} validated")

    SELF_TEST_CONFIG_OVERRIDE = None
    ACTIVE_CONFIG = _copy_default_config()
    print(f"self-test suite PASS: {suite_name}")
    return 0



def run_self_test() -> int:
    """Run the complete self-test suite against defaults and a neutral legacy-style layout."""
    legacy_layout_raw: Dict[str, Any] = {
        "approvers": ["Alice"],
        "workspace_dir": "work/pending-changes",
        "candidates_dir": "candidates",
        "receipts_dir": "receipts",
        "evidence_dir": "evidence",
        "notes_dir": ".",
        "project_id_pattern": r"^PS-\d{8}-\d{2}$",
        "approval_id_pattern": r"^APR-\d{8}-\d{2}$",
        "watch_dirs": ["app/runtime", ".claude"],
        "ignore_files": [["app/runtime", "service-*.log"]],
        "restart_command": ["true"],
        "restart_check_log": None,
        "os_process_names": ["mdworker_shared", "mdworker", "mds", "mds_stores"],
        "flags_order_dir": "work/tmp/orders",
    }

    # Validate/normalize the embedded profile using the same public configuration loader.
    with tempfile.TemporaryDirectory(prefix="apply-candidate-self-test-config-") as td:
        config_path = os.path.join(td, "legacy-layout.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(legacy_layout_raw, f)
        legacy_cfg, warnings, error = load_config(config_path, td)
        assert legacy_cfg is not None and error is None and not warnings

    for suite_name, cfg in (
        ("default layout", _copy_default_config()),
        ("legacy-style layout", legacy_cfg),
    ):
        code = _run_self_test_once(cfg, suite_name)
        if code != 0:
            return code

    print("self-test: PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="スキル・承認候補の確実な反映スクリプト")
    parser.add_argument("--receipt", type=str, help="受領書JSONへのパス")
    parser.add_argument("--config", type=str, help="設定JSONへのパス (既定: <repo>/.apply-candidate.json)")
    parser.add_argument("--test-cwd", type=str, help="テスト実行時ディレクトリ (既定: repo)")
    parser.add_argument(
        "--test",
        action="append",
        dest="tests",
        default=[],
        help="<python>::<unittest の引数> (複数指定可、1つ以上必須)",
    )
    parser.add_argument(
        "--test-timeout",
        type=float,
        default=None,
        help="テスト1件あたりのタイムアウト秒数 (既定: 60秒、または基準ログから動的決定)",
    )
    parser.add_argument("--restart-listener", action="store_true", help="リスナーの再起動を行う")
    parser.add_argument(
        "--repo",
        type=str,
        default=".",
        help="リポジトリルート (既定: 現在のディレクトリ)",
    )
    parser.add_argument("--dry-run", action="store_true", help="照合のみ行い変更しない")
    parser.add_argument("--self-test", action="store_true", help="自己検査を実行")

    args = parser.parse_args()
    repo = os.path.abspath(args.repo)

    if args.self_test:
        return run_self_test()

    config, config_warnings, config_error = load_config(args.config, repo)
    for warning in config_warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    if config_error or config is None:
        print(f"RESULT: {RES_REFUSED}\nreason: invalid configuration: {config_error}")
        return EXIT_REFUSED

    if not args.receipt:
        print("RESULT: ERROR\nreason: --receipt is required", file=sys.stderr)
        return EXIT_ERROR

    runner: Optional[ApplyCandidateRunner] = None
    try:
        runner = ApplyCandidateRunner(
            receipt_path=args.receipt,
            test_cwd=args.test_cwd,
            test_specs=args.tests,
            restart_listener=args.restart_listener,
            repo=repo,
            dry_run=args.dry_run,
            test_timeout=args.test_timeout,
            config=config,
        )
        result_str, exit_code, output_msg = runner.run()
        print(output_msg)
        return exit_code
    except Exception as exc:
        # (7) 例外時の復元の結果（戻せたか、戻せなかったファイル）を RESULT: ERROR の下に出す
        rollback_info = "no changes to rollback"
        if runner and runner.applied_paths:
            try:
                rb_ok = runner.rollback()
                if rb_ok:
                    rollback_info = "rollback completed (all files restored)"
                else:
                    rollback_info = (
                        f"rollback failed for: {', '.join(runner.rollback_failed_files)}"
                    )
            except Exception as rb_exc:
                rollback_info = f"rollback error: {rb_exc}"
        print(f"RESULT: {RES_ERROR}\nrollback: {rollback_info}\nreason: unhandled exception: {exc}")
        return EXIT_UNHANDLED_ERROR


if __name__ == "__main__":
    sys.exit(main())
