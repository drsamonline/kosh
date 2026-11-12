#!/usr/bin/env python3
"""check-claims.py — every number the README quotes must exist in FINDINGS.md.

CT-11 in docs/ROADMAP.md. ziptastic has run this gate since its 1.4.x line; the
rule it enforces is the one this project already believes: `docs/FINDINGS.md` is
the authority for any number quoted anywhere else, and a hand-copied figure is how
a published claim drifts from the measurement behind it.

What counts as a claim: an integer of three digits or more (with or without
thousands separators) or a percentage with one decimal, anywhere in the README
outside a fenced code block. What does not: IP addresses and ports, because
`127.0.0.1:8765` is configuration, not a measurement.

    python tools/check-claims.py      # exit 0 = every claim is in FINDINGS
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
FINDINGS = ROOT / "docs" / "FINDINGS.md"

CLAIM = re.compile(r"\b\d{1,3}(?:,\d{3})+\b|\b\d+\.\d%\b|\b\d{3,}\b")
FENCED = re.compile(r"```.*?```", re.S)
ADDRESS = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")


def main() -> int:
    readme = README.read_text(encoding="utf-8")
    findings = FINDINGS.read_text(encoding="utf-8").replace(",", "")

    prose = ADDRESS.sub(" ", FENCED.sub(" ", readme))
    claims = CLAIM.findall(prose)
    if not claims:
        sys.exit("no claims found in README.md — has the file been emptied?")

    missing = []
    for claim in dict.fromkeys(claims):
        if claim.replace(",", "") not in findings:
            # Name the line, so the fix is obvious without a search.
            for number, line in enumerate(readme.splitlines(), 1):
                if claim in line:
                    missing.append((claim, number, line.strip()[:90]))
                    break

    print(f"{len(set(claims))} distinct claims in README.md, {len(missing)} not in docs/FINDINGS.md")
    for claim, line_no, text in missing:
        print(f"  {claim} (README.md:{line_no})  {text}")
    if missing:
        print("\nEvery quoted number must be in docs/FINDINGS.md. Either add the measurement")
        print("there, or stop quoting it — a claim without a measurement behind it is the")
        print("thing this gate exists to prevent.")
        return 1
    print("every claim is accounted for")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
