# Changelog

## Unreleased

**Fixed**

- The suite is green on all three runners again. `test_sniff.py` asserted that the
  running interpreter is `elf` — true on Linux, false on Windows (`pe`) and on macOS
  (kosh has no Mach-O entry, so `unknown`), so the matrix had been red since
  2026-11-18 over a test about the machine rather than about the code. The
  expectation is now the platform's, and macOS skips with that reason stated. The
  test-count badge was stale at the same time (114 against 132 collected, left behind
  by the PR that added the signature table), so the badge step would have failed next;
  it is regenerated.

**Added**

- Property-based round-trip tests with Hypothesis (roadmap CT-3) for split/join
  by part count and by part size, pack/unpack, and the full PNG filter matrix —
  five filters plus the adaptive choice, `bpp` 1–4. The push suite generates
  1 000 cases; `KOSH_PROPERTY_CASES=full` generates the roadmap's 10 000, which
  `fuzz.yml` runs nightly (`docs/FINDINGS.md` §11).
- `hypothesis>=6` in the `dev` extras, so `pip install -e ".[dev]"` is still the
  only setup step.
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

- `kosh verify --quick` on a **tar** decompressed every member, the same defect
  CT-6 fixed for zip: the flag is documented as "structure only; do not
  decompress". Tar now reads the headers and says `headers only` under `--quick`,
  and `every entry read (no per-member checksum in tar)` under deep verify. That
  wording is deliberate — tar carries no per-member checksum, so reading a member
  proves the bytes are there, not that they are the bytes that were written. Zip's
  deep path can compare a CRC; tar's cannot.

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
