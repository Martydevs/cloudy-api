"""Verify yt-dlp's runtime dependencies (run at Docker build time).

Requirements come from the yt-dlp README ("DEPENDENCIES") and the EJS wiki:
- ffmpeg + ffprobe binaries: merging DASH video+audio, MP3 extraction.
- yt-dlp-ejs at the exact version yt-dlp pins: YouTube JS challenge solver.
- A JS runtime to run it: deno >= 2.3, node >= 22, quickjs >= 2023-12-09
  (any quickjs-ng), bun 1.2.11-1.3.14 (deprecated).

Usage: python scripts/check_deps.py [--runtimes deno,node]
Exits non-zero, listing every problem, if something is missing or too old.
"""

import argparse
import re
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, requires, version

MIN_RUNTIME_VERSIONS: dict[str, tuple[int, ...]] = {
    "deno": (2, 3, 0),
    "node": (22, 0, 0),
    "bun": (1, 2, 11),
}
# EJS wiki: "latest supported 1.3.14"; yt-dlp itself only enforces the minimum.
MAX_TESTED_VERSIONS: dict[str, tuple[int, ...]] = {"bun": (1, 3, 14)}
VERSION_COMMANDS: dict[str, tuple[list[str], str]] = {
    "deno": (["deno", "--version"], r"^deno (\d+(?:\.\d+)*)"),
    "node": (["node", "--version"], r"^v(\d+(?:\.\d+)*)"),
    "bun": (["bun", "--version"], r"^(\d+(?:\.\d+)*)"),
}


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    return (result.stdout or result.stderr).strip()


def check_ffmpeg() -> list[str]:
    problems = []
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            problems.append(f"{tool} not found on PATH")
        else:
            print(f"ok  {_run([tool, '-hide_banner', '-version']).splitlines()[0]}")
    return problems


def check_ejs() -> list[str]:
    try:
        have = version("yt-dlp-ejs")
    except PackageNotFoundError:
        return ["yt-dlp-ejs is not installed (install yt-dlp[default])"]
    pins = [r for r in requires("yt-dlp") or [] if r.startswith("yt-dlp-ejs")]
    match = re.search(r"==\s*([\w.]+)", pins[0]) if pins else None
    if match and match.group(1) != have:
        return [f"yt-dlp-ejs {have} installed, but yt-dlp {version('yt-dlp')} pins {match.group(1)}"]
    print(f"ok  yt-dlp {version('yt-dlp')}, yt-dlp-ejs {have}")
    return []


def check_runtime(name: str) -> list[str]:
    if name == "quickjs":
        if shutil.which("qjs") is None:
            return ["quickjs: `qjs` not found on PATH"]
        print("ok  quickjs (qjs) found; version not checked")
        return []
    if name not in VERSION_COMMANDS:
        return [f"unknown JS runtime {name!r}"]
    cmd, pattern = VERSION_COMMANDS[name]
    if shutil.which(cmd[0]) is None:
        return [f"{name} not found on PATH"]
    output = _run(cmd)
    match = re.search(pattern, output, re.MULTILINE)
    if not match:
        return [f"{name}: could not read version from {output!r}"]
    found = _version_tuple(match.group(1))
    minimum = MIN_RUNTIME_VERSIONS[name]
    if found < minimum:
        need = ".".join(map(str, minimum))
        return [f"{name} {match.group(1)} is too old for yt-dlp (needs >= {need})"]
    maximum = MAX_TESTED_VERSIONS.get(name)
    if maximum and found > maximum:
        top = ".".join(map(str, maximum))
        print(f"WARN {name} {match.group(1)} is newer than yt-dlp supports (<= {top})")
        return []
    print(f"ok  {name} {match.group(1)}")
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--runtimes",
        default="deno,node",
        help="comma-separated JS runtimes that must be present (default: deno,node)",
    )
    args = parser.parse_args(argv)

    problems = check_ffmpeg() + check_ejs()
    for name in filter(None, (n.strip() for n in args.runtimes.split(","))):
        problems += check_runtime(name)

    for problem in problems:
        print(f"ERR {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
