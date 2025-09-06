#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from container_helpers import (
    sh, ensure_command, ensure_model_key,
    build_base_image, find_repo_dir,
)


def main() -> None:
    """Run Trae Agent using an existing Docker image with a baked-in repo.

    This runner does not build an overlay nor create symlinks; it relies on
    the image's configured repo working directory.

    Raises:
        SystemExit: If required commands are missing or execution fails.
    """
    ap = argparse.ArgumentParser(description="Run Trae Agent with a pre-baked repo image (no overlay).")
    ap.add_argument("--dockerfile", type=Path, required=True, help="Path to your base Dockerfile.")
    ap.add_argument("--image-tag", default="myproj:latest", help="Base image tag to build and run.")
    ap.add_argument("--skip-build", action="store_true", help="Skip building the base image.")

    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--prompt-file", type=Path, help="Markdown/text file with task instructions.")
    src.add_argument("--prompt-text", help="Inline text problem statement.")

    ap.add_argument("--provider", required=True, help="Model provider (e.g., openai | anthropic | google | openrouter | doubao | ollama).")
    ap.add_argument("--model", required=True, help="Model name for the provider (e.g., gpt-4o, claude-sonnet-4-20250514, gemini-2.5-flash).")
    ap.add_argument("--max-steps", type=int, help="Max agent steps for this run.")
    ap.add_argument("--working-dir", help="Working directory inside the container; defaults to discovered repo root.")

    args = ap.parse_args()

    ensure_command("docker")
    ensure_command("trae-cli")
    ensure_model_key(f"{args.provider}/{args.model}")

    # 1) Base image
    if not args.skip_build:
        build_base_image(args.dockerfile, args.image_tag)

    # 2) Repo location (no symlink)
    repo_dir, _repo_name = find_repo_dir(args.image_tag, args.dockerfile)
    workdir = args.working_dir or repo_dir

    # 3) Task text
    if args.prompt_file:
        task_text = Path(args.prompt_file).read_text(encoding="utf-8")
    else:
        task_text = args.prompt_text

    # 4) Run Trae Agent (containerized execution via trae-cli)
    cmd = [
        "trae-cli", "run", task_text,
        "--provider", args.provider,
        "--model", args.model,
        "--docker-image", args.image_tag,
        "--working-dir", workdir,
    ]
    if args.max_steps is not None:
        cmd += ["--max-steps", str(args.max_steps)]

    sh(cmd)


if __name__ == "__main__":
    main()
