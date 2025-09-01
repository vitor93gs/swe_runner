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
    print("▶", " ".join(map(str, cmd)))
    return subprocess.run(cmd, check=True, text=True, **kw)


def ensure(cmd: str) -> None:
    try:
        subprocess.run([cmd, "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        print(f"ERROR: required command not found or not working: {cmd}", file=sys.stderr)
        sys.exit(1)


def ensure_model_key(model: str) -> None:
    """Require a reasonable provider key based on the model string."""
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
    """Return: debian | alpine | rhel | unknown"""
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
    """Take the last WORKDIR from the Dockerfile and return its full path."""
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
    """
    Return the image's configured WorkingDir via `docker image inspect`.
    This is authoritative and survives multi-stage builds.
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
    """
    Check inside the image whether /<repo_dir> has a .git directory AND the git binary exists.
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
    """
    Build a tiny overlay that:
      - Installs Python 3 + pipx (+ python shim), git, curl, ca-certs (OS-family aware)
      - Installs swe-rex via pipx
      - Exposes ~/.local/bin on PATH
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
            "RUN apk add --no-cache python3 py3-pip py3-virtualenv git curl ca-certificates",
            'ENV PATH="/root/.local/bin:${PATH}"',
            # install pipx via pip (no ensurepath needed in Docker)
            "RUN python3 -m pip install --no-cache-dir pipx",
            'RUN command -v python >/dev/null 2>&1 || ln -sf "$(command -v python3)" /usr/local/bin/python',
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
    """
    Create /<repo_name> -> <repo_dir> inside the overlay so SWE-agent can `cd /<repo_name>`.
    This is a tiny "one-layer" image on top of the overlay.
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
    """Ensure `sweagent` imports; if not, install editable from source (stable and reliable)."""
    try:
        __import__("sweagent")
        return
    except Exception:
        pass

    ensure("git")
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
    """Find installed SWE-agent default.yaml (we pass it first, then a tiny override)."""
    root = Path(sys.modules["sweagent"].__file__).resolve().parent.parent
    cfg = root / "config" / "default.yaml"
    if not cfg.exists():
        raise FileNotFoundError(f"Cannot locate SWE-agent default config at {cfg}")
    return str(cfg)


def make_cacheless_override() -> str:
    """
    Disable history processors (e.g., cache_control) to sidestep provider cache minimums by default.
    """
    yml = "agent:\n  history_processors: []\n"
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    tmp.write(yml)
    tmp.flush()
    tmp.close()
    return tmp.name


# --------------------------- CLI ---------------------------

def main():
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

    ensure("docker")
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