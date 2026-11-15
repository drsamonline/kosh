# Changelog

## Unreleased

**Added**

- `kosh verify` reports `zip64` (in the table and in `--json`) when the archive
  carried ZIP64 records, so an entry count above 65 535 is visibly the real one
  and not the saturated `0xFFFF` of the classic end record. Tests build a
  65 536-entry archive in the test itself — no binary in the repository.
- `kosh run recipe.yaml` (EN-5) — chain `optimize → pack → engine → verify` from a
  recipe file, locally, with no engine install and no shell script. Every step's
  operation, arguments, exit status and evidence (output paths, sizes, SHA-256) go
  into `<recipe>.manifest.json`. A malformed recipe, an unknown operation or an
  argument the operation does not take is refused before the first step runs; the
  first failing step stops the run with a non-zero exit and the manifest names it.
  The recipe format is a deliberately narrow, documented subset of YAML — no new
  dependency. See `docs/RECIPES.md` and `examples/recipes/`.

**Fixed**

- `kosh verify --quick` on a ZIP decompressed every entry, against its own
  documentation ("structure only; do not decompress"). It now reads the central
  directory and says `central directory only`; on a 65 536-entry archive that is
  0.8 s instead of 11.7 s (`docs/FINDINGS.md` §10).

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
