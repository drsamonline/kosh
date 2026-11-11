# Kosh — project overview

## 1. What it is

The file workbench of the CHCE family. One engine, two front ends (a CLI and a
local web UI), for the jobs that happen around compression rather than inside it:
re-compressing what is already compressed, splitting and rejoining, archiving,
verifying, and identifying unknown files.

## 2. Why it exists

The family already had an engine (`ziptastic`), a source vault (`HCS`), an
orchestrator (`heavenzip`) and a coordinator (`compfactory`). What it did not
have was the workbench: a place to take a folder of PNGs, a pile of ZIPs, a
400 GB disk image or an archive of unknown provenance and do something
verifiable with it.

Before Kosh that job was done by five unrelated Windows programs — DeflOpt, ECT,
WABBIE, SplitSat and WinRK — none of which verify anything, and two of which
rewrite files in place with no undo. `docs/ORIGINALS.md` records what each of
them does and what replaced it.

## 3. Boundaries

Kosh must not become the engine. Concretely:

- **No new entropy coder.** Kosh picks between existing ones (Zstandard, Brotli,
  LZMA, raw deflate via Zopfli). If a stronger codec is wanted, that is
  `ziptastic`'s job.
- **No CHCE bitstream implementation.** Kosh recognises the `CHCE` magic and
  reports the header. It does not decode `.dr`.
- **Process boundaries for other engines.** `kosh pack --engine ziptastic`
  spawns the binary. Nothing is linked, so nothing is entangled.
- **Offline at runtime.** No network calls, no downloads, no models. The web UI
  listens on loopback and serves only what is on disk.

## 4. What lives where

| path | purpose |
| --- | --- |
| `kosh/core/util.py` | sizes, hashing, atomic writes, progress, path safety |
| `kosh/core/splitjoin.py` | split / join / inspect with a JSON manifest |
| `kosh/core/pack.py` | the `.kosh` container: codecs, TOC, manifest digest, verify |
| `kosh/core/archive.py` | zip, tar, tar.gz, tar.zst, tar.xz, `.kosh` |
| `kosh/core/optimize.py` | PNG, GZIP, ZIP, JPEG lossless re-compression |
| `kosh/core/verify.py` | integrity checks that never write |
| `kosh/core/doctor.py` | environment self-check with a live round-trip |
| `kosh/core/bench.py` | reproducible codec comparison |
| `kosh/core/engine.py` | CHCE stream detection, external engine delegation |
| `kosh/cli.py` | Typer + Rich front end |
| `kosh/ui/` | FastAPI server and a dependency-free single-page front end |

## 5. Hard rules

- **Verify, don't assume.** Every artefact carries a hash; every consumer checks
  it and fails loudly. `kosh verify` exists so this is one command.
- **Write atomically.** Temp file, fsync, `os.replace`. A crash never leaves a
  half-written output and never damages the original.
- **Never trust a path from a file.** Archive members are checked for traversal
  before anything is created.
- **Report the truth.** A file is only rewritten when it gets smaller, and the
  report names the engine that ran (`· zopfli`, `· zlib-9`).
- **Keep the README's numbers reproducible.** Every table in the README comes
  from `kosh bench` or from a test in `tests/`.

## 6. Roadmap

`docs/ROADMAP.md` holds every planned enhancement, grouped by milestone, each with
its exit test, its size and the decision it depends on. The three next items are
Windows CI, a real ziptastic round-trip, and the `check-claims.py` gate that keeps
this README's tables honest.

## 7. State

| area | state |
| --- | --- |
| Engine, CLI, web UI | working; 43 tests pass |
| Container v2 (manifest digest) | shipped; v1 containers still read |
| `verify`, `info`, `doctor`, `bench` | shipped |
| CHCE stream detection | shipped (header only, by design) |
| External engine delegation | shipped; exercised by a stand-in binary in the tests, not against a built `ziptastic` |
| Windows | supported in code (paths, modes, symlinks degrade); not exercised on a Windows runner |
| macOS | expected to work; not exercised |
