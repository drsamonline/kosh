"""Shared primitives: sizes, hashing, atomic writes, progress reporting.

Everything in Kosh is streamed in fixed-size blocks so memory use stays flat
regardless of input size.  These helpers are the only place that touches raw
files, which keeps the rest of the engine testable.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Optional

BLOCK = 8 * 1024 * 1024  # 8 MiB streaming block

# progress(done_bytes, total_bytes, message)
ProgressFn = Callable[[int, int, str], None]


class KoshError(Exception):
    """User-facing failure.  The CLI prints these without a traceback."""


# --------------------------------------------------------------------------- sizes
_UNITS = {
    "b": 1,
    "k": 1000, "kb": 1000, "kib": 1024,
    "m": 1000**2, "mb": 1000**2, "mib": 1024**2,
    "g": 1000**3, "gb": 1000**3, "gib": 1024**3,
    "t": 1000**4, "tb": 1000**4, "tib": 1024**4,
}


def parse_size(value: str | int | float) -> int:
    """Parse ``"700M"``, ``"1.5 GiB"``, ``"4096"`` into a byte count."""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower().replace("_", "")
    m = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*([a-z]*)", text)
    if not m:
        raise KoshError(f"cannot read size {value!r} (try 700M, 1.5G, 4096)")
    number, unit = float(m.group(1)), m.group(2) or "b"
    if unit not in _UNITS:
        raise KoshError(f"unknown size unit {unit!r} in {value!r}")
    size = int(number * _UNITS[unit])
    if size <= 0:
        raise KoshError("size must be greater than zero")
    return size


def human_size(n: float | int | None) -> str:
    if n is None:
        return "-"
    n = float(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    for unit, scale in (("B", 1), ("KB", 1000), ("MB", 1000**2), ("GB", 1000**3), ("TB", 1000**4)):
        if n < scale * 1000 or unit == "TB":
            if unit == "B":
                return f"{sign}{int(n)} B"
            return f"{sign}{n / scale:.2f} {unit}"
    return f"{sign}{n:.0f} B"


def human_bits(bits: int) -> str:
    if bits < 8:
        return f"{bits} bit" + ("s" if bits != 1 else "")
    return f"{bits / 8:.3f} B ({bits} bits)"


def percent(before: int, after: int) -> float:
    if before <= 0:
        return 0.0
    return (before - after) * 100.0 / before


# --------------------------------------------------------------------------- hashing
def new_hash(algo: str = "sha256"):
    try:
        return hashlib.new(algo)
    except ValueError as exc:  # pragma: no cover - defensive
        raise KoshError(f"unknown hash {algo!r}") from exc


def hash_file(path: str, algo: str = "sha256", chunk: int = BLOCK,
              progress: Optional[ProgressFn] = None) -> str:
    h = new_hash(algo)
    total = os.path.getsize(path)
    done = 0
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            h.update(buf)
            done += len(buf)
            if progress:
                progress(done, total, "hashing")
    return h.hexdigest()


# --------------------------------------------------------------------------- files
def fsync_directory(directory: str) -> None:
    """Flush a directory entry, so a rename survives a crash.

    POSIX only in any meaningful sense: Windows has no directory handle to flush,
    and `os.open` on a directory fails there, so the failure is swallowed. That is
    deliberate — a durability nicety must not turn into a write error.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - Windows, or a filesystem without it
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


@contextlib.contextmanager
def atomic_write(path: str, mode: str = "wb"):
    """Write to a sibling temp file and rename on success.

    A crash mid-write never leaves a half-written destination behind, and the
    original file keeps its inode until the swap.
    """
    path = os.path.abspath(path)
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".kosh-", suffix=".tmp", dir=parent)
    os.close(fd)
    try:
        with open(tmp, mode) as fh:
            yield fh
            fh.flush()
            os.fsync(fh.fileno())
        yield_path = tmp
        os.replace(yield_path, path)
        # The rename is atomic, the directory entry is not: without this fsync a
        # crash can lose the new name while the file it points at survives — the
        # worst of both states. Best-effort, because a filesystem that does not
        # support directory fsync is not a reason to fail the write.
        fsync_directory(parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def iter_file(path: str, chunk: int = BLOCK) -> Iterator[bytes]:
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                return
            yield buf


def unique_path(path: str) -> str:
    """Return a non-clashing path (``file.zip`` -> ``file (2).zip``)."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"


def copy_meta(src: str, dst: str) -> None:
    """Best-effort mtime/mode copy (Windows ignores mode bits)."""
    try:
        st = os.stat(src)
        os.utime(dst, (st.st_atime, st.st_mtime))
        if os.name != "nt":
            os.chmod(dst, st.st_mode & 0o7777)
    except OSError:
        pass


def is_within(base: str, candidate: str) -> bool:
    base_abs = os.path.abspath(base)
    cand_abs = os.path.abspath(candidate)
    try:
        return os.path.commonpath([base_abs, cand_abs]) == base_abs
    except ValueError:  # different drives on Windows
        return False


def safe_relpath(name: str) -> str:
    """Reject absolute paths and traversal in archive member names."""
    name = name.replace("\\", "/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or (parts and re.match(r"^[A-Za-z]:", parts[0])):
        raise KoshError(f"unsafe path in archive: {name!r}")
    return "/".join(parts)


@dataclass
class Timer:
    started: float = field(default_factory=time.perf_counter)

    def stop(self) -> float:
        return time.perf_counter() - self.started


def rate(bytes_done: int, seconds: float) -> str:
    if seconds <= 0:
        return "-"
    return f"{human_size(bytes_done / seconds)}/s"


def cpu_count(default: int = 4) -> int:
    return os.cpu_count() or default


def which_all(names: Iterable[str]) -> dict[str, str | None]:
    return {n: shutil.which(n) for n in names}
