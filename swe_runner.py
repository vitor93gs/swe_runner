#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


# --------------------------- shell helpers ---------------------------

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
        subprocess.run([cmd, "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        print(f"ERROR: required command not found or not working: {cmd}", file=sys.stderr)
        sys.exit(1)


def ensure_model_key(model: str) -> None:
    """Ensure appropriate API key is available for the specified model.

    Args:
        model (str): Name of the model to check API key for (e.g., 'gemini').

    Raises:
        SystemExit: If required API key is not found in environment variables
            or .env.sweagent file.

    Note:
        For Gemini models, looks for GEMINI_API_KEY or GOOGLE_API_KEY.
        For other models, checks for any environment variable containing 'KEY'.
    """
    model_l = model.lower()
    # load .env.sweagent if present
    env_file = Path(".env.sweagent")
    if env_file.exists():
        with env_file.open() as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    have = {k for k in os.environ if "KEY" in k}
    if "gemini" in model_l:
        if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
            return
        print("ERROR: Put GEMINI_API_KEY (or GOOGLE_API_KEY) in .env.sweagent for Gemini models.", file=sys.stderr)
        sys.exit(1)
    if not have:
        print("ERROR: No API key variables detected (looked in .env.sweagent).", file=sys.stderr)
        sys.exit(1)


# --------------------------- image / container helpers ---------------------------

def detect_os_family(image: str) -> str:
    """Detect the operating system family of a Docker image.

    Args:
        image (str): Name/tag of the Docker image to check.

    Returns:
        str: Operating system family name, one of:
            - 'debian': For Debian/Ubuntu based images
            - 'alpine': For Alpine Linux based images
            - 'rhel': For RHEL/Fedora/CentOS/Rocky based images
            - 'unknown': If OS family cannot be determined
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
    if "rhel" in out or "fedora" in out or "centos" in out or "rocky" in out:
        return "rhel"
    return "unknown"


def infer_repo_dir_from_dockerfile(dockerfile: Path) -> str | None:
    """Extract the final working directory path from a Dockerfile.

    Args:
        dockerfile (Path): Path to the Dockerfile to analyze.

    Returns:
        str | None: The last WORKDIR path specified in the Dockerfile,
            with quotes and trailing slashes removed.
            Returns None if no WORKDIR is found or file cannot be read.
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

def image_workdir(image: str) -> str | None:
    """Get the working directory configured in a Docker image.

    Args:
        image (str): Name/tag of the Docker image to inspect.

    Returns:
        str | None: The configured WorkingDir from the image metadata,
            or None if not set or if inspection fails.

    Note:
        This method uses `docker image inspect` and is authoritative,
        working correctly even with multi-stage builds.
    """
    try:
        out = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Config.WorkingDir}}"],
            check=True, capture_output=True, text=True
        ).stdout.strip()
        return out or None
    except Exception:
        return None

def container_repo_has_git(image: str, repo_dir: str) -> bool:
    """Check if a Docker container has both a Git repository and Git binary.

    Args:
        image (str): Name/tag of the Docker image to check.
        repo_dir (str): Path to directory expected to contain .git folder.

    Returns:
        bool: True if both .git directory exists in repo_dir AND git binary
            is available in the container's PATH, False otherwise.
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


# --------------------------- overlay builder ---------------------------

def build_overlay_with_rex(base_image: str, overlay_tag: str) -> None:
    """Build a Docker image overlay with SWE runtime dependencies.

    Creates a new Docker image that adds the following to the base image:
        - Python 3, pipx (with python shim if needed)
        - Git, curl, and CA certificates (installed appropriately for OS family)
        - swe-rex package installed via pipx
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
            # pipx from apt; no `python3 -m pipx`.
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            "python3 python3-venv pipx git curl ca-certificates && "
            "rm -rf /var/lib/apt/lists/*",
            'ENV PATH="/root/.local/bin:${PATH}"',
            # provide `python` shim if only python3 exists
            'RUN command -v python >/dev/null 2>&1 || ln -sf "$(command -v python3)" /usr/local/bin/python',
            # install the runtime CLI for SWE-agent
            "RUN pipx install swe-rex",
        ]
    elif fam == "alpine":
        lines += [
            # base toolchain
            "RUN apk add --no-cache python3 py3-pip py3-virtualenv git curl ca-certificates",
            'ENV PATH="/root/.local/bin:${PATH}"',
            # try system pipx; if unavailable (Alpine 3.19), create a dedicated venv for pipx
            (
                "RUN apk add --no-cache py3-pipx || ("
                "python3 -m venv /opt/pipx && "
                "/opt/pipx/bin/python -m pip install --upgrade pip && "
                "/opt/pipx/bin/python -m pip install pipx && "
                "ln -sf /opt/pipx/bin/pipx /usr/local/bin/pipx"
                ")"
            ),
            # provide `python` shim if only python3 exists
            'RUN command -v python >/dev/null 2>&1 || ln -sf "$(command -v python3)" /usr/local/bin/python',
            # install the runtime CLI for SWE-agent
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
            'RUN command -v python >/dev/null 2>&1 || ln -sf "$(command -v python3)" /usr/local/bin/python',
            "RUN pipx install swe-rex",
        ]
    else:
        # best-effort for unknown bases
        lines += [
            "RUN python3 -m ensurepip --upgrade || true",
            "RUN python3 -m pip install --upgrade pip || true",
            "RUN python3 -m pip install pipx || true",
            'ENV PATH="/root/.local/bin:${PATH}"',
            'RUN command -v python >/dev/null 2>&1 || (command -v python3 >/dev/null 2>&1 && ln -sf "$(command -v python3)" /usr/local/bin/python) || true',
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
        # Create/overwrite a root-level alias so SWE-agent can `cd /<repo_name>`
        f'RUN ln -sfn "{repo_dir}" "/{repo_name}" || true\n'
    )
    with tempfile.TemporaryDirectory() as tmp:
        df = Path(tmp) / "Dockerfile"
        df.write_text(dockerfile, encoding="utf-8")
        sh(["docker", "build", "-f", str(df), "-t", overlay_tag, tmp])

