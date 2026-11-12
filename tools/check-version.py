#!/usr/bin/env python3
"""check-version.py — one version string, three places, or the build fails.

CT-10 in docs/ROADMAP.md. `pyproject.toml` declares it, `kosh/__init__.py` ships
it, and `docs/assets/badge-version.svg` advertises it. Nothing compared the three,
so a bump that missed one would ship a footer that lies — which is the failure this
project's badge rule exists to prevent.

    python tools/check-version.py     # exit 0 = they agree
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(pattern: str, path: str) -> str | None:
    match = re.search(pattern, (ROOT / path).read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else None


def main() -> int:
    found = {
        "pyproject.toml": read(r'^version\s*=\s*"([^"]+)"', "pyproject.toml"),
        "kosh/__init__.py": read(r'__version__\s*=\s*"([^"]+)"', "kosh/__init__.py"),
        "docs/assets/badge-version.svg": read(r"version: ([0-9][0-9.]*)", "docs/assets/badge-version.svg"),
    }
    for where, value in found.items():
        print(f"{where:32} {value or '<not found>'}")

    missing = [where for where, value in found.items() if not value]
    if missing:
        sys.exit(f"no version found in: {', '.join(missing)}")

    distinct = set(found.values())
    if len(distinct) != 1:
        sys.exit(
            f"versions disagree: {sorted(distinct)}. "
            "Bump all three (tools/badges.py --version writes the badge)."
        )
    print(f"version agrees: {distinct.pop()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
