#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import subprocess
from pathlib import Path

from container_helpers import (
    sh, ensure_command, ensure_model_key,
    build_base_image, find_repo_dir,
)


# --------------------------- SWE-only helpers (overlay/symlink/git) ---------------------------

def detect_os_family(image: str) -> str:
    """Detect the operating system family of a Docker image.

    Args:
        image (str): Name/tag of the Docker image to check.

    Returns:
        str: One of 'debian', 'alpine', 'rhel', or 'unknown'.
    """
    try:
        out = subprocess.run(
            ["docker", "run", "--rm", image, "sh", "-lc", "cat /etc/os-release 2>/dev/null || true"],
            check=True, capture_output=True, text=True
        ).stdout.lower()
    except Exception:
        return "unknown"
    if "alpine" in out:
        return "alpine"
    if "debian" in out or "ubuntu" in out:
        return "debian"
    if any(x in out for x in ("rhel", "fedora", "centos", "rocky")):
        return "rhel"
    return "unknown"


def build_overlay_with_rex(base_image: str, overlay_tag: str) -> None:
    """Build a Docker image overlay with SWE runtime dependencies.

    Creates a new Docker image that adds:
        - Python 3, pipx (with python shim if needed)
        - Git, curl, CA certificates (per OS family)
        - swe-rex via pipx
        - ~/.local/bin added to PATH

    Args:
        base_image (str): Name/tag of the base Docker image to build upon.
        overlay_tag (str): Tag to assign to the resulting overlay image.

    Raises:
        subprocess.CalledProcessError: If Docker build fails.
    """
    fam = detect_os_family(base_image)

    lines = [f"FROM {base_image}", "USER root", 'SHELL ["/bin/sh", "-lc"]']

    if fam == "debian":
        lines += [
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            "python3 python3-venv pipx git curl ca-certificates && "
            "rm -rf /var/lib/apt/lists/*",
            'ENV PATH="/root/.local/bin:${PATH}"',
            'RUN command -v python >/dev/null 2>&1 || ln -sf \"$(command -v python3)\" /usr/local/bin/python',
            "RUN pipx install swe-rex",
        ]
    elif fam == "alpine":
        lines += [
            "RUN apk add --no-cache python3 py3-pip py3-virtualenv git curl ca-certificates",
            'ENV PATH="/root/.local/bin:${PATH}"',
            (
                "RUN apk add --no-cache py3-pipx || ("
                "python3 -m venv /opt/pipx && "
                "/opt/pipx/bin/python -m pip install --upgrade pip && "
                "/opt/pipx/bin/python -m pip install pipx && "
                "ln -sf /opt/pipx/bin/pipx /usr/local/bin/pipx"
                ")"
            ),
            'RUN command -v python >/dev/null 2>&1 || ln -sf \"$(command -v python3)\" /usr/local/bin/python',
            "RUN pipx install swe-rex",
        ]
    elif fam == "rhel":
        lines += [
            "RUN (microdnf install -y python3 python3-pip python3-virtualenv git curl ca-certificates || "
            " dnf install -y python3 python3-pip python3-virtualenv git curl ca-certificates || "
            " yum install -y python3 python3-pip python3-virtualenv git curl ca-certificates)",
            "RUN python3 -m ensurepip --upgrade || true",
            "RUN python3 -m pip install --upgrade pip --break-system-packages || true",
            "RUN python3 -m pip install pipx --break-system-packages || true",
            'ENV PATH="/root/.local/bin:${PATH}"',
            'RUN command -v python >/dev/null 2>&1 || ln -sf \"$(command -v python3)\" /usr/local/bin/python',
            "RUN pipx install swe-rex",
        ]
    else:
        lines += [
            "RUN python3 -m ensurepip --upgrade || true",
            "RUN python3 -m pip install --upgrade pip || true",
            "RUN python3 -m pip install pipx || true",
            'ENV PATH="/root/.local/bin:${PATH}"',
            'RUN command -v python >/dev/null 2>&1 || (command -v python3 >/dev/null 2>&1 && ln -sf \"$(command -v python3)\" /usr/local/bin/python) || true',
            "RUN pipx install swe-rex || true",
        ]

    dockerfile = "\n".join(lines)
    with tempfile.TemporaryDirectory() as tmp:
        df = Path(tmp) / "Dockerfile"
        df.write_text(dockerfile, encoding="utf-8")
        sh(["docker", "build", "-f", str(df), "-t", overlay_tag, tmp])


