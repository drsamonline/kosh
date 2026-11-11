# Architecture

Kosh is one engine with two front ends. The CLI (`kosh/cli.py`) and the local web
server (`kosh/ui/server.py`) both call the same functions in `kosh/core/`; neither
contains logic the other lacks, so a bug fixed once is fixed everywhere.

```
kosh/
├── cli.py              Typer + Rich front end
├── core/
│   ├── util.py         sizes, hashing, atomic writes, progress, path safety
│   ├── splitjoin.py    split / join / inspect with a JSON manifest
│   ├── pack.py         .kosh container format (codecs, TOC, verification)
│   ├── archive.py      zip / tar / tar.gz / tar.zst / tar.xz / .kosh
│   ├── optimize.py     PNG, GZIP, ZIP and JPEG lossless re-compression
│   ├── verify.py       integrity checks that never write
│   ├── doctor.py       environment self-check with a live round-trip
│   ├── bench.py        reproducible codec comparison
│   └── engine.py       CHCE stream detection, external engine delegation
└── ui/
    ├── server.py       FastAPI: upload → job → poll → download
    └── static/         index.html, app.js, style.css (no build step)
```

## Principles

1. **Stream, never slurp.** Every read and write goes through 8 MiB blocks. A
   40 GB split uses the same memory as a 40 MB one.
2. **Verify, don't assume.** Every produced artefact carries a SHA-256; every
   consumer checks it and fails loudly.
3. **Write atomically.** Output is written to a sibling temp file, fsynced, then
   `os.replace`d. A crash leaves the original untouched and no partial output.
4. **Never trust a path from a file.** Archive members go through
   `safe_relpath` and `is_within`; absolute paths, `..` and drive letters are
   rejected before anything is created.
5. **Report the truth.** Savings are measured from real byte counts, and a file
   is only rewritten when it actually gets smaller (unless `--force`).

## `.kosh` container format (v1)

```
"KOSH\x01\x00"                       6-byte start magic
block record 0..n                    <u32 compressed_len><u32 raw_len><payload>
table of contents                    UTF-8 JSON
<u32 toc_len>                        little-endian
"KOSHA1END"                          9-byte footer
```

The footer is read backwards, so listing a container costs one `seek`, not a
scan. The TOC holds the codec, level, block size, and per entry: relative path,
type (`file`/`dir`/`symlink`), size, mode, mtime, SHA-256 and the block table
`[offset, compressed_len, raw_len]`.

Blocks are independent, which is what makes packing and unpacking parallel.
Compression runs through a bounded window of `2 × threads` blocks so memory
stays flat even when the thread pool is large.

## Split manifest

`<name>.koshsplit.json` sits next to the parts:

```json
{"format": "kosh-split", "version": 1, "algo": "sha256",
 "source": {"name": "disk.img", "size": 4194304, "sha256": "…"},
 "part_size": 1048576,
 "parts": [{"name": "disk.part001", "size": 1048576, "sha256": "…"}]}
```

Joining accepts any part, the manifest, or the directory, finds the manifest,
checks each part's hash as it streams, then checks the whole-file hash.

## PNG pipeline

1. Parse chunks and validate every CRC.
2. Refuse animated (APNG) and interlaced files rather than risk a silent change.
3. Inflate the concatenated `IDAT` stream.
4. Undo the per-row filters into a `(height, stride)` uint8 matrix (NumPy;
   Sub decodes as a cumulative sum per byte lane).
5. Encode candidate filters: forced 0–4 and an adaptive per-row choice by
   minimum sum of absolute signed values.
6. Rank candidates with `zlib -6` — cheap — then spend Zopfli on the winner
   (level 3 tries the top two). Above 1 MB of payload the encoder switches to
   `zlib -9`, because Zopfli's cost per byte is roughly 50× higher there; the
   report names the engine that ran.
7. Rebuild: same chunk order, one `IDAT`, metadata dropped only when asked.

Correctness is pinned by a round-trip test over every filter strategy and
`bpp` in {1, 3, 4}.

## ZIP rewriting

Entries are read through `zipfile`, re-deflated with Zopfli, and written back
with a hand-built local header + central directory. Original method, timestamps,
attributes, extra fields and comments are preserved; encrypted archives, ZIP64
and non-deflate methods are passed through untouched rather than guessed at.
A file is only replaced when the result is smaller.

## Web server model

Uploads land in a per-session directory under `~/.kosh/sessions/<token>/`.
`POST /api/run` starts a worker thread and returns a job id; the page polls
`/api/job/{id}` for status and progress. Results are exposed as `/api/download`
links, and a directory result is zipped on demand. Sessions expire after six
hours. The server binds to `127.0.0.1` by default and makes no outbound calls.

## Family boundaries

Kosh is the workbench of the CHCE family and must not become the engine. It does
not implement a CHCE bitstream; it recognises the `CHCE` magic and reports the
header, then defers. Where an engine is installed (`ziptastic`, `heavenzip`),
`kosh pack --engine …` spawns it — a process boundary, never a link, which is the
same rule that keeps heavenzip GPL-safe.

Container format v2 adds `manifest_sha256`: a digest over the canonical JSON of
the table of contents, so damaged metadata is caught before it is trusted. v1
containers still read, and `verify` says "not present (v1)" rather than implying a
check that did not run.

## Testing

`tests/test_core.py` covers round-trips (split, pack, every archive format),
corruption detection, traversal rejection, filter correctness and each optimiser
against its content checksum. `tests/test_api.py` drives the HTTP surface with a
TestClient. `tests/test_family.py` covers the manifest digest, tampering
detection, `verify` across formats, `doctor`, benchmark determinism, CHCE header
parsing and engine delegation (against a stand-in binary, so it runs without
ziptastic installed). 43 tests, all passing.
