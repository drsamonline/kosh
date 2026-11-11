"""Archive create / list / extract for zip, tar and Kosh containers.

WinRK wrapped a proprietary archive format in a Windows-only GUI.  Kosh keeps
the workflow — pick files, make one file, get them back — but speaks formats
every other machine already reads.
"""

from __future__ import annotations

import contextlib
import os
import tarfile
import time
import zipfile
from dataclasses import dataclass, field
from typing import Optional

import zstandard

from .util import KoshError, ProgressFn, human_size, is_within, safe_relpath
from . import pack as kosh_pack

ZIP_EXT = (".zip",)
TAR_EXT = (".tar", ".tar.gz", ".tgz", ".tar.zst", ".tzst", ".tar.xz")


@dataclass
class ArchiveResult:
    output: str
    fmt: str
    entries: int
    raw_size: int
    stored_size: int
    seconds: float


@dataclass
class ExtractResult:
    output_dir: str
    entries: int
    bytes: int
    skipped: list[str] = field(default_factory=list)


def detect_format(path: str) -> str:
    low = path.lower()
    if low.endswith(".kosh"):
        return "kosh"
    if low.endswith(ZIP_EXT):
        return "zip"
    if low.endswith(TAR_EXT):
        return "tar"
    raise KoshError(
        "cannot tell the format from the extension; use .kosh, .zip or .tar[.gz|.zst|.xz]"
    )


@contextlib.contextmanager
def _open_tar(path: str):
    """Open a tar, including zstd-compressed tar, which stdlib tarfile lacks."""
    low = path.lower()
    if low.endswith((".tar.zst", ".tzst")):
        with open(path, "rb") as raw:
            reader = zstandard.ZstdDecompressor().stream_reader(raw)
            with tarfile.open(fileobj=reader, mode="r|") as tf:
                yield tf
    else:
        with tarfile.open(path, "r:*") as tf:
            yield tf


def _walk(inputs):
    for raw in inputs:
        path = os.path.abspath(raw)
        if not os.path.exists(path):
            raise KoshError(f"no such file or directory: {raw}")
        if os.path.isfile(path):
            yield path, os.path.basename(path)
            continue
        base = os.path.dirname(path)
        for root, dirs, files in os.walk(path):
            dirs.sort()
            files.sort()
            rel_root = os.path.relpath(root, base).replace(os.sep, "/")
            yield root, rel_root
            for name in files:
                full = os.path.join(root, name)
                yield full, os.path.relpath(full, base).replace(os.sep, "/")


def create(out: str, inputs: list[str], fmt: Optional[str] = None,
           level: int = 9, progress: Optional[ProgressFn] = None) -> ArchiveResult:
    fmt = fmt or detect_format(out)
    started = time.perf_counter()
    raw_size = 0
    count = 0

    if fmt == "kosh":
        result = kosh_pack.pack(inputs, out, codec="zstd", level=min(level + 9, 19),
                                progress=progress)
        return ArchiveResult(result.output, fmt, result.entries, result.raw_size,
                             result.stored_size, result.seconds)

    if fmt == "zip":
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=max(1, min(level, 9))) as zf:
            for path, arcname in _walk(inputs):
                arcname = arcname.replace(os.sep, "/")
                if os.path.isdir(path):
                    continue
                zf.write(path, arcname)
                raw_size += os.path.getsize(path)
                count += 1
                if progress:
                    progress(count, count, f"adding {arcname}")
    elif fmt == "tar":
        mode = "w"
        low = out.lower()
        if low.endswith((".tar.gz", ".tgz")):
            mode = "w:gz"
        elif low.endswith((".tar.zst", ".tzst")):
            mode = "w"
        elif low.endswith(".tar.xz"):
            mode = "w:xz"
        if low.endswith((".tar.zst", ".tzst")):
            compressor = zstandard.ZstdCompressor(level=max(1, min(level + 10, 22)))
            with open(out, "wb") as raw:
                with compressor.stream_writer(raw, closefd=False) as stream:
                    with tarfile.open(fileobj=stream, mode="w|") as tf:
                        for path, arcname in _walk(inputs):
                            tf.add(path, arcname=arcname, recursive=False)
                            if os.path.isfile(path):
                                raw_size += os.path.getsize(path)
                                count += 1
        else:
            with tarfile.open(out, mode) as tf:
                for path, arcname in _walk(inputs):
                    tf.add(path, arcname=arcname, recursive=False)
                    if os.path.isfile(path):
                        raw_size += os.path.getsize(path)
                        count += 1
    else:  # pragma: no cover - guarded by detect_format
        raise KoshError(f"unsupported format {fmt}")

    stored = os.path.getsize(out)
    return ArchiveResult(os.path.abspath(out), fmt, count, raw_size, stored,
                         time.perf_counter() - started)