# --------------------------- SWE-agent bootstrap ---------------------------

def ensure_sweagent_from_source(python_exe: str, local_src: Path | None, ref: str) -> None:
    """Ensure SWE-agent package is available, installing from source if needed.

    Attempts to import sweagent module and if not available, clones and installs
    it from source in editable mode.

    Args:
        python_exe (str): Path to Python executable to use for installation.
        local_src (Path | None): Path to existing local source directory, if any.
        ref (str): Git reference (branch/tag/commit) to use when cloning.

    Raises:
        ImportError: If sweagent module cannot be imported after installation.
        subprocess.CalledProcessError: If git clone or pip install fails.
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
        sh(["git", "clone", "--depth", "1", "--branch", ref, "https://github.com/SWE-agent/SWE-agent.git", str(local_src)])
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

    Searches for the default.yaml configuration file in the installed
    sweagent package directory.

    Returns:
        str: Absolute path to the default configuration file.

    Raises:
        FileNotFoundError: If default.yaml cannot be found in the expected location.
    """
    root = Path(sys.modules["sweagent"].__file__).resolve().parent.parent
    cfg = root / "config" / "default.yaml"
    if not cfg.exists():
        raise FileNotFoundError(f"Cannot locate SWE-agent default config at {cfg}")
    return str(cfg)


def make_cacheless_override() -> str:
    """Create a temporary configuration file that disables caching.

    Creates a YAML configuration file that disables history processors,
    particularly cache_control, to bypass provider-imposed cache minimums.

    Returns:
        str: Path to the temporary configuration file.

    Note:
        The created file is not automatically deleted and should be managed
        by the caller.
    """
    yml = "agent:\n  history_processors: []\n"
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    tmp.write(yml)
    tmp.flush()
    tmp.close()
    return tmp.name


# --------------------------- CLI ---------------------------

def main() -> None:
    """Execute the main SWE runner workflow.

    Sets up and runs the SWE agent with a repository pre-baked in a Docker image.
    This includes:
    1. Building the base Docker image (if not skipped)
    2. Creating an overlay image with SWE runtime dependencies
    3. Installing and configuring the SWE agent
    4. Running the agent with the specified task and configuration

    Command-line arguments control all aspects of the execution, including:
    - Docker image building and configuration
    - Task specification (via file or inline text)
    - Model selection and resource limits
    - Output and workspace configuration

    Raises:
        SystemExit: If required commands are missing or execution fails.
    """
    ap = argparse.ArgumentParser(description="Run SWE-agent with a repo pre-baked in your Docker image (preexisting mode).")
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

    # 1) Build the base image (unless skipped)
    if not args.skip_build:
        ctx = args.dockerfile.resolve().parent
        sh(["docker", "build", "-f", str(args.dockerfile), "-t", args.image_tag, str(ctx)])

    # 2) Build overlay with swe-rex + toolchain
    if args.overlay_tag:
        overlay_tag = args.overlay_tag
    else:
        # Keep registry/repo; if there's a tag, suffix it; else add ":with-rex"
        img_tail = args.image_tag.rsplit("/", 1)[-1]
        if ":" in img_tail:
            base, tag = args.image_tag.rsplit(":", 1)
            overlay_tag = f"{base}:{tag}-with-rex"
        else:
            overlay_tag = f"{args.image_tag}:with-rex"
    build_overlay_with_rex(args.image_tag, overlay_tag)

    # 3) Ensure SWE-agent is importable locally
    ensure_sweagent_from_source(py, args.sweagent_src, args.sweagent_ref)

    # 4) Configs: default + "cacheless" override
    default_cfg = locate_default_cfg()
    cacheless_cfg = make_cacheless_override()

    # 5) Discover repo dir (prefer the image config WorkingDir; fallback to Dockerfile; else /app)
    repo_dir = image_workdir(args.image_tag) or infer_repo_dir_from_dockerfile(args.dockerfile) or "/app"
    if not repo_dir.startswith("/"):
        repo_dir = "/" + repo_dir
    repo_name = Path(repo_dir).name or "app"

    # Ensure a root-level alias so SWE-agent can cd /<repo_name>
    add_repo_symlink_to_overlay(overlay_tag, repo_dir, repo_name)

    # 6) If the baked repo isn't a git repo (or git missing), disable reset for robustness
    can_reset = container_repo_has_git(overlay_tag, repo_dir)

    # 7) Compose the SWE-agent run command
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