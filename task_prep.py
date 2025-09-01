#!/usr/bin/env python3
from __future__ import annotations

import csv
import io
import re
import sys
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse, parse_qs

try:
    import requests  # type: ignore
except Exception:
    requests = None  # we'll fallback to curl if needed


# --------------------------- small shell helper ---------------------------

def sh(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    """Execute a shell command and return its result.

    Args:
        cmd (List[str]): Command to execute as a list of strings.
        **kw: Additional keyword arguments to pass to subprocess.run.

    Returns:
        subprocess.CompletedProcess: Result of the command execution.

    Raises:
        subprocess.CalledProcessError: If the command returns a non-zero exit status.
    """
    print("▶", " ".join(map(str, cmd)))
    return subprocess.run(cmd, check=True, text=True, **kw)


# --------------------------- Google Sheets helpers ---------------------------

_SHEETS_HOSTS = {"docs.google.com", "sheets.google.com"}

def _is_http_url(s: str) -> bool:
    """Check if a string represents an HTTP(S) URL.

    Args:
        s (str): String to check.

    Returns:
        bool: True if the string starts with 'http://' or 'https://', False otherwise.
    """
    return s.startswith("http://") or s.startswith("https://")

def _is_google_sheets_url(url: str) -> bool:
    """Determine if a URL is a Google Sheets URL.

    Args:
        url (str): URL to check.

    Returns:
        bool: True if the URL is a Google Sheets URL, False otherwise.
    """
    try:
        host = urlparse(url).netloc
        return any(host.endswith(h) for h in _SHEETS_HOSTS) and "/spreadsheets/" in url
    except Exception:
        return False

def _sheets_url_to_csv(url: str) -> str:
    """Convert a Google Sheets URL to its CSV export URL.

    Args:
        url (str): Google Sheets URL to convert.

    Returns:
        str: CSV export URL for the sheet. If the input URL already appears
            to be an export URL, returns it unchanged.

    Note:
        - If a gid parameter is present in the URL, it is preserved to export
          the specific tab.
        - If no gid is present, exports the active tab.
    """
    parsed = urlparse(url)
    # /spreadsheets/d/<ID>/...
    m = re.search(r"/spreadsheets/d/([^/]+)/", parsed.path)
    if not m:
        return url  # fall back; maybe caller gave an export link already
    sheet_id = m.group(1)
    qs = parse_qs(parsed.query)
    gid = qs.get("gid", [None])[0]
    base = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    if gid:
        base += f"&gid={gid}"
    return base

def _fetch_csv_text(sheet_src: str) -> str:
    """Load CSV text from either a Google Sheets URL or a local CSV path.

    Args:
        sheet_src (str): URL or file path to the CSV source.

    Returns:
        str: Content of the CSV file.

    Raises:
        requests.exceptions.RequestException: If HTTP request fails.
        IOError: If local file cannot be read.
    """
    if _is_http_url(sheet_src):
        url = _sheets_url_to_csv(sheet_src) if _is_google_sheets_url(sheet_src) else sheet_src
        if requests is None:
            # Fallback: use curl
            out = subprocess.check_output(["curl", "-fsSL", url], text=True)
            return out
        resp = requests.get(url)
        resp.raise_for_status()
        return resp.text
    # local file path
    return Path(sheet_src).read_text(encoding="utf-8")


def read_tasks_from_sheet(sheet_src: str) -> List[Dict[str, str]]:
    """Read task definitions from a CSV sheet.

    Args:
        sheet_src (str): URL or file path to the sheet containing task definitions.

    Returns:
        List[Dict[str, str]]: List of task definitions, where each task is a dictionary
            with the following expected headers as keys:
            - task_id: Unique identifier for the task
            - updated_issue_description: Description of the task
            - dockerfile: Path or content of the Dockerfile
            - test_command: Command to run tests
            - test_patch: Path to test patch file
            All values are strings with whitespace trimmed.

    Note:
        Rows without a task_id are skipped.
    """
    csv_text = _fetch_csv_text(sheet_src)
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = []
    for raw in reader:
        row = { (k or "").strip(): (v or "").strip() for k, v in raw.items() }
        if not row.get("task_id"):
            continue
        rows.append(row)
    return rows


# --------------------------- Google Drive download helpers ---------------------------

_DRIVE_PATTERNS = [
    re.compile(r"https?://drive\.google\.com/file/d/([^/]+)/", re.I),
    re.compile(r"https?://drive\.google\.com/open\?id=([^&]+)", re.I),
    re.compile(r"https?://drive\.google\.com/uc\?id=([^&]+)", re.I),
]

def extract_drive_file_id(url: str) -> Optional[str]:
    """Extract the file ID from a Google Drive URL.

    Args:
        url (str): Google Drive URL to parse.

    Returns:
        Optional[str]: The file ID if found in the URL using any of the known
            patterns (file/d/, open?id=, uc?id=), None if no pattern matches.
    """
    for pat in _DRIVE_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None

def _ensure_gdown_available() -> Optional[str]:
    """Ensure the gdown package is available either as Python module or CLI tool.

    Attempts to:
    1. Import gdown as a Python module
    2. If that fails, check for gdown CLI
    3. If neither exists, attempt to install via pip

    Returns:
        Optional[str]: 
            - "python" if gdown is available as a Python module
            - "cli" if only the command-line tool is available
            - None if gdown cannot be found or installed

    Note:
        gdown is used as it reliably handles Google Drive downloads,
        including large files that require consent.
    """
    try:
        import gdown  # type: ignore
        return "python"
    except Exception:
        pass
    # Try CLI
    try:
        subprocess.run(["gdown", "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "cli"
    except Exception:
        pass
    # Attempt install
    try:
        sh([sys.executable, "-m", "pip", "install", "--quiet", "gdown"])
        return "python"
    except Exception:
        return None

def download_drive_file(url: str, output_path: Path) -> None:
    """Download a file from Google Drive using the best available method.

    Args:
        url (str): Google Drive URL of the file to download.
        output_path (Path): Path where the downloaded file should be saved.

    Note:
        Uses a two-step approach for maximum reliability:
        1. Attempts to use gdown (either Python module or CLI tool)
        2. Falls back to direct download URL for small files if gdown fails

    Raises:
        RuntimeError: If all download attempts fail.
    """
    file_id = extract_drive_file_id(url)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Try gdown
    mode = _ensure_gdown_available()
    if mode == "python":
        import gdown  # type: ignore
        gdown.download(id=file_id or url, output=str(output_path), quiet=False)
        if output_path.exists():
            return
    elif mode == "cli":
        target = file_id if file_id else url
        sh(["gdown", "--fuzzy", target, "-O", str(output_path)])
        if output_path.exists():
            return

    # Fallback: curl direct link (simple cases)
    if not file_id:
        raise RuntimeError(f"Cannot extract Google Drive file id from: {url}")
    direct = f"https://drive.google.com/uc?export=download&id={file_id}"
    sh(["curl", "-fL", "-o", str(output_path), direct])


# --------------------------- task folder prep ---------------------------

@dataclass
class TaskPaths:
    """Container for paths to task-related files.

    Attributes:
        root (Path): Root directory of the task folder.
        dockerfile (Path): Path to the task's Dockerfile.
        task_md (Path): Path to the task's markdown description file.
        test_cmd (Path): Path to the file containing test commands.
        test_patch (Path): Path to the task's test patch file.
    """
    root: Path
    dockerfile: Path
    task_md: Path
    test_cmd: Path
    test_patch: Path

def prepare_task_folder(base_dir: Path, row: Dict[str, str]) -> TaskPaths:
    """Create and populate a task directory with required files.

    Creates a directory named 'task_id_<ID>' under base_dir and populates it with:
    1. task.md - Task description file
    2. Dockerfile - From Drive link or local path
    3. test_command.txt - Test command specification
    4. test_patch.tar - Optional test patch file from Drive link

    Args:
        base_dir (Path): Base directory where task folder will be created.
        row (Dict[str, str]): Task data from spreadsheet with following keys:
            - task_id: Unique identifier for the task
            - updated_issue_description: Content for task.md
            - dockerfile: Source path/URL for Dockerfile
            - test_command: Content for test_command.txt
            - test_patch: Optional source path/URL for test patch

    Returns:
        TaskPaths: Object containing paths to all created files.

    Raises:
        ValueError: If required fields are missing from row.
        RuntimeError: If file downloads fail.
    """
    task_id = str(row["task_id"]).strip()
    root = base_dir / f"task_id_{task_id}"
    root.mkdir(parents=True, exist_ok=True)

    # 1) task.md
    task_md = root / "task.md"
    desc = row.get("updated_issue_description", "").strip()
    # strip surrounding quotes if they exist
    if len(desc) >= 2 and ((desc[0] == desc[-1] == '"') or (desc[0] == desc[-1] == "'")):
        desc = desc[1:-1]
    task_md.write_text(desc + ("\n" if not desc.endswith("\n") else ""), encoding="utf-8")

    # 2) Dockerfile (from Drive link or local path)
    dockerfile_target = root / "Dockerfile"
    docker_src = row.get("dockerfile", "").strip()
    if docker_src:
        if _is_http_url(docker_src):
            download_drive_file(docker_src, dockerfile_target)
        else:
            shutil.copyfile(docker_src, dockerfile_target)
    else:
        raise ValueError(f"Row {task_id}: missing 'dockerfile' value")

    # 3) test_command -> save as text (even if we ignore it at runtime)
    test_cmd_path = root / "test_command.txt"
    test_cmd_val = row.get("test_command", "").strip()
    test_cmd_path.write_text(test_cmd_val + ("\n" if not test_cmd_val.endswith("\n") else ""), encoding="utf-8")

    # 4) test_patch (.tar from Drive link, optional)
    test_patch_path = root / "test_patch.tar"
    test_patch_src = row.get("test_patch", "").strip()
    if test_patch_src:
        if _is_http_url(test_patch_src):
            download_drive_file(test_patch_src, test_patch_path)
        else:
            shutil.copyfile(test_patch_src, test_patch_path)

    return TaskPaths(
        root=root,
        dockerfile=dockerfile_target,
        task_md=task_md,
        test_cmd=test_cmd_path,
        test_patch=test_patch_path,
    )