def add_repo_symlink_to_overlay(overlay_tag: str, repo_dir: str, repo_name: str) -> None:
    """Add a repository symlink to a Docker overlay image.

    Creates a new Docker image layer that adds a symbolic link from /<repo_name>
    to the actual repository directory, enabling SWE-agent to navigate to the
    repository using a consistent path.

    Args:
        overlay_tag (str): Tag of the Docker overlay image to modify.
        repo_dir (str): Target directory path in the container.
        repo_name (str): Name to use for the symlink at root level.

    Raises:
        subprocess.CalledProcessError: If Docker build fails.
    """
    dockerfile = (
        f"FROM {overlay_tag}\n"
        'SHELL ["/bin/sh","-lc"]\n'
        f'RUN ln -sfn "{repo_dir}" "/{repo_name}" || true\n'
    )
    with tempfile.TemporaryDirectory() as tmp:
        df = Path(tmp) / "Dockerfile"
        df.write_text(dockerfile, encoding="utf-8")
        sh(["docker", "build", "-f", str(df), "-t", overlay_tag, tmp])


def container_repo_has_git(image: str, repo_dir: str) -> bool:
    """Check if a Docker container has both a Git repository and Git binary.

    Args:
        image (str): Name/tag of the Docker image to check.
        repo_dir (str): Path to directory expected to contain .git folder.

    Returns:
        bool: True if .git exists in repo_dir and git is available; otherwise False.
    """
    try:
        out = subprocess.run(
            ["docker", "run", "--rm", "-w", f"{repo_dir}", image, "sh", "-lc",
             'test -d .git && command -v git >/dev/null 2>&1 && echo yes || echo no'],
            check=True, capture_output=True, text=True
        ).stdout.strip()
        return out == "yes"
    except Exception:
        return False


def ensure_sweagent_from_source(python_exe: str, local_src: Path | None, ref: str) -> None:
    """Ensure SWE-agent package is available, installing from source if needed.

    Args:
        python_exe (str): Python executable path.
        local_src (Path | None): Local SWE-agent repo path (editable install).
        ref (str): Git reference (branch/tag/commit) to checkout.

    Raises:
        ImportError: If sweagent cannot be imported after install.
        subprocess.CalledProcessError: On git/pip failures.
    """
    try:
        __import__("sweagent")
        return
    except Exception:
        pass

    ensure_command("git")
    if local_src is None:
        tmpdir = tempfile.mkdtemp(prefix="sweagent-src-")
        local_src = Path(tmpdir)
        sh(["git", "clone", "--depth", "1", "--branch", ref,
            "https://github.com/SWE-agent/SWE-agent.git", str(local_src)])
    else:
        local_src = local_src.resolve()
        if not (local_src / ".git").exists():
            sh(["git", "clone", "https://github.com/SWE-agent/SWE-agent.git", str(local_src)])
        sh(["git", "-C", str(local_src), "checkout", ref])

    sh([python_exe, "-m", "pip", "install", "--upgrade", "pip"])
    sh([python_exe, "-m", "pip", "install", "--editable", str(local_src)])
    __import__("sweagent")


def locate_default_cfg() -> str:
    """Locate the default configuration file for SWE-agent.

    Returns:
        str: Absolute path to sweagent/config/default.yaml.

    Raises:
        FileNotFoundError: If default.yaml cannot be found.
    """
    root = Path(sys.modules["sweagent"].__file__).resolve().parent.parent
    cfg = root / "config" / "default.yaml"
    if not cfg.exists():
        raise FileNotFoundError(f"Cannot locate SWE-agent default config at {cfg}")
    return str(cfg)


def make_cacheless_override() -> str:
    """Create a temporary configuration file that disables caching.

    Returns:
        str: Path to the temporary configuration file.

    Note:
        Caller is responsible for deleting the file.
    """
    yml = "agent:\n  history_processors: []\n"
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    tmp.write(yml)
    tmp.flush()
    tmp.close()
    return tmp.name


