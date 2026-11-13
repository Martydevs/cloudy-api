"""Verify the download engine's runtime dependencies (run at Docker build time).

- ffmpeg + ffprobe binaries: pytubefix doesn't merge streams, so the service
  runs ffmpeg itself to merge adaptive video+audio and to convert MP3.
- pytubefix's bundled Node.js (``nodejs-wheel-binaries``): runs BotGuard to
  generate the PO tokens that get past YouTube's bot check.

Usage: python scripts/check_deps.py
Exits non-zero, listing every problem, if something is missing or broken.
"""

import os
import shutil
import subprocess
import sys


def _run(cmd: list[str]) -> tuple[int, str]:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    return result.returncode, (result.stdout or result.stderr).strip()


def check_ffmpeg() -> list[str]:
    problems = []
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            problems.append(f"{tool} not found on PATH")
        else:
            print(f"ok  {_run([tool, '-hide_banner', '-version'])[1].splitlines()[0]}")
    return problems


def check_pytubefix_node() -> list[str]:
    try:
        from pytubefix.botGuard.bot_guard import NODE_PATH
        from pytubefix.version import __version__ as pytubefix_version
    except ImportError as exc:
        return [f"pytubefix is not installed ({exc})"]
    if not os.path.isfile(NODE_PATH):
        return [f"pytubefix's bundled Node.js not found at {NODE_PATH}"]
    # Running it also catches missing shared libraries (libstdc++6).
    try:
        code, output = _run([NODE_PATH, "--version"])
    except OSError as exc:
        return [f"pytubefix's bundled Node.js at {NODE_PATH} can't run: {exc}"]
    if code != 0:
        return [f"pytubefix's bundled Node.js at {NODE_PATH} failed: {output}"]
    print(f"ok  pytubefix {pytubefix_version}, bundled node {output}")
    return []


def main() -> int:
    problems = check_ffmpeg() + check_pytubefix_node()
    for problem in problems:
        print(f"ERR {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
