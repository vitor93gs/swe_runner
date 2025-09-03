#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# --------------------------- tiny shell helpers ---------------------------

def run_logged(cmd: List[str], log_path: Path, cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> int:
    """Execute a command and stream its output to a log file.

    Args:
        cmd (List[str]): Command to execute as a list of strings.
        log_path (Path): Path to the log file where output will be written.
        cwd (Optional[Path], optional): Working directory for command execution. Defaults to None.
        env (Optional[Dict[str, str]], optional): Environment variables for command execution. Defaults to None.

    Returns:
        int: Exit code of the command.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as lf:
        lf.write(f"▶ {' '.join(map(str, cmd))}\n")
        lf.flush()
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=str(cwd) if cwd else None, env=env)
        return proc.wait()

def run_capture(cmd: List[str], check: bool = False) -> Tuple[int, str, str]:
    """Execute a command and capture its output streams.

    Args:
        cmd (List[str]): Command to execute as a list of strings.
        check (bool, optional): If True, raises CalledProcessError for non-zero exit codes. Defaults to False.

    Returns:
        Tuple[int, str, str]: A tuple containing (exit_code, stdout, stderr).

    Raises:
        subprocess.CalledProcessError: If check=True and the command returns non-zero exit code.
    """
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, proc.stdout, proc.stderr)
    return proc.returncode, proc.stdout, proc.stderr

def echo_to_log(log: Path, text: str) -> None:
    """Append text to a log file, creating parent directories if needed.

    Args:
        log (Path): Path to the log file.
        text (str): Text to append to the log file.
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as lf:
        lf.write(text.rstrip() + "\n")

# --------------------------- workdir detection ---------------------------

_WORKDIR_RE = re.compile(r"(?im)^\s*WORKDIR\s+(.+)$")

def parse_workdir_from_dockerfile(dockerfile: Path) -> Optional[str]:
    """Extract the final working directory from a Dockerfile.

    Args:
        dockerfile (Path): Path to the Dockerfile to analyze.

    Returns:
        Optional[str]: The last WORKDIR path specified in the Dockerfile,
            with quotes and trailing slashes removed. Returns None if
            no WORKDIR is found, file cannot be read, or path is empty.
            Paths not starting with '/' will have it prepended.
    """
    try:
        txt = dockerfile.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    matches = list(_WORKDIR_RE.finditer(txt))
    if not matches:
        return None
    val = matches[-1].group(1).strip().strip("'").strip('"').rstrip("/")
    if not val:
        return None
    return val if val.startswith("/") else "/" + val

def image_workdir(image_tag: str) -> Optional[str]:
    """Get the working directory configured in a Docker image.

    Args:
        image_tag (str): Name/tag of the Docker image to inspect.

    Returns:
        Optional[str]: The configured WorkingDir from the image metadata,
            or None if not set, empty, or if inspection fails.

    Note:
        Uses 'docker image inspect' to get the WorkingDir configuration,
        which is authoritative even for multi-stage builds.
    """
    # docker image inspect --format '{{.Config.WorkingDir}}' <image>
    code, out, _ = run_capture(["docker", "image", "inspect", image_tag, "--format", "{{.Config.WorkingDir}}"])
    val = (out or "").strip()
    return val or None

# --------------------------- task discovery ---------------------------

@dataclass
class TaskPaths:
    """Container for paths related to a task's files and directories.

    Attributes:
        task_id (str): Unique identifier for the task.
        task_dir (Path): Root directory containing task files.
        dockerfile (Path): Path to the task's Dockerfile.
        test_cmd_file (Path): Path to file containing test commands.
        test_patch_tar (Path): Path to test patch archive.
        agent_patch (Path): Path to trajectories patch file.
        logs_dir (Path): Directory for test logs (tests/task_id_<id>/).
    """
    task_id: str
    task_dir: Path
    dockerfile: Path
    test_cmd_file: Path
    test_patch_tar: Path
    agent_patch: Path  # trajectories patch
    logs_dir: Path     # tests/task_id_<id>/

def discover_tasks(tasks_dir: Path, trajectories_dir: Path, tests_dir: Path, only_ids: Optional[set[str]]) -> List[TaskPaths]:
    """Find and collect task information from the workspace.

    Args:
        tasks_dir (Path): Directory containing task subdirectories.
        trajectories_dir (Path): Directory containing task trajectories/patches.
        tests_dir (Path): Directory for test outputs.
        only_ids (Optional[set[str]]): If provided, only include tasks with these IDs.

    Returns:
        List[TaskPaths]: List of TaskPaths objects for discovered tasks,
            filtered by only_ids if specified.
    """
    tasks: List[TaskPaths] = []
    for p in sorted(tasks_dir.glob("task_id_*")):
        if not p.is_dir():
            continue
        m = re.match(r"task_id_(.+)", p.name)
        if not m:
            continue
        tid = m.group(1)
        if only_ids and tid not in only_ids:
            continue

        dockerfile = p / "Dockerfile"
        test_cmd_file = p / "test_command.txt"
        test_patch_tar = p / "test_patch.tar"

        traj_dir = trajectories_dir / f"task_id_{tid}"
        agent_patch = traj_dir / f"task_id_{tid}.patch"

        logs_dir = tests_dir / f"task_id_{tid}"

        tasks.append(TaskPaths(
            task_id=tid,
            task_dir=p,
            dockerfile=dockerfile,
            test_cmd_file=test_cmd_file,
            test_patch_tar=test_patch_tar,
            agent_patch=agent_patch,
            logs_dir=logs_dir,
        ))
    return tasks

# --------------------------- container lifecycle ---------------------------

def unique_container_name(tid: str) -> str:
    """Generate a unique container name for a task.

    Args:
        tid (str): Task identifier.

    Returns:
        str: Unique container name combining task ID and random hex string.
    """
    return f"task{tid}_runner_{uuid.uuid4().hex[:8]}"

def start_container(image: str, name: str, setup_log: Path) -> int:
    """Start a detached Docker container for task execution.

    Args:
        image (str): Docker image to run.
        name (str): Name to assign to the container.
        setup_log (Path): Path to log file for setup operations.

    Returns:
        int: Exit code from container startup (0 for success).

    Note:
        Container is started detached with 'sleep infinity' to keep it running
        for subsequent 'docker cp' and 'docker exec' operations.
    """
    return run_logged(["docker", "run", "-d", "--name", name, image, "sh", "-lc", "sleep infinity"], setup_log)

def stop_rm_container(name: str, setup_log: Path) -> None:
    """Stop and remove a Docker container.

    Args:
        name (str): Name of the container to stop and remove.
        setup_log (Path): Path to log file for setup operations.
    """
    run_logged(["docker", "rm", "-f", name], setup_log)

def docker_cp(src: Path, container: str, dst_in_container: str, setup_log: Path) -> int:
    """Copy a file into a running Docker container.

    Args:
        src (Path): Path to source file on host.
        container (str): Name of the target container.
        dst_in_container (str): Destination path inside container.
        setup_log (Path): Path to log file for setup operations.

    Returns:
        int: Exit code from copy operation (0 for success).
    """
    return run_logged(["docker", "cp", str(src), f"{container}:{dst_in_container}"], setup_log)

def docker_exec(container: str, command: str, setup_log: Path, workdir: Optional[str] = None) -> int:
    """Execute a command in a running Docker container.

    Args:
        container (str): Name of the container to execute in.
        command (str): Shell command to execute.
        setup_log (Path): Path to log file for setup operations.
        workdir (Optional[str], optional): Working directory in container. Defaults to None.

    Returns:
        int: Exit code from command execution (0 for success).

    Note:
        Uses sh -lc to allow shell pipelines and expansions. Command runs as long as
        container's PID 1 is alive.
    """
    exec_cmd = ["docker", "exec"]
    if workdir:
        exec_cmd += ["-w", workdir]
    exec_cmd += [container, "sh", "-lc", command]
    return run_logged(exec_cmd, setup_log)

# --------------------------- robust patch application ---------------------------

def apply_patch_robustly(container: str, patch_file: str, repo_dir: str, setup_log: Path, patch_type: str = "agent") -> bool:
    """Apply a patch file using multiple fallback strategies.
    
    Args:
        container (str): Name of the container
        patch_file (str): Path to patch file inside container
        repo_dir (str): Repository directory in container
        setup_log (Path): Log file for operations
        patch_type (str): Type of patch for logging ("agent" or "test")
    
    Returns:
        bool: True if patch was successfully applied, False otherwise
    """
    echo_to_log(setup_log, f"=== Attempting to apply {patch_type} patch: {patch_file} ===")
    
    # Check if patch file exists
    check_code = docker_exec(container, f'[ -f "{patch_file}" ]', setup_log)
    if check_code != 0:
        echo_to_log(setup_log, f"WARNING: {patch_type} patch file {patch_file} does not exist")
        return True  # No patch to apply is not a failure
    
    strategies = [
        # Strategy 1: Git apply with robust options (preferred)
        {
            'name': 'git apply (robust)',
            'cmd': f'cd "{repo_dir}" && command -v git >/dev/null 2>&1 && git apply -v --recount --unidiff-zero --whitespace=fix "{patch_file}"'
        },
        # Strategy 2: Git apply basic
        {
            'name': 'git apply (basic)',
            'cmd': f'cd "{repo_dir}" && command -v git >/dev/null 2>&1 && git apply "{patch_file}"'
        },
        # Strategy 3: Git apply ignoring whitespace
        {
            'name': 'git apply (ignore whitespace)',
            'cmd': f'cd "{repo_dir}" && command -v git >/dev/null 2>&1 && git apply --ignore-whitespace "{patch_file}"'
        },
        # Strategy 4: patch command with -p1
        {
            'name': 'patch -p1',
            'cmd': f'cd "{repo_dir}" && command -v patch >/dev/null 2>&1 && patch -p1 --no-backup-if-mismatch -i "{patch_file}"'
        },
        # Strategy 5: patch command with -p1 and fuzzy matching
        {
            'name': 'patch -p1 (fuzzy)',
            'cmd': f'cd "{repo_dir}" && command -v patch >/dev/null 2>&1 && patch -p1 --no-backup-if-mismatch --fuzz=3 -i "{patch_file}"'
        },
        # Strategy 6: patch command with -p0
        {
            'name': 'patch -p0',
            'cmd': f'cd "{repo_dir}" && command -v patch >/dev/null 2>&1 && patch -p0 --no-backup-if-mismatch -i "{patch_file}"'
        },
        # Strategy 7: patch command with -p0 and fuzzy matching
        {
            'name': 'patch -p0 (fuzzy)',
            'cmd': f'cd "{repo_dir}" && command -v patch >/dev/null 2>&1 && patch -p0 --no-backup-if-mismatch --fuzz=3 -i "{patch_file}"'
        }
    ]
    
    for strategy in strategies:
        echo_to_log(setup_log, f"Trying strategy: {strategy['name']}")
        code = docker_exec(container, strategy['cmd'], setup_log)
        
        if code == 0:
            echo_to_log(setup_log, f"SUCCESS: {patch_type} patch applied using {strategy['name']}")
            return True
        else:
            echo_to_log(setup_log, f"FAILED: {strategy['name']} returned code {code}")
    
    echo_to_log(setup_log, f"ERROR: All patch application strategies failed for {patch_type} patch")
    return False

# --------------------------- core per-task routine ---------------------------

def process_task(tp: TaskPaths, default_repo_dir: str = "/app") -> Dict[str, object]:
    """Process a single task for testing.

    This function:
    1. Creates log directories
    2. Validates task files (Dockerfile, test commands)
    3. Builds Docker image
    4. Determines repository working directory
    5. Sets up and runs tests in container
    6. Applies patches if available (with robust fallback strategies)
    7. Collects and returns results

    Args:
        tp (TaskPaths): Task paths and configuration.
        default_repo_dir (str, optional): Default repository directory if not specified. 
            Defaults to "/app".

    Returns:
        Dict[str, object]: Results dictionary containing:
            - task_id: Task identifier
            - image_tag: Docker image tag
            - repo_dir: Repository directory in container
            - build_ok: Whether Docker build succeeded
            - agent_patch_ok: Whether agent patch application succeeded
            - test_patch_ok: Whether test patch application succeeded
            - test_ok: Whether tests passed
            - test_exit_code: Test command exit code
            - skipped: Whether task was skipped due to patch failures
            - skip_reason: Reason for skipping if applicable
            - notes: List of notable events/issues
            - paths: Dictionary of log file paths
    """
    tp.logs_dir.mkdir(parents=True, exist_ok=True)
    build_log = tp.logs_dir / "build.log"
    setup_log = tp.logs_dir / "setup.log"
    test_log  = tp.logs_dir / "test.log"

    result: Dict[str, object] = {
        "task_id": tp.task_id,
        "image_tag": f"task{tp.task_id}:test-run",
        "repo_dir": None,
        "build_ok": False,
        "agent_patch_ok": False,
        "test_patch_ok": False,
        "test_ok": False,
        "test_exit_code": None,
        "skipped": False,
        "skip_reason": None,
        "notes": [],
        "paths": {
            "build_log": str(build_log),
            "setup_log": str(setup_log),
            "test_log":  str(test_log),
        },
    }

    # 0) Sanity: Dockerfile & test_command
    if not tp.dockerfile.exists():
        echo_to_log(build_log, f"ERROR: Dockerfile missing at {tp.dockerfile}")
        result["notes"].append("Missing Dockerfile")
        return result
    if not tp.test_cmd_file.exists():
        echo_to_log(test_log, f"ERROR: test_command.txt missing at {tp.test_cmd_file}")
        result["notes"].append("Missing test_command.txt")
        return result

    test_cmd = tp.test_cmd_file.read_text(encoding="utf-8").strip()
    if not test_cmd:
        echo_to_log(test_log, "ERROR: test_command.txt is empty")
        result["notes"].append("Empty test command")
        return result

    # 1) Build image
    image_tag = result["image_tag"]
    build_code = run_logged(
        ["docker", "build", "-f", str(tp.dockerfile), "-t", image_tag, str(tp.task_dir)],
        build_log
    )
    result["build_ok"] = (build_code == 0)
    if build_code != 0:
        result["notes"].append("Docker build failed")
        return result

    # 2) Determine repo workdir in image (inspect -> Dockerfile -> /app)
    repo_dir = image_workdir(image_tag) or parse_workdir_from_dockerfile(tp.dockerfile) or default_repo_dir
    result["repo_dir"] = repo_dir

    # 3) Start container
    cname = unique_container_name(tp.task_id)
    start_code = start_container(image_tag, cname, setup_log)
    if start_code != 0:
        result["notes"].append("Failed to start container")
        return result

    try:
        # 4) Copy patches (if present)
        if tp.agent_patch.exists():
            if docker_cp(tp.agent_patch, cname, "/tmp/agent.patch", setup_log) != 0:
                result["notes"].append("Failed to docker cp agent patch")
                result["skipped"] = True
                result["skip_reason"] = "Failed to copy agent patch to container"
                return result
        else:
            echo_to_log(setup_log, f"NOTE: Missing agent patch at {tp.agent_patch}")

        test_patch_exists = tp.test_patch_tar.exists()
        if test_patch_exists:
            if docker_cp(tp.test_patch_tar, cname, "/tmp/test_patch.tar", setup_log) != 0:
                result["notes"].append("Failed to docker cp test_patch.tar")
                result["skipped"] = True
                result["skip_reason"] = "Failed to copy test patch to container"
                return result
        else:
            echo_to_log(setup_log, f"NOTE: No test_patch.tar at {tp.test_patch_tar}")

        # 5) Apply agent patch with robust strategies
        if tp.agent_patch.exists():
            agent_patch_success = apply_patch_robustly(cname, "/tmp/agent.patch", repo_dir, setup_log, "agent")
            result["agent_patch_ok"] = agent_patch_success
            
            if not agent_patch_success:
                result["skipped"] = True
                result["skip_reason"] = "Agent patch failed to apply"
                result["notes"].append("Task skipped: agent patch application failed")
                return result
        else:
            result["agent_patch_ok"] = True  # No patch to apply counts as success

        # 6) Extract and apply test patch if present
        if test_patch_exists:
            # Extract test_patch.tar
            tar_code = docker_exec(cname, f'cd "{repo_dir}" && tar -xf /tmp/test_patch.tar', setup_log)
            if tar_code != 0:
                result["notes"].append("Failed to extract test_patch.tar")
                result["skipped"] = True
                result["skip_reason"] = "Failed to extract test_patch.tar"
                return result
            
            # Check if extraction created patch files and try to apply them
            # Look for common patch file patterns
            patch_check_cmd = f'''cd "{repo_dir}" && find . -maxdepth 2 -name "*.patch" -o -name "*.diff" | head -5'''
            patch_files_code = docker_exec(cname, patch_check_cmd, setup_log)
            
            if patch_files_code == 0:
                # Try to apply any patch files found from the tar extraction
                apply_extracted_patches_cmd = f'''
                cd "{repo_dir}"
                success=true
                for pf in $(find . -maxdepth 2 -name "*.patch" -o -name "*.diff"); do
                    echo "Attempting to apply extracted patch: $pf"
                    if ! (git apply -v --recount --unidiff-zero --whitespace=fix "$pf" || \\
                          git apply "$pf" || \\
                          git apply --ignore-whitespace "$pf" || \\
                          patch -p1 --no-backup-if-mismatch -i "$pf" || \\
                          patch -p1 --no-backup-if-mismatch --fuzz=3 -i "$pf" || \\
                          patch -p0 --no-backup-if-mismatch -i "$pf" || \\
                          patch -p0 --no-backup-if-mismatch --fuzz=3 -i "$pf"); then
                        echo "ERROR: Failed to apply patch $pf"
                        success=false
                        break
                    else
                        echo "SUCCESS: Applied patch $pf"
                    fi
                done
                $success
                '''
                patch_apply_code = docker_exec(cname, apply_extracted_patches_cmd, setup_log)
                result["test_patch_ok"] = (patch_apply_code == 0)
                
                if patch_apply_code != 0:
                    result["skipped"] = True
                    result["skip_reason"] = "Test patch failed to apply"
                    result["notes"].append("Task skipped: test patch application failed")
                    return result
            else:
                # No patch files found in extraction, that's fine
                result["test_patch_ok"] = True
        else:
            result["test_patch_ok"] = True  # No test patch to apply

        # 7) Run tests (capture to test_log by streaming)
        exit_code = run_logged(["docker", "exec", "-w", repo_dir, cname, "sh", "-lc", test_cmd], test_log)
        result["test_exit_code"] = exit_code
        result["test_ok"] = (exit_code == 0)

    finally:
        # 8) Cleanup container
        stop_rm_container(cname, setup_log)

    # 9) Save per-task result.json
    with (tp.logs_dir / "result.json").open("w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    return result

# --------------------------- summary helpers ---------------------------

def write_summary(tests_dir: Path, results: List[Dict[str, object]]) -> None:
    """Generate and write test execution summaries.

    Creates two summary files:
    1. summary.json: Detailed JSON format with all test results
    2. summary.md: Human-friendly markdown format with status tables

    Args:
        tests_dir (Path): Directory to write summary files to.
        results (List[Dict[str, object]]): List of task execution results.
    """
    summary = {
        "total": len(results),
        "build_ok": sum(1 for r in results if r.get("build_ok")),
        "agent_patch_ok": sum(1 for r in results if r.get("agent_patch_ok")),
        "test_patch_ok": sum(1 for r in results if r.get("test_patch_ok")),
        "test_ok":  sum(1 for r in results if r.get("test_ok")),
        "skipped": sum(1 for r in results if r.get("skipped")),
        "by_task": results,
        "generated_at": int(time.time()),
    }
    (tests_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    # Also a compact human-friendly summary.md
    lines = [
        "# Test Summary",
        "",
        f"- Total tasks: {summary['total']}",
        f"- Builds OK:  {summary['build_ok']}",
        f"- Agent patches OK: {summary['agent_patch_ok']}",
        f"- Test patches OK: {summary['test_patch_ok']}",
        f"- Tests OK:   {summary['test_ok']}",
        f"- Skipped:    {summary['skipped']}",
        "",
        "| Task ID | Build | Agent Patch | Test Patch | Test | Exit | Status | Logs |",
        "|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for r in results:
        logs = r.get("paths", {}) or {}
        link = str(Path(logs.get("test_log", ""))) if logs else "-"
        status = "SKIPPED" if r.get("skipped") else "COMPLETED"
        skip_reason = f" ({r.get('skip_reason', '')})" if r.get("skipped") else ""
        lines.append(
            f"| {r.get('task_id')} | "
            f"{'✅' if r.get('build_ok') else '❌'} | "
            f"{'✅' if r.get('agent_patch_ok') else '❌'} | "
            f"{'✅' if r.get('test_patch_ok') else '❌'} | "
            f"{'✅' if r.get('test_ok') else '❌'} | "
            f"{r.get('test_exit_code') or '-'} | "
            f"{status}{skip_reason} | "
            f"{link} |"
        )

    (tests_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

# --------------------------- CLI ---------------------------

def main() -> None:
    """Execute the main verification workflow.

    Handles command-line arguments and orchestrates the task verification process:
    1. Validates Docker availability
    2. Discovers tasks to process
    3. Executes each task's verification with robust patch application
    4. Generates summary reports

    Command-line arguments:
    - --tasks-dir: Folder containing task subdirectories
    - --trajectories-dir: Folder with task patches
    - --tests-dir: Output folder for logs
    - --only-task-ids: Optional task ID filter
    - --limit: Optional limit on number of tasks to process

    Exits with status 1 if Docker is unavailable or no tasks are found.
    """
    ap = argparse.ArgumentParser(description="Build, patch, and test each SWE task; log results under tests/")
    ap.add_argument("--tasks-dir", default="tasks", help="Folder containing task_id_<id>/ subfolders")
    ap.add_argument("--trajectories-dir", default="trajectories", help="Folder containing task_id_<id>/*.patch files")
    ap.add_argument("--tests-dir", default="tests", help="Output folder for logs and summaries")
    ap.add_argument("--only-task-ids", default="", help="Comma-separated list of task ids to include (optional)")
    ap.add_argument("--limit", type=int, default=0, help="Only process first N tasks (0 = all)")
    args = ap.parse_args()

    tasks_dir = Path(args.tasks_dir)
    trajectories_dir = Path(args.trajectories_dir)
    tests_dir = Path(args.tests_dir)

    # Validate docker presence early
    code, _, _ = run_capture(["docker", "--version"])
    if code != 0:
        print("ERROR: Docker is required but not available on PATH.", file=sys.stderr)
        sys.exit(1)

    only_ids = {x.strip() for x in args.only_task_ids.split(",") if x.strip()} if args.only_task_ids else None
    tps = discover_tasks(tasks_dir, trajectories_dir, tests_dir, only_ids)
    if args.limit > 0:
        tps = tps[: args.limit]
    if not tps:
        print("No tasks found.", file=sys.stderr)
        sys.exit(1)

    results: List[Dict[str, object]] = []
    for tp in tps:
        print(f"=== Processing task {tp.task_id} ===")
        res = process_task(tp)
        if res.get("skipped"):
            print(f"    SKIPPED: {res.get('skip_reason', 'Unknown reason')}")
        elif res.get("test_ok"):
            print(f"    PASSED")
        else:
            print(f"    FAILED (exit code: {res.get('test_exit_code', 'unknown')})")
        results.append(res)

    tests_dir.mkdir(parents=True, exist_ok=True)
    write_summary(tests_dir, results)
    
    total = len(results)
    skipped = sum(1 for r in results if r.get("skipped"))
    passed = sum(1 for r in results if r.get("test_ok"))
    
    print(f"\n=== SUMMARY ===")
    print(f"Total tasks: {total}")
    print(f"Skipped: {skipped}")
    print(f"Passed: {passed}")
    print(f"Failed: {total - skipped - passed}")
    print(f"\nSee per-task logs under '{tests_dir}/task_id_<id>/' and overall summary in '{tests_dir}/summary.md' and 'summary.json'.")

if __name__ == "__main__":
    main()