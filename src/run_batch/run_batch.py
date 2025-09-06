#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import subprocess
import os
from pathlib import Path
from typing import List, Optional

from task_prep import read_tasks_from_sheet, prepare_task_folder


def sh(cmd: List[str], log_file: Optional[Path] = None, **kw) -> subprocess.CompletedProcess:
    """Execute a shell command with optional live tee to a log file.

    If `log_file` is provided, the command's combined stdout/stderr is streamed
    to both the console and the file. This preserves live feedback while
    retaining a persistent log.

    Args:
        cmd (List[str]): Command to execute (list of tokens).
        log_file (Optional[Path]): If set, append output to this file while also
            printing to stdout.
        **kw: Additional keyword arguments. Supports a `check` bool like
            `subprocess.run`; other kwargs are passed to the underlying call.

    Returns:
        subprocess.CompletedProcess: Result with returncode and args set.

    Raises:
        subprocess.CalledProcessError: If the process exits non-zero and
            `check=True`.
    """
    import shlex
    from datetime import datetime

    print("▶", " ".join(map(str, cmd)))

    # Emulate subprocess.run's 'check' behavior, but don't pass it into Popen/run twice.
    check = kw.pop("check", True)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        # Header
        with log_file.open("a", encoding="utf-8") as lf:
            lf.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} =====\n")
            lf.write("$ " + " ".join(shlex.quote(str(x)) for x in cmd) + "\n")

        # Don't forward args Popen doesn't accept or ones we control explicitly
        popen_kw = {k: v for k, v in kw.items() if k not in {"stdout", "stderr", "text", "encoding", "check"}}
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            **popen_kw,
        )
        assert proc.stdout is not None
        with log_file.open("a", encoding="utf-8") as lf:
            for line in proc.stdout:
                sys.stdout.write(line)
                lf.write(line)
        ret = proc.wait()
        if check and ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)
        return subprocess.CompletedProcess(cmd, ret)

    # Simple path: don't duplicate 'check' kw
    run_kw = kw.copy()
    run_kw.pop("text", None)  # we set it explicitly
    return subprocess.run(cmd, check=check, text=True, **run_kw)


def main() -> None:
    """
    Execute the main batch processing logic for running SWE tasks.

    This function handles command line argument parsing and orchestrates the execution of multiple
    SWE tasks defined in a Google Sheet or CSV file. Each task is processed using swe_runner.py
    in its own environment with task-specific configurations.

    Key features include:
      - Per-task logging to trajectories/logs/<instance_id>/run.log
      - Processing subsequent tasks even if one fails
      - End-of-run summary with paths to logs

    Returns:
        None
    """
    ap = argparse.ArgumentParser(description="Batch runner for swe_runner.py over tasks listed in a sheet/CSV.")
    ap.add_argument("--sheet", required=True,
                    help="Google Sheets URL (shareable) or path to a local CSV with headers: "
                         "task_id, updated_issue_description, dockerfile, test_command, test_patch")
    ap.add_argument("--tasks-dir", default="tasks", help="Folder to create per-task subfolders (default: tasks)")
    ap.add_argument("--model", default="gemini/gemini-2.5-pro", help="LiteLLM model string for swe_runner (e.g., gemini/gemini-2.5-pro)")
    ap.add_argument("--limit", type=int, default=0, help="Process only the first N tasks (0 = all)")
    ap.add_argument("--only-task-ids", default="", help="Comma-separated task IDs to include (optional)")
    ap.add_argument("--swe-runner-path", default=str(Path(__file__).parent / "runners/swe_runner.py"),
                    help="Path to your existing swe_runner.py (default: alongside this script)")

    # Any extra args (e.g., --cost-limit, --call-limit, --base-commit, --allow-missing-key, --skip-build, --overlay-tag, --docker-platform, etc.)
    args, extra = ap.parse_known_args()

    tasks_dir = Path(args.tasks_dir)
    tasks_dir.mkdir(parents=True, exist_ok=True)

    rows = read_tasks_from_sheet(args.sheet)
    if args.only_task_ids:
        wanted = {x.strip() for x in args.only_task_ids.split(",") if x.strip()}
        rows = [r for r in rows if r.get("task_id") in wanted]
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    if not rows:
        print("No tasks found matching the selection.")
        return

    failures = []
    successes = []

    for i, row in enumerate(rows, 1):
        tid = row.get("task_id")
        instance_id = f"task_id_{tid}"

        # Prepare shared output + log paths up front so task_prep can log too
        out_dir = Path("trajectories")
        log_dir = out_dir / "logs" / instance_id
        log_file = log_dir / "run.log"
        log_dir.mkdir(parents=True, exist_ok=True)

        # Make task_prep's subprocess calls (gdown/curl) write into the same log
        prev_env_val = os.environ.get("SWE_TASK_LOG")
        os.environ["SWE_TASK_LOG"] = str(log_file)

        try:
            # Build per-task folder (downloads/copies artifacts) with logging
            paths = prepare_task_folder(tasks_dir, row)

            image_tag = f"task{tid}:latest"
            cmd = [
                sys.executable, args.swe_runner_path,
                "--dockerfile", str(paths.dockerfile),
                "--image-tag", image_tag,
                "--prompt-file", str(paths.task_md),
                "--model", args.model,
                "--instance-id", instance_id,
                "--output-dir", str(out_dir),
            ] + extra

            # Tee the entire swe_runner session (docker builds + agent logs)
            sh(cmd, log_file=log_file, check=True)

            successes.append((tid, str(log_file)))
        except subprocess.CalledProcessError as e:
            print(f"❌ Task {tid} failed (exit {e.returncode}). Log: {log_file}")
            failures.append((tid, str(log_file)))
            # Continue to next task
        finally:
            # Restore environment variable
            if prev_env_val is None:
                os.environ.pop("SWE_TASK_LOG", None)
            else:
                os.environ["SWE_TASK_LOG"] = prev_env_val

    # Print a concise summary and exit non-zero if anything failed
    print("\n==================== Batch Summary ====================")
    if successes:
        print("✅ Succeeded:")
        for tid, lf in successes:
            print(f"  - {tid} (log: {lf})")
    if failures:
        print("❌ Failed:")
        for tid, lf in failures:
            print(f"  - {tid} (log: {lf})")
    print("=======================================================\n")

    if failures:
        sys.exit(1)

if __name__ == "__main__":
    main()
