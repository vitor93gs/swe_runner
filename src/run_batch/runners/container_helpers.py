#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


def sh(cmd, **kw) -> subprocess.CompletedProcess:
    """Execute a shell command and return its result.

    Args:
        cmd: Command to execute as a string or list of strings.
        **kw: Additional keyword arguments to pass to subprocess.run.

    Returns:
        subprocess.CompletedProcess: Result of the command execution.

    Raises:
        subprocess.CalledProcessError: If the command returns a non-zero exit status.
    """
    print("▶", " ".join(map(str, cmd)))
    return subprocess.run(cmd, check=True, text=True, **kw)


def ensure_command(cmd: str) -> None:
    """Ensure a command is available and working in the system.

    Args:
        cmd (str): Command to check for availability.

    Raises:
        SystemExit: If the command is not found or not working properly.
    """
    try:
        subprocess.run([cmd, "--version"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        print(f"ERROR: required command not found or not working: {cmd}", file=sys.stderr)
        sys.exit(1)


def ensure_model_key(provider_or_model: str) -> None:
    """Ensure appropriate API key is available for the specified provider/model.

    Args:
        provider_or_model (str): Provider or model string (e.g., 'openai/gpt-4o', 'anthropic/claude-…').

    Raises:
        SystemExit: If required API key is not found in environment variables
            or .env.sweagent file.

    Note:
        Loads .env.sweagent if present. Provider-specific checks:
        - 'gemini' or 'google': GEMINI_API_KEY or GOOGLE_API_KEY
        - 'anthropic': ANTHROPIC_API_KEY
        - 'openai': OPENAI_API_KEY
        - 'openrouter': OPENROUTER_API_KEY
        - 'doubao': DOUBAO_API_KEY
        - 'ollama': no key required
        Otherwise accepts any env var containing 'KEY' as fallback.
    """
    # load .env.sweagent if present
    repo_root = Path(__file__).resolve().parents[3]  # up from src/run_batch/runners to repo root
    env_file = repo_root / ".env.sweagent"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

    s = provider_or_model.lower()

    def have(*names: str) -> bool:
        return any(os.getenv(n) for n in names)

    if "ollama" in s:
        return
    if "gemini" in s or "google" in s:
        if have("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            return
        print("ERROR: Missing GEMINI_API_KEY (or GOOGLE_API_KEY).", file=sys.stderr)
        sys.exit(1)
    if "anthropic" in s:
        if have("ANTHROPIC_API_KEY"):
            return
        print("ERROR: Missing ANTHROPIC_API_KEY.", file=sys.stderr)
        sys.exit(1)
    if "openai" in s:
        if have("OPENAI_API_KEY"):
            return
        print("ERROR: Missing OPENAI_API_KEY.", file=sys.stderr)
        sys.exit(1)
    if "openrouter" in s:
        if have("OPENROUTER_API_KEY"):
            return
        print("ERROR: Missing OPENROUTER_API_KEY.", file=sys.stderr)
        sys.exit(1)
    if "doubao" in s:
        if have("DOUBAO_API_KEY"):
            return
        print("ERROR: Missing DOUBAO_API_KEY.", file=sys.stderr)
        sys.exit(1)

    # fallback: any *KEY* present
    if not any("KEY" in k for k in os.environ.keys()):
        print("ERROR: No API key variables detected (env or .env.sweagent).", file=sys.stderr)
        sys.exit(1)


def build_base_image(dockerfile: Path, image_tag: str) -> None:
    """Build the base Docker image.

    Args:
        dockerfile (Path): Path to the base Dockerfile.
        image_tag (str): Tag to assign to the built image.

    Raises:
        subprocess.CalledProcessError: If the build fails.
    """
    ctx = dockerfile.resolve().parent
    sh(["docker", "build", "-f", str(dockerfile), "-t", image_tag, str(ctx)])


def image_workdir(image: str) -> str | None:
    """Get the working directory configured in a Docker image.

    Args:
        image (str): Name/tag of the Docker image to inspect.

    Returns:
        str | None: Configured WorkingDir from the image metadata,
            or None if not set or if inspection fails.
    """
    try:
        out = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Config.WorkingDir}}"],
            check=True, capture_output=True, text=True
        ).stdout.strip()
        return out or None
    except Exception:
        return None


def infer_repo_dir_from_dockerfile(dockerfile: Path) -> str | None:
    """Extract the final working directory path from a Dockerfile.

    Args:
        dockerfile (Path): Path to the Dockerfile to analyze.

    Returns:
        str | None: The last WORKDIR path specified in the Dockerfile,
            with quotes and trailing slashes removed. Returns None if no
            WORKDIR is found or file cannot be read.
    """
    try:
        txt = dockerfile.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    matches = list(re.finditer(r"(?i)^\s*WORKDIR\s+(.+)$", txt, flags=re.MULTILINE))
    if not matches:
        return None
    final_workdir = matches[-1].group(1).strip().strip("'").strip('"').rstrip("/")
    return final_workdir or None


def find_repo_dir(image_tag: str, dockerfile: Path) -> tuple[str, str]:
    """Determine repository directory and name inside the container.

    Args:
        image_tag (str): Tag of the built image.
        dockerfile (Path): Path to the Dockerfile (fallback for WORKDIR).

    Returns:
        tuple[str, str]: (repo_dir absolute path, repo_name).

    Note:
        Prefers image WorkingDir; falls back to Dockerfile WORKDIR; else /app.
    """
    repo_dir = image_workdir(image_tag) or infer_repo_dir_from_dockerfile(dockerfile) or "/app"
    if not repo_dir.startswith("/"):
        repo_dir = "/" + repo_dir
    repo_name = Path(repo_dir).name or "app"
    return repo_dir, repo_name
