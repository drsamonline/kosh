#!/usr/bin/env python3
"""Draw the README badges from what is actually in the tree.

Same rule as the rest of the fleet: a badge that cannot be derived is a badge
that will eventually lie.  Everything here is read from a file or passed in from
a command that ran.
"""

from __future__ import annotations

import argparse
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "docs", "assets")

COLOURS = {
    "green": "#2e6b45",
    "blue": "#2f5d7c",
    "amber": "#8a6415",
    "grey": "#3a3a3a",
    "dark": "#14110e",
}


def badge(path: str, label: str, value: str, colour: str = "green") -> None:
    label_w = 7 * len(label) + 10
    value_w = 7 * len(value) + 10
    width = label_w + value_w
    mid = label_w + value_w / 2
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="22" role="img" aria-label="{label}: {value}"><title>{label}: {value}</title><linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#fff" stop-opacity=".08"/><stop offset="1" stop-color="#000" stop-opacity=".10"/></linearGradient><clipPath id="r"><rect width="{width}" height="22" rx="4" fill="#fff"/></clipPath><g clip-path="url(#r)"><rect width="{label_w}" height="22" fill="{COLOURS['dark']}"/><rect x="{label_w}" width="{value_w}" height="22" fill="{COLOURS.get(colour, colour)}"/><rect width="{width}" height="22" fill="url(#s)"/></g><g fill="#f7f2e9" font-family="ui-sans-serif,Segoe UI,Helvetica,Arial,sans-serif" font-size="11" text-anchor="middle"><text x="{label_w / 2}" y="15">{label}</text><text x="{mid}" y="15">{value}</text></g></svg>
"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(svg)


def project_version() -> str:
    with open(os.path.join(ROOT, "kosh", "__init__.py"), encoding="utf-8") as fh:
        match = re.search(r'__version__ = "([^"]+)"', fh.read())
    return match.group(1) if match else "0"


def main() -> None:
    parser = argparse.ArgumentParser(description="redraw the README badges")
    parser.add_argument("--tests", type=int, help="passing test count from a real run")
    args = parser.parse_args()

    os.makedirs(ASSETS, exist_ok=True)
    badge(os.path.join(ASSETS, "badge-licence.svg"), "licence", "MIT", "green")
    badge(os.path.join(ASSETS, "badge-python.svg"), "python", "3.10+", "blue")
    badge(os.path.join(ASSETS, "badge-offline.svg"), "offline", "no network", "green")
    badge(os.path.join(ASSETS, "badge-version.svg"), "version", project_version(), "grey")
    if args.tests is not None:
        badge(os.path.join(ASSETS, "badge-tests.svg"), "tests",
              f"{args.tests} passed", "green" if args.tests else "amber")
    print("wrote badges to docs/assets")


if __name__ == "__main__":
    main()
