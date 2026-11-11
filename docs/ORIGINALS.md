# What the original six programs do — and what Kosh does instead

Notes from taking the supplied binaries apart, plus the decision each one drove.
This is analysis, not a port: no code was copied, and no proprietary format was
reimplemented.

## 1. DeflOpt.exe — "Optimises deflated files" 2.07

* Vendor string: WinnieWorks, © 2003–2007 Ben Jos Walbeehm. PE32, MSVCRT, 52 KB,
  four sections, linker 6.0.
* Import surface is small and revealing: `CreateFileA`, `SetFilePointer`,
  `GetFileTime`/`SetFileTime`, `MoveFileA`, `FindFirstFileA` — it rewrites files
  in place and preserves timestamps. `BigFile::*` error strings show a custom
  buffered IO layer built on Win32 handles.
* Strings expose its whole scope: PNG IHDR fields ("Width/Height/Bit depth/
  colour type"), "PNG compression method must be 0", GZIP/ZIP handling, and the
  line `Number of bits that can be saved: %s (%0.3f bytes)` — it measures its
  work in **bits**, not bytes, and rewrites only when a whole byte is saved
  unless `/b` is given.
* Option set recovered from the embedded help: `/a` scan all files regardless of
  extension, `/b` rewrite on bit savings, `/c` keep GZIP/ZIP comments, `/d` keep
  timestamps, `/f` force rewrite, `/k` keep all chunks, `/r` recurse, `/s` silent,
  `/v` verbose. Slashes and dashes both accepted; wildcards allowed; explicit
  note that it will not touch archives nested inside archives.

**Kosh equivalent:** `kosh optimize`. Same guarantee (byte-identical content,
smaller deflate stream) but the container is rebuilt from parsed structures
rather than patched, so CRC/Adler fields, extra fields and local headers stay
consistent. `--dry-run` replaces guesswork; `--strip` is the `/k` inverse;
`--backup` replaces the implicit in-place rewrite.

## 2. ECT.exe — "Efficient Compression Tool" 0.1

* PE32, 1.8 MB, 14 sections including `/4`, `/14`, `/29`, `/41`… (Borland/GCC
  section naming). Statically linked: zopfli, libpng 1.6.18, lodepng, libjpeg,
  optipng's `opngreduc`, a C++ stdlib with `std::regex`/`std::future`.
* Recovered usage: `ECT [Options] File`, with `-strip`, `-gzip`, `-quiet`,
  `-M1`–`-M5` (compression level, default 1), and advanced `--strict`
  (strict losslessness), `--mt-deflate[=i]` (per-block multithreading).
* Its own error strings describe the pipeline: "ECT (JPEG)", "opngreduc",
  "zopflipng", "zopfli_gzip", "Can't finalize IDAT", "Result is bigger",
  "Compression of empty files is currently not supported", `.ungz` outputs.
* So: it is a zopfli-backed PNG/GZIP/JPEG optimiser with lossless PNG reduction,
  per-block threading and a strict mode.

**Kosh equivalent:** `kosh optimize --level 1|2|3`. Same algorithms, rebuilt:
Zopfli for the entropy layer, our own PNG filter analysis (adaptive or forced,
ranked with fast zlib before spending Zopfli), real metadata stripping, and
`--jobs` to parallelise across files. Empty files and already-optimal files are
handled instead of refused.

## 3. WABBIE_1.0.exe — "Wabbie Compressor"

* PE32 console, MSVC 14, linked against **VCRUNTIME140D/ucrtbased** (a debug
  build shipped by accident). Strings: "Copyright Alberto Fulcini (c) 2016",
  `Usage: Wabbie e/d inputfile outputfile`.
* Symbol strings name the algorithm stack: `bscBlockHeader`, `lz4Stream_body`,
  `dec64table`, `MTFTable`, `rankHistory`, `runHistory`, `num_indexes` — that is
  **BSC** (a Burrows–Wheeler + MTF + entropy coder) with **LZ4** framing.
* Also: "General GPU failure", "Your GPU is not supported", "Not enough GPU
  memory" — the BSC stage runs on CUDA. One file in, one file out, no container,
  no checksums, no file names.

**Kosh equivalent:** `kosh pack` / `kosh unpack`. Keeps the good part — blocks
that compress independently, so it parallelises — and drops the GPU dependency:
Zstandard (with internal threading), Brotli or LZMA per block, a table of
contents, per-entry SHA-256, selective extraction, symlinks and safe paths.

## 4. SplitSat.exe — "Ultra Fast File Splitter" 5.0

* PE32 .NET (CLR v2.0.50727), © Luis León 2013, 187 KB, resources dominated by a
  large `.rsrc` (console UI art). Type names in the metadata are obfuscated
  (private-use Unicode identifiers), so the IL is not readable without an
  unpacking step; the string literals are likewise hidden.
* Behaviour is legible from the imports alone: `FileStream`, `BinaryWriter`,
  `FileInfo`, `Directory`, `ConsoleKeyInfo`, `ConsoleColor` — a console
  splitter/joiner that reads a key from the user between operations.

**Kosh equivalent:** `kosh split` / `kosh join` / `kosh parts`. Splitting by size
or by part count, but each part is hashed into a JSON manifest, so joining
verifies every part and the reassembled file. Works on any OS, streams with
constant memory, and refuses to silently produce a corrupt output.

## 5. WinRK312x64.exe — WinRK 3.12 (x64) installer

* PE32 **GUI** subsystem, Nullsoft (NSIS) self-extracting installer, 3.4 MB,
  imports USER32/GDI32/COMCTL32/OLE32 — a Windows-only GUI archiver whose payload
  is a proprietary RK-format engine.
* The payload is NSIS-compressed and the archive format is undocumented and
  closed, so there is nothing here that can be honestly reimplemented.

**Kosh equivalent:** `kosh archive` plus the `.kosh` container. The workflow is
reproduced (one window, pick files, one output file, get them back) but the
formats are open: ZIP, tar, tar.gz, tar.zst, tar.xz and `.kosh`, each with a CLI
verb and a browser UI.

## 6. decomp.exe — 4,473 bytes

* Not a usable PE: `e_lfanew` points at offset 4, the section table and data
  directories are absent (`pefile` and `dnfile` both reject it), and the bytes
  after the DOS stub are high-entropy. It looks like a protected or deliberately
  scrambled stub rather than a program that can be read.
* Nothing was extracted from it, so nothing was rebuilt from it. If you have the
  original source or a working copy, it can be analysed properly.

## What "better" means here

| Concern | Originals | Kosh |
| --- | --- | --- |
| Platform | Windows x86 only | Linux, macOS, Windows (pure Python + wheels) |
| Interface | Console flags or a GUI | One CLI **and** a local web UI over the same engine |
| Integrity | None / implicit | SHA-256 per part, per file, per container entry |
| Failure handling | In-place rewrite, no undo | Atomic temp-and-rename, `--dry-run`, `--backup` |
| Progress | Sparse or none | Live progress with throughput, JSON output for scripting |
| Scope | One job per program | Split, join, pack, unpack, archive, optimise, inspect |