def main() -> None:
    """Execute the main SWE runner workflow.

    Sets up and runs SWE-agent with a repository pre-baked in a Docker image.

    Raises:
        SystemExit: If required commands are missing or execution fails.
    """
    ap = argparse.ArgumentParser(description="Run SWE-agent in preexisting-repo Docker mode.")
    ap.add_argument("--dockerfile", type=Path, required=True, help="Path to your base Dockerfile.")
    ap.add_argument("--image-tag", default="myproj:latest", help="Base image tag to build and run.")
    ap.add_argument("--skip-build", action="store_true", help="Skip building the base image.")
    ap.add_argument("--overlay-tag", default=None, help="Optional tag for the overlay image; default: <image>:with-rex")

    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--prompt-file", type=Path, help="Markdown/text file with task instructions.")
    src.add_argument("--prompt-text", help="Inline text problem statement.")

    ap.add_argument("--model", required=True, help="LiteLLM model string (e.g., gemini/gemini-2.5-pro)")
    ap.add_argument("--cost-limit", type=float, default=3.0, help="Per-instance $ cost cap (default 3.0).")
    ap.add_argument("--call-limit", type=int, default=0, help="Per-instance API call cap (0 = unlimited).")
    ap.add_argument("--base-commit", default="HEAD", help="Commit/branch/tag to reset to (default HEAD).")

    ap.add_argument("--instance-id", help="Set problem statement ID.")
    ap.add_argument("--output-dir", type=Path, help="Custom output directory for SWE-agent artifacts.")

    # power-user (optional)
    ap.add_argument("--sweagent-src", type=Path, help="Local SWE-agent repo path to use for editable install.")
    ap.add_argument("--sweagent-ref", default="main", help="Git ref (branch/tag) to use for SWE-agent source.")

    args = ap.parse_args()
    py = sys.executable

    ensure_command("docker")
    ensure_model_key(args.model)

    # 1) Base image
    if not args.skip_build:
        build_base_image(args.dockerfile, args.image_tag)

    # 2) Overlay (with swe-rex)
    if args.overlay_tag:
        overlay_tag = args.overlay_tag
    else:
        # derive overlay tag
        img_tail = args.image_tag.rsplit("/", 1)[-1]
        if ":" in img_tail:
            base, tag = args.image_tag.rsplit(":", 1)
            overlay_tag = f"{base}:{tag}-with-rex"
        else:
            overlay_tag = f"{args.image_tag}:with-rex"
    build_overlay_with_rex(args.image_tag, overlay_tag)

    # 3) Ensure SWE-agent importable locally
    ensure_sweagent_from_source(py, args.sweagent_src, args.sweagent_ref)

    # 4) Configs
    default_cfg = locate_default_cfg()
    cacheless_cfg = make_cacheless_override()

    # 5) Repo discovery and alias
    repo_dir, repo_name = find_repo_dir(args.image_tag, args.dockerfile)
    add_repo_symlink_to_overlay(overlay_tag, repo_dir, repo_name)

    # 6) Git reset capability
    can_reset = container_repo_has_git(overlay_tag, repo_dir)

    # 7) Compose run cmd
    cmd = [
        py, "-m", "sweagent", "run",
        "--config", default_cfg,
        "--config", cacheless_cfg,
        f"--agent.model.name={args.model}",
        f"--agent.model.per_instance_cost_limit={args.cost_limit:.2f}",
        f"--agent.model.per_instance_call_limit={args.call_limit}",
        f"--env.deployment.image={overlay_tag}",
        "--env.repo.type=preexisting",
        f"--env.repo.repo_name={repo_name}",
        f"--env.repo.base_commit={args.base_commit}",
    ]

    if args.output_dir:
        out_path = Path(args.output_dir).resolve()
        out_path.mkdir(parents=True, exist_ok=True)
        cmd.append(f"--output_dir={str(out_path)}")
    if args.instance_id:
        cmd.append(f"--problem_statement.id={args.instance_id}")
    if not can_reset:
        cmd.append("--env.repo.reset=False")

    if args.prompt_file:
        cmd += [f"--problem_statement.path={str(args.prompt_file.resolve())}", "--problem_statement.type=text_file"]
    else:
        cmd += [f"--problem_statement.text={args.prompt_text}", "--problem_statement.type=text"]

    try:
        sh(cmd)
    finally:
        try:
            os.remove(cacheless_cfg)
        except Exception:
            pass


if __name__ == "__main__":
    main()
