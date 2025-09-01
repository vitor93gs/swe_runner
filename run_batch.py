#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import subprocess
from pathlib import Path
from typing import List

from task_prep import read_tasks_from_sheet, prepare_task_folder, TaskPaths


def sh(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    print("▶", " ".join(map(str, cmd)))
    return subprocess.run(cmd, check=True, text=True, **kw)


def main() -> None:
    ap = argparse.ArgumentParser(description="Batch runner for swe_runner.py over tasks listed in a sheet/CSV.")
    ap.add_argument("--sheet", required=True,
                    help="Google Sheets URL (shareable) or path to a local CSV with headers: "
                         "task_id, updated_issue_description, dockerfile, test_command, test_patch")
    ap.add_argument("--tasks-dir", default="tasks", help="Folder to create per-task subfolders (default: tasks)")
    ap.add_argument("--model", default="gemini/gemini-2.5-pro", help="LiteLLM model string for swe_runner (e.g., gemini/gemini-2.5-pro)")
    ap.add_argument("--limit", type=int, default=0, help="Process only the first N tasks (0 = all)")
    ap.add_argument("--only-task-ids", default="", help="Comma-separated task IDs to include (optional)")
    ap.add_argument("--swe-runner-path", default=str(Path(__file__).parent / "swe_runner.py"),
                    help="Path to your existing swe_runner.py (default: alongside this script)")

    # Any extra args (e.g., --cost-limit, --call-limit, --base-commit, --allow-missing-key, --skip-build, --overlay-tag, etc.)
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

    for i, row in enumerate(rows, 1):
        tid = row.get("task_id")
        paths = prepare_task_folder(tasks_dir, row)  
        
        # Per-task naming & output folder
        instance_id = f"task_id_{tid}"
        out_dir = Path("trajectories")  # e.g., trajectories/task_id_1

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

        sh(cmd)

if __name__ == "__main__":
    main()
