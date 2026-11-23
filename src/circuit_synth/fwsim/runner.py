"""Find and run the simulation suites of a design.

A suite is a directory with a `tests/` package (unittest) next to the
firmware it tests.  If the directory has a Makefile with a `sim` target it
is built first (that is where the firmware's shared library comes from).
Tests run with the interpreter `cs` runs under, so they can import
`circuit_synth.fwsim`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


def is_suite(path: Path) -> bool:
    return (path / "tests").is_dir() and any((path / "tests").glob("test_*.py"))


def find_suites(path: str | Path, depth: int = 3) -> list[Path]:
    """Suites at `path` or below it (a design directory holds one per firmware)."""
    root = Path(path).resolve()
    if is_suite(root):
        return [root]
    found: list[Path] = []
    if depth > 0:
        for child in sorted(
            p
            for p in root.iterdir()
            if p.is_dir() and not p.name.startswith((".", "build"))
        ):
            found += find_suites(child, depth - 1)
    return found


def has_make_target(path: Path, target: str) -> bool:
    makefile = path / "Makefile"
    return (
        makefile.exists()
        and re.search(rf"^{re.escape(target)}\s*:", makefile.read_text(), re.M)
        is not None
    )


def run_suite(
    suite: Path, pattern: str | None = None, verbose: bool = False, build: bool = True
) -> int:
    if build and has_make_target(suite, "sim"):
        res = subprocess.run(["make", "-s", "sim"], cwd=suite)
        if res.returncode:
            return res.returncode
    cmd = [
        sys.executable,
        "-W",
        "ignore::ResourceWarning",
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-t",
        ".",
    ]
    if pattern:
        cmd += ["-k", pattern]
    if verbose:
        cmd.append("-v")
    return subprocess.run(cmd, cwd=suite).returncode
