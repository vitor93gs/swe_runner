#!/usr/bin/env python3
from __future__ import annotations

import csv
import io
import os
import re
import sys
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

try:
    import requests  # type: ignore
except Exception:
    requests = None  # we'll fallback to curl if needed


# --------------------------- small shell helper ---------------------------

def sh(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    print("▶", " ".join(map(str, cmd)))
    return subprocess.run(cmd, check=True, text=True, **kw)


# --------------------------- Google Sheets helpers ---------------------------

_SHEETS_HOSTS = {"docs.google.com", "sheets.google.com"}

def _is_http_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")

def _is_google_sheets_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc
        return any(host.endswith(h) for h in _SHEETS_HOSTS) and "/spreadsheets/" in url
    except Exception:
        return False

def _sheets_url_to_csv(url: str) -> str:
    """
    Convert a typical Sheets URL to a CSV export URL.
    - If gid is present, keep it (exports that tab).
    - Otherwise, exports the active tab.
    Ref: CSV export pattern '/export?format=csv' and using gid for a specific sheet. :contentReference[oaicite:0]{index=0}
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
    """
    Load CSV text from either a Google Sheets URL or a local CSV path.
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
    """
    Expect headers:
      task_id, updated_issue_description, dockerfile, test_command, test_patch
    Returns list of dict rows with string values (trimmed).
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
    for pat in _DRIVE_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None

def _ensure_gdown_available() -> Optional[str]:
    """
    Try python API, else 'gdown' CLI. Install if needed.
    gdown is the reliable way to download Drive files (handles large file consent). :contentReference[oaicite:1]{index=1}
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
    """
    Best-effort downloader:
      1) Try gdown (python or CLI)
      2) Fallback to a simple direct /uc?export=download&id=FILE_ID (works for small files)
    Direct link pattern reference. :contentReference[oaicite:2]{index=2}
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
    root: Path
    dockerfile: Path
    task_md: Path
    test_cmd: Path
    test_patch: Path

def prepare_task_folder(base_dir: Path, row: Dict[str, str]) -> TaskPaths:
    """
    Create `tasks/task_id_<ID>/` and materialize all assets.
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