def list_archive(path: str) -> dict:
    fmt = detect_format(path)
    if fmt == "kosh":
        info = kosh_pack.list_container(path)
        return {
            "path": info["path"], "format": "kosh", "codec": info["codec"],
            "stored_size": info["stored_size"], "raw_size": info["raw_size"],
            "entries": [
                {"name": e["path"], "size": e.get("size", 0), "type": e["type"]}
                for e in info["entries"]
            ],
        }
    if fmt == "zip":
        with zipfile.ZipFile(path) as zf:
            entries = [
                {"name": i.filename, "size": i.file_size,
                 "compressed": i.compress_size, "type": "dir" if i.is_dir() else "file"}
                for i in zf.infolist()
            ]
    else:
        with _open_tar(path) as tf:
            entries = [
                {"name": m.name, "size": m.size,
                 "type": "dir" if m.isdir() else ("symlink" if m.issym() else "file")}
                for m in tf
            ]
    return {
        "path": os.path.abspath(path), "format": fmt,
        "stored_size": os.path.getsize(path),
        "raw_size": sum(e["size"] for e in entries),
        "entries": entries,
    }


def extract(path: str, dest: str = ".", members: Optional[list[str]] = None,
            progress: Optional[ProgressFn] = None) -> ExtractResult:
    fmt = detect_format(path)
    if fmt == "kosh":
        result = kosh_pack.unpack(path, dest, members=members, progress=progress)
        return ExtractResult(result.output_dir, result.entries, result.bytes, result.skipped)

    dest = os.path.abspath(dest)
    os.makedirs(dest, exist_ok=True)
    total = 0
    count = 0
    skipped: list[str] = []

    def target_for(name: str) -> str:
        rel = safe_relpath(name)
        target = os.path.join(dest, *rel.split("/"))
        if not is_within(dest, target):
            raise KoshError(f"refusing to extract outside {dest}: {name}")
        return target

    if fmt == "zip":
        with zipfile.ZipFile(path) as zf:
            names = members or zf.namelist()
            for name in names:
                info = zf.getinfo(name)
                target = target_for(name)
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    while True:
                        buf = src.read(1 << 20)
                        if not buf:
                            break
                        dst.write(buf)
                        total += len(buf)
                        if progress:
                            progress(total, total, f"extracting {name}")
                count += 1
    else:
        with _open_tar(path) as tf:
            for member in tf:
                if members and member.name not in members:
                    continue
                target = target_for(member.name)
                if member.isdir():
                    os.makedirs(target, exist_ok=True)
                    continue
                if member.issym() or member.islnk():
                    skipped.append(f"{member.name} (link not extracted)")
                    continue
                os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                src = tf.extractfile(member)
                if src is None:
                    continue
                with src, open(target, "wb") as dst:
                    while True:
                        buf = src.read(1 << 20)
                        if not buf:
                            break
                        dst.write(buf)
                        total += len(buf)
                        if progress:
                            progress(total, total, f"extracting {member.name}")
                count += 1

    return ExtractResult(dest, count, total, skipped)
