"""Kosh container: one file in, many entries out.

WABBIE compressed a single file with a GPU-bound BSC/LZ4 pipeline and nothing
else.  This format keeps the good idea (independent blocks, so compression
parallelises) and adds what a modern tool needs: a table of contents, per-entry
hashing, codec choice, safe extraction and selective restore.

Layout::

    "KOSH\\x01\\x00"                 start magic
    block records                     <u32 compressed_len><u32 raw_len><payload>
    table of contents (UTF-8 JSON)
    <u32 toc_len> "KOSHA1END"         footer, read backwards
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import shutil
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable, Optional

import brotli
import lzma
import zstandard

from .util import (
    BLOCK, KoshError, ProgressFn, atomic_write, cpu_count, copy_meta,
    human_size, iter_file, safe_relpath, is_within,
)

START_MAGIC = b"KOSH\x01\x00"
END_MAGIC = b"KOSHA1END"
FORMAT = "kosh-container"
VERSION = 2  # v2 adds manifest_sha256; v1 containers still read fine
BLOCK_HDR = struct.Struct("<II")

CODECS = ("zstd", "brotli", "lzma", "store")

_LEVEL_HINTS = {
    "zstd": (1, 19, 10),
    "brotli": (1, 11, 9),
    "lzma": (0, 9, 6),
    "store": (0, 0, 0),
}


@dataclass
class PackResult:
    output: str
    entries: int
    raw_size: int
    stored_size: int
    codec: str
    seconds: float

    @property
    def ratio(self) -> float:
        return self.stored_size / self.raw_size if self.raw_size else 0.0

    @property
    def saved(self) -> int:
        return self.raw_size - self.stored_size


@dataclass
class UnpackResult:
    output_dir: str
    entries: int
    bytes: int
    verified: bool
    skipped: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- codecs
def _level_for(codec: str, level: Optional[int]) -> int:
    low, high, default = _LEVEL_HINTS[codec]
    if level is None:
        return default
    if not low <= level <= high:
        raise KoshError(f"{codec} level must be {low}-{high} (got {level})")
    return level


def _compress(buf: bytes, codec: str, level: int) -> bytes:
    if codec == "zstd":
        return zstandard.ZstdCompressor(level=level).compress(buf)
    if codec == "brotli":
        return brotli.compress(buf, quality=level, lgwin=24)
    if codec == "lzma":
        return lzma.compress(buf, preset=level)
    return buf


def _decompress(buf: bytes, raw_len: int, codec: str) -> bytes:
    if codec == "zstd":
        return zstandard.ZstdDecompressor().decompress(buf, max_output_size=raw_len)
    if codec == "brotli":
        return brotli.decompress(buf)
    if codec == "lzma":
        return lzma.decompress(buf)
    return buf


# --------------------------------------------------------------------------- walking
def _iter_inputs(inputs: Iterable[str]):
    """Yield (abs_path, arcname, kind) for files, dirs and symlinks."""
    for raw in inputs:
        path = os.path.abspath(raw)
        if not os.path.exists(path) and not os.path.islink(path):
            raise KoshError(f"no such file or directory: {raw}")
        if os.path.islink(path):
            yield path, os.path.basename(path), "symlink"
            continue
        if os.path.isfile(path):
            yield path, os.path.basename(path), "file"
            continue
        base = os.path.dirname(path)
        for root, dirs, files in os.walk(path):
            dirs.sort()
            files.sort()
            rel_root = os.path.relpath(root, base).replace(os.sep, "/")
            yield root, rel_root, "dir"
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, base).replace(os.sep, "/")
                yield full, rel, "symlink" if os.path.islink(full) else "file"


# --------------------------------------------------------------------------- pack
def pack(inputs: list[str], out: str, codec: str = "zstd", level: Optional[int] = None,
         block_size: int = BLOCK, threads: Optional[int] = None,
         progress: Optional[ProgressFn] = None) -> PackResult:
    if codec not in CODECS:
        raise KoshError(f"unknown codec {codec!r} (choose {', '.join(CODECS)})")
    lvl = _level_for(codec, level)
    if block_size < 64 * 1024:
        raise KoshError("block size must be at least 64 KiB")
    threads = max(1, threads or min(8, cpu_count()))
    started = time.perf_counter()

    entries: list[dict] = []
    total_raw = 0
    workers = 1 if codec == "store" else threads
    window = max(1, workers * 2)
    offset = len(START_MAGIC)

    with atomic_write(out) as fh:
        fh.write(START_MAGIC)
        executor = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
        pending: list[tuple[int, object]] = []

        def write_block(comp: bytes, raw_len: int) -> list[int]:
            nonlocal offset
            fh.write(BLOCK_HDR.pack(len(comp), raw_len))
            fh.write(comp)
            rec = [offset, len(comp), raw_len]
            offset += BLOCK_HDR.size + len(comp)
            return rec

        def flush_one() -> None:
            raw_len, fut = pending.pop(0)
            comp = fut.result() if executor else fut
            blocks_ref.append(write_block(comp, raw_len))

        blocks_ref: list[list[int]] = []

        for path, arcname, kind in _iter_inputs(inputs):
            arcname = arcname.replace(os.sep, "/")
            st = os.lstat(path)
            if kind == "dir":
                entries.append({
                    "path": arcname, "type": "dir",
                    "mode": stat.S_IMODE(st.st_mode), "mtime": st.st_mtime,
                })
                continue
            if kind == "symlink":
                entries.append({
                    "path": arcname, "type": "symlink", "target": os.readlink(path),
                    "mtime": st.st_mtime,
                })
                continue

            blocks_ref = []
            digest = hashlib.sha256()
            size = 0
            for buf in iter_file(path, block_size):
                digest.update(buf)
                size += len(buf)
                pending.append(
                    (len(buf), executor.submit(_compress, buf, codec, lvl) if executor
                     else _compress(buf, codec, lvl))
                )
                if len(pending) >= window:
                    flush_one()
                if progress:
                    progress(total_raw + size, total_raw + os.path.getsize(path), "packing")
            while pending:
                flush_one()

            total_raw += size
            entries.append({
                "path": arcname, "type": "file", "size": size,
                "mode": stat.S_IMODE(st.st_mode), "mtime": st.st_mtime,
                "sha256": digest.hexdigest(), "blocks": blocks_ref,
            })

        if executor:
            executor.shutdown(wait=True)

        toc = {
            "format": FORMAT, "version": VERSION, "codec": codec, "level": lvl,
            "block_size": block_size, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "entries": entries,
        }
        toc["manifest_sha256"] = manifest_digest(toc)
        blob = json.dumps(toc, separators=(",", ":")).encode("utf-8")
        fh.write(blob)
        fh.write(struct.pack("<I", len(blob)))
        fh.write(END_MAGIC)

    stored = os.path.getsize(out)
    seconds = time.perf_counter() - started
    if progress:
        progress(total_raw, total_raw, "done")
    return PackResult(
        output=os.path.abspath(out), entries=sum(1 for e in entries if e["type"] == "file"),
        raw_size=total_raw, stored_size=stored, codec=codec, seconds=seconds,
    )


# --------------------------------------------------------------------------- read
def manifest_digest(toc: dict) -> str:
    """SHA-256 over the table of contents, excluding the digest itself.

    The per-file hashes protect the payload; this protects the metadata — entry
    names, sizes and block offsets — which is what a truncation or an editor
    damages first.
    """
    body = {k: v for k, v in toc.items() if k != "manifest_sha256"}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def read_toc(path: str, verify_manifest: bool = True) -> dict:
    size = os.path.getsize(path)
    if size < len(START_MAGIC) + 13:
        raise KoshError(f"{os.path.basename(path)} is too small to be a Kosh container")
    with open(path, "rb") as fh:
        head = fh.read(len(START_MAGIC))
        if head != START_MAGIC:
            raise KoshError(f"{os.path.basename(path)} is not a Kosh container")
        fh.seek(size - len(END_MAGIC) - 4)
        (toc_len,) = struct.unpack("<I", fh.read(4))
        if fh.read(len(END_MAGIC)) != END_MAGIC:
            raise KoshError("container footer is damaged (truncated download?)")
        # The table cannot be larger than the file minus its own header and footer,
        # and a length that says otherwise would seek before the start of the file —
        # which raises OSError rather than KoshError, so `verify` crashed instead of
        # refusing. One flipped bit in this field is enough; found by the exhaustive
        # sweep in tests/test_fuzz.py.
        body_start = len(START_MAGIC)
        footer = len(END_MAGIC) + 4
        if toc_len > size - body_start - footer:
            raise KoshError("container table of contents is corrupt")
        fh.seek(size - footer - toc_len)
        blob = fh.read(toc_len)
    try:
        toc = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KoshError("container table of contents is corrupt") from exc
    if toc.get("format") != FORMAT:
        raise KoshError("not a Kosh container")

    # `version` must be present, not defaulted. A one-byte edit that renames the key
    # — `"version"` to `"wersion"` — used to fall back to version 1, and the digest
    # check below only runs for version 2 and above, so the mutation switched the
    # integrity check off and the container reported intact. Found by the corruption
    # fuzzer (CT-4).
    if "version" not in toc:
        raise KoshError("container table of contents has no version")
    version = toc["version"]
    if not isinstance(version, int) or version < 1:
        raise KoshError("container version is not a positive integer")
    if version > VERSION:
        raise KoshError("container was written by a newer Kosh; please upgrade")
    if verify_manifest and version >= 2:
        if "manifest_sha256" not in toc:
            raise KoshError("a version-2 container must carry a manifest digest")
        if toc["manifest_sha256"] != manifest_digest(toc):
            raise KoshError("container manifest digest mismatch: metadata is corrupt")
    return toc


def list_container(path: str) -> dict:
    toc = read_toc(path)
    total = sum(e.get("size", 0) for e in toc["entries"] if e["type"] == "file")
    return {
        "path": os.path.abspath(path),
        "codec": toc.get("codec"),
        "level": toc.get("level"),
        "created": toc.get("created"),
        "stored_size": os.path.getsize(path),
        "raw_size": total,
        "entries": toc["entries"],
    }


def unpack(path: str, dest: str = ".", members: Optional[list[str]] = None,
           verify: bool = True, threads: Optional[int] = None,
           progress: Optional[ProgressFn] = None,
           overwrite: bool = True, max_bytes: Optional[int] = None,
           check_space: bool = True) -> UnpackResult:
    toc = read_toc(path)
    codec = toc.get("codec", "zstd")
    threads = max(1, threads or min(8, cpu_count()))
    workers = 1 if codec == "store" else threads
    dest = os.path.abspath(dest)
    os.makedirs(dest, exist_ok=True)

    wanted = None
    if members:
        wanted = {safe_relpath(m) for m in members}
        known = {e["path"] for e in toc["entries"]}
        missing = wanted - known
        if missing:
            raise KoshError(f"not in container: {', '.join(sorted(missing))}")

    total = sum(e.get("size", 0) for e in toc["entries"]
                if e["type"] == "file" and (wanted is None or e["path"] in wanted))

    # A zip bomb is a legitimate archive. Refuse before writing anything rather
    # than filling the disk and reporting the failure as an OSError from the last
    # write — the size is in the table of contents, so the answer is free.
    if max_bytes is not None and total > max_bytes:
        raise KoshError(
            f"this container expands to {human_size(total)}, over the {human_size(max_bytes)} "
            "budget; pass --max-size to allow it"
        )
    if check_space:
        try:
            free = shutil.disk_usage(dest).free
        except OSError:  # pragma: no cover - dest is not stat-able yet
            free = None
        if free is not None and total > free:
            raise KoshError(
                f"extracting needs {human_size(total)} but {human_size(free)} is free in {dest}"
            )
    done = 0
    written_entries = 0
    skipped: list[str] = []

    with open(path, "rb") as fh:
        executor = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
        try:
            for entry in toc["entries"]:
                rel = safe_relpath(entry["path"])
                if wanted is not None and rel not in wanted:
                    continue
                target = os.path.join(dest, *rel.split("/"))
                if not is_within(dest, target):
                    raise KoshError(f"refusing to write outside {dest}: {rel}")

                if entry["type"] == "dir":
                    os.makedirs(target, exist_ok=True)
                    continue
                if entry["type"] == "symlink":
                    parent = os.path.dirname(target)
                    os.makedirs(parent, exist_ok=True)
                    if os.path.lexists(target):
                        os.unlink(target)
                    try:
                        os.symlink(entry["target"], target)
                    except (OSError, NotImplementedError):
                        skipped.append(f"{rel} (symlink not supported here)")
                    continue

                os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                if os.path.exists(target) and not overwrite:
                    raise KoshError(f"refusing to overwrite {target}")

                digest = hashlib.sha256()
                pending: list[tuple[int, object]] = []
                window = max(1, workers * 2)

                def flush(sink) -> None:
                    raw_len, fut = pending.pop(0)
                    data = fut.result() if executor else fut
                    if len(data) != raw_len:
                        raise KoshError(f"block size mismatch while unpacking {rel}")
                    sink.write(data)
                    digest.update(data)

                with atomic_write(target) as sink:
                    for off, comp_len, raw_len in entry["blocks"]:
                        fh.seek(off)
                        hdr = fh.read(BLOCK_HDR.size)
                        if len(hdr) != BLOCK_HDR.size:
                            raise KoshError("container is truncated")
                        c_len, r_len = BLOCK_HDR.unpack(hdr)
                        if (c_len, r_len) != (comp_len, raw_len):
                            raise KoshError("container block header disagrees with the table of contents")
                        payload = fh.read(c_len)
                        if len(payload) != c_len:
                            raise KoshError("container is truncated")
                        pending.append((raw_len, executor.submit(_decompress, payload, raw_len, codec)
                                        if executor else _decompress(payload, raw_len, codec)))
                        if len(pending) >= window:
                            flush(sink)
                    while pending:
                        flush(sink)

                if verify and entry.get("sha256") and digest.hexdigest() != entry["sha256"]:
                    os.unlink(target)
                    raise KoshError(f"checksum mismatch for {rel}; the container is corrupt")

                if os.name != "nt":
                    os.chmod(target, entry.get("mode", 0o644))
                os.utime(target, (entry.get("mtime", time.time()), entry.get("mtime", time.time())))
                done += entry.get("size", 0)
                written_entries += 1
                if progress:
                    progress(done, total, f"extracting {rel}")
        finally:
            if executor:
                executor.shutdown(wait=True)

    if progress:
        progress(total, total, "done")
    return UnpackResult(output_dir=dest, entries=written_entries, bytes=done,
                        verified=verify, skipped=skipped)


# --------------------------------------------------------------------------- verify
@dataclass
class VerifyResult:
    path: str
    kind: str
    entries: int
    bytes: int
    ok: bool
    problems: list[str] = field(default_factory=list)
    manifest_verified: bool = False
    seconds: float = 0.0


def verify_container(path: str, deep: bool = True,
                     progress: Optional[ProgressFn] = None) -> VerifyResult:
    """Check a container without extracting anything.

    Structure is always checked (magic, footer, manifest digest, block bounds).
    With *deep*, every block is decompressed and every entry's SHA-256 is
    recomputed — the same evidence `unpack` would produce, minus the writes.
    """
    started = time.perf_counter()
    problems: list[str] = []
    size = os.path.getsize(path)
    try:
        toc = read_toc(path)
    except KoshError as exc:
        return VerifyResult(path, "kosh", 0, 0, False, [str(exc)])

    codec = toc.get("codec", "zstd")
    entries = [e for e in toc["entries"] if e["type"] == "file"]
    total = sum(e.get("size", 0) for e in entries)
    done = 0

    with open(path, "rb") as fh:
        for entry in entries:
            rel = entry["path"]
            digest = hashlib.sha256()
            for off, comp_len, raw_len in entry["blocks"]:
                if off + BLOCK_HDR.size + comp_len > size:
                    problems.append(f"{rel}: block points past the end of the file")
                    break
                fh.seek(off)
                c_len, r_len = BLOCK_HDR.unpack(fh.read(BLOCK_HDR.size))
                if (c_len, r_len) != (comp_len, raw_len):
                    problems.append(f"{rel}: block header disagrees with the manifest")
                    break
                if not deep:
                    continue
                payload = fh.read(c_len)
                try:
                    data = _decompress(payload, raw_len, codec)
                except Exception as exc:  # noqa: BLE001 - any codec failure is a problem
                    problems.append(f"{rel}: block will not decompress ({exc})")
                    break
                if len(data) != raw_len:
                    problems.append(f"{rel}: block decoded to {len(data)} bytes, expected {raw_len}")
                    break
                digest.update(data)
            if deep and entry.get("sha256") and digest.hexdigest() != entry["sha256"]:
                problems.append(f"{rel}: content hash mismatch")
            done += entry.get("size", 0)
            if progress:
                progress(done, total, f"verifying {rel}")

    return VerifyResult(
        path=os.path.abspath(path), kind="kosh", entries=len(entries), bytes=total,
        ok=not problems, problems=problems,
        manifest_verified=toc.get("version", 1) >= 2,
        seconds=time.perf_counter() - started,
    )


def block_index(path: str) -> list[dict]:
    """The physical layout, entry by entry — what `ziptastic info` does for .dr."""
    toc = read_toc(path)
    rows = []
    for entry in toc["entries"]:
        if entry["type"] != "file":
            continue
        blocks = entry.get("blocks", [])
        rows.append({
            "path": entry["path"], "size": entry.get("size", 0),
            "blocks": len(blocks),
            "first_offset": blocks[0][0] if blocks else None,
            "last_offset": blocks[-1][0] if blocks else None,
        })
    return rows
