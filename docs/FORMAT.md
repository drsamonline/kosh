# The .kosh container — format version 2

This is the normative description of the bytes `kosh pack` writes.
`kosh/core/pack.py` is the source of truth in code; if the two disagree, the code
wins and this file gets fixed.

`.kosh` is a **container**, not a CHCE bitstream. It has its own magic on purpose:
a decoder that sees `CHCE` must be able to trust that the rest of the stream is a
CHCE stream.

## Conventions

- Integers are unsigned little-endian: `u8`, `u32` as written by `struct <I`.
- Sizes are byte counts.
- A stream that does not match this document exactly is rejected. There are no
  optional trailing bytes and no padding.

## Layout

```
+--------------------------------------------------+
| magic        "KOSH\x01\x00"                6 B   |
| block 0 .. n        <u32 comp_len><u32 raw_len><payload>   |
| table of contents   UTF-8 JSON                   |
| toc_len             u32                          |
| footer              "KOSHA1END"            9 B   |
+--------------------------------------------------+
```

Reading a container starts at the end: `seek(size - 13)`, read `u32 toc_len`,
read the 9-byte footer, then read `toc_len` bytes of JSON immediately before it.
Listing a container therefore costs one seek and one small read, not a scan.

## Blocks

A block is the unit of compression. Each record is:

| field | type | meaning |
| --- | --- | --- |
| `comp_len` | u32 | bytes of payload that follow |
| `raw_len` | u32 | bytes after decompression |
| payload | bytes | the codec's output |

Blocks are independent, which is what allows packing and unpacking to run on a
thread pool. A block never spans two entries.

## Table of contents

```json
{
  "format": "kosh-container",
  "version": 2,
  "codec": "zstd",
  "level": 10,
  "block_size": 8388608,
  "created": "2026-11-11T12:00:00Z",
  "entries": [
    {"path": "src/main.py", "type": "file", "size": 4210, "mode": 420,
     "mtime": 1762862400.0, "sha256": "…",
     "blocks": [[6, 1200, 4210]]},
    {"path": "src", "type": "dir", "mode": 493, "mtime": 1762862400.0}
  ],
  "manifest_sha256": "…"
}
```

`blocks` holds `[offset, comp_len, raw_len]` triples, where `offset` is the
absolute file offset of the block's `comp_len` field. A consumer can therefore
seek straight to a block without walking the stream.

`type` is `file`, `dir` or `symlink` (which carries `target` instead of `blocks`).

### manifest_sha256

SHA-256 over the canonical JSON of the table of contents **excluding** this key:
keys sorted, separators `(",", ":")`, UTF-8. It protects the metadata — names,
sizes, offsets — which is what truncation and editing damage first. Per-entry
`sha256` protects the payload.

Version 1 containers have no `manifest_sha256`; they are read, and `kosh verify`
reports `not present (v1)` rather than implying a check that did not happen.

## Codecs

| name | implementation | level range | default |
| --- | --- | --- | --- |
| `zstd` | Zstandard frame per block | 1–19 | 10 |
| `brotli` | Brotli, `lgwin=24` | 1–11 | 9 |
| `lzma` | LZMA2 preset | 0–9 | 6 |
| `store` | identity | — | — |

`store` exists so the container can be used as an archive without a second
compression pass; its output is the input plus framing.

## Verification rules

A reader MUST:

1. Check the magic at offset 0 and the footer at `size - 9`.
2. Reject `toc_len` larger than the file.
3. Reject an unknown `format` or a `version` greater than it understands.
4. Recompute `manifest_sha256` when `version >= 2` and reject a mismatch.
5. Reject a block whose `comp_len`/`raw_len` disagrees with the table.
6. Reject a block that ends past the end of the file.
7. Check each entry's `sha256` after decoding, unless verification was waived.

`kosh verify` implements exactly these, plus the per-entry hashes.

## Not in the format, deliberately

- **No encryption.** A password field would invite a home-grown cipher.
- **No compression dictionary.** It would make the stream unreadable without an
  external file, and the container is meant to be self-contained.
- **No incremental update.** Rewriting a container is a full rewrite; that keeps
  the block table honest.
