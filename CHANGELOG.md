# Changelog

## Unreleased

**Added**

- Property-based round-trip tests with Hypothesis (roadmap CT-3) for split/join
  by part count and by part size, pack/unpack, and the full PNG filter matrix —
  five filters plus the adaptive choice, `bpp` 1–4. The push suite generates
  1 000 cases; `KOSH_PROPERTY_CASES=full` generates the roadmap's 10 000, which
  `fuzz.yml` runs nightly (`docs/FINDINGS.md` §11).
- `hypothesis>=6` in the `dev` extras, so `pip install -e ".[dev]"` is still the
  only setup step.

## 1.1.0 — 11 November 2026

First release as part of the CHCE family.

**Added**

- `kosh verify` — integrity check for containers, split sets, ZIP and tar,
  without extracting. `--quick` checks structure only.
- `kosh info` — container header, manifest digest and the physical block table.
- `kosh doctor` — installation self-check with a live pack/unpack round-trip and
  a Zopfli probe; exits non-zero on a fatal problem.
- `kosh bench` — reproducible codec comparison against `gzip -9`, `xz -9` and
  `zip -9`, with `--markdown` for pasting into `docs/FINDINGS.md`.
- `kosh pack --engine ziptastic|heavenzip` — drive a CHCE engine across a process
  boundary; nothing is linked.
- CHCE stream detection in `kosh scan` (header only; decoding is the engine's job).
- Container format v2: `manifest_sha256` over the table of contents.
- Web UI: a Verify operation.

**Changed**

- README rewritten around the CHCE family, with measured tables and an explicit
  "what it is not".
- `docs/FINDINGS.md` added: every number, the Zopfli budget, the rejected ideas.
- `docs/FORMAT.md` added: the container, byte by byte.

**Fixed**

- PNG adaptive filtering used a zero "up" row when encoding each row, which
  corrupted images whose chosen filter was Average or Paeth. Caught by the filter
  round-trip test, not by the screenshot.

## 1.0.0 — 11 November 2026

First working release, built as a replacement for five Windows utilities
(DeflOpt, ECT, WABBIE, SplitSat, WinRK).

- `split` / `join` / `parts` — parts with a manifest and SHA-256 per part.
- `pack` / `unpack` — `.kosh` container, four codecs, per-entry hashes.
- `archive create|list|extract` — zip, tar, tar.gz, tar.zst, tar.xz.
- `optimize` — PNG, GZIP, ZIP, JPEG lossless re-compression.
- `scan`, `ui`, `version`.
- 31 tests.
