"""Lossless re-compression: PNG, GZIP, ZIP and JPEG.

DeflOpt rewrote deflate streams byte-hunting for a shorter encoding; ECT bolted
zopfli onto PNG and GZIP behind a dozen flags.  Kosh does the same job with one
idea: decode the container, re-encode the entropy layer as small as possible,
rebuild the container so every other byte is preserved, and only replace the
file when the result is genuinely smaller.

Nothing here is lossy.  Image pixels, archive names, timestamps and file order
survive; only redundant encoding and (optionally) metadata go away.
"""

from __future__ import annotations

import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
import struct
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import zopfli.zlib

from .util import KoshError, ProgressFn, atomic_write, copy_meta, human_size

PNG_SIG = b"\x89PNG\r\n\x1a\n"
GZIP_SIG = b"\x1f\x8b"
ZIP_SIG = b"PK\x03\x04"
JPEG_SIG = b"\xff\xd8\xff"

# Metadata chunks that never affect rendering.
PNG_STRIP_CHUNKS = {b"tEXt", b"zTXt", b"iTXt", b"tIME", b"eXIf", b"prVW"}
# Chunks whose presence means "do not touch this file".
PNG_SKIP_CHUNKS = {b"acTL", b"fcTL", b"fdAT"}

ITERATIONS = {1: 5, 2: 15, 3: 40}


@dataclass
class OptimizeResult:
    path: str
    kind: str
    before: int
    after: int
    changed: bool
    note: str = ""
    bits_saved: int = 0

    @property
    def saved(self) -> int:
        return self.before - self.after

    @property
    def percent(self) -> float:
        return (self.before - self.after) * 100.0 / self.before if self.before else 0.0

    def as_dict(self) -> dict:
        return {
            "path": self.path, "kind": self.kind, "before": self.before,
            "after": self.after, "saved": self.saved, "percent": round(self.percent, 3),
            "changed": self.changed, "note": self.note,
        }


# --------------------------------------------------------------------------- detect
def sniff(data: bytes) -> str:
    if data.startswith(PNG_SIG):
        return "png"
    if data.startswith(GZIP_SIG):
        return "gzip"
    if data.startswith(ZIP_SIG):
        return "zip"
    if data.startswith(JPEG_SIG):
        return "jpeg"
    return "unknown"


# Zopfli's cost grows much faster than linearly with input size: ~20 KB/s on
# image data, versus megabytes per second for zlib.  Past a megabyte the wait
# stops being worth the last few percent, so the budget below decides per size.
ZOPFLI_SMALL = 256 * 1024
ZOPFLI_CAP = 1024 * 1024
ZOPFLI_MAX_REQUEST = 4 * 1024 * 1024


def _compress_stream(data: bytes, iterations: int) -> tuple[bytes, str]:
    """Zlib-wrapped deflate, within a predictable time budget.

    Returns the stream and the engine used, so the report can say which ran.
    """
    size = len(data)
    if size <= ZOPFLI_CAP:
        passes = iterations if size <= ZOPFLI_SMALL else min(iterations, 3)
        return zopfli.zlib.compress(data, numiterations=passes), "zopfli"
    if iterations >= 40 and size <= ZOPFLI_MAX_REQUEST:
        # Level 3 is an explicit request to wait for the smallest possible file.
        return zopfli.zlib.compress(data, numiterations=1), "zopfli"
    return zlib.compress(data, 9), "zlib-9"


def _deflate(data: bytes, iterations: int) -> tuple[bytes, str]:
    """Raw deflate (no zlib wrapper), same budget as _compress_stream."""
    stream, engine = _compress_stream(data, iterations)
    return stream[2:-4], engine


# --------------------------------------------------------------------------- PNG
def _png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    if not data.startswith(PNG_SIG):
        raise KoshError("not a PNG")
    pos, chunks = 8, []
    while pos + 12 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])
        if len(payload) != length or zlib.crc32(ctype + payload) & 0xFFFFFFFF != crc:
            raise KoshError(f"PNG chunk {ctype.decode('latin-1')} has a bad CRC")
        chunks.append((ctype, payload))
        pos += 12 + length
        if ctype == b"IEND":
            break
    return chunks


def _pack_chunk(ctype: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + ctype + payload +
            struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF))


def _shift(arr, n: int):
    """Shift an array right by n, filling with zeros (the PNG 'left' neighbour)."""
    out = np.zeros_like(arr)
    if n < arr.shape[-1]:
        out[..., n:] = arr[..., :-n]
    return out


def _decode_rows(raw: bytes, height: int, bpp: int, stride: int):
    """Undo PNG row filters into a (height, stride) uint8 matrix."""
    buf = np.frombuffer(raw, dtype=np.uint8, count=height * (stride + 1))
    buf = buf.reshape(height, stride + 1)
    filters = buf[:, 0]
    rows = np.empty((height, stride), dtype=np.uint8)
    prev = np.zeros(stride, dtype=np.uint8)
    for y in range(height):
        line = buf[y, 1:].copy()
        ftype = int(filters[y])
        if ftype == 0:
            pass
        elif ftype == 1:
            line = np.cumsum(line.reshape(-1, bpp), axis=0, dtype=np.uint8).reshape(-1)
        elif ftype == 2:
            line = line + prev
        elif ftype in (3, 4):
            left = np.zeros(stride, dtype=np.int16)
            for i in range(stride):
                a = int(left[i - bpp]) if i >= bpp else 0
                if ftype == 3:
                    pred = (a + int(prev[i])) >> 1
                else:
                    b = int(prev[i])
                    c = int(prev[i - bpp]) if i >= bpp else 0
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                value = (int(line[i]) + pred) & 0xFF
                line[i] = value
                left[i] = value
        else:
            raise KoshError(f"PNG row uses filter {ftype}, which does not exist")
        rows[y] = line
        prev = line
    return rows


def _filter_rows(rows, bpp: int, ftype: int):
    """Apply one PNG filter to every row, vectorised."""
    if ftype == 0:
        return rows
    a = _shift(rows, bpp)
    if ftype == 1:
        return rows - a
    prev = np.zeros_like(rows)
    prev[1:] = rows[:-1]
    if ftype == 2:
        return rows - prev
    if ftype == 3:
        return rows - ((a.astype(np.int16) + prev) >> 1).astype(np.uint8)
    b = prev
    c = _shift(prev, bpp)
    p = a.astype(np.int16) + b - c
    pa = np.abs(p - a)
    pb = np.abs(p - b)
    pc = np.abs(p - c)
    pred = np.where((pa <= pb) & (pa <= pc), a, np.where(pb <= pc, b, c))
    return rows - pred.astype(np.uint8)


def _filter_row(line, prev, bpp: int, ftype: int):
    """Filter one row against its already-decoded predecessor."""
    if ftype == 0:
        return line
    a = _shift(line, bpp)
    if ftype == 1:
        return line - a
    if ftype == 2:
        return line - prev
    if ftype == 3:
        return line - ((a.astype(np.int16) + prev) >> 1).astype(np.uint8)
    b = prev
    c = _shift(prev, bpp)
    p = a.astype(np.int16) + b - c
    pa = np.abs(p - a)
    pb = np.abs(p - b)
    pc = np.abs(p - c)
    pred = np.where((pa <= pb) & (pa <= pc), a, np.where(pb <= pc, b, c))
    return line - pred.astype(np.uint8)


def _score(block) -> int:
    signed = block.astype(np.int16)
    signed = np.where(signed < 128, signed, signed - 256)
    return int(np.abs(signed).sum())


def _encode_rows(rows, bpp: int, strategy: int) -> bytes:
    """Serialise filtered rows.  strategy 0-4 forces a filter, -1 picks per row."""
    height, stride = rows.shape
    out = np.empty((height, stride + 1), dtype=np.uint8)
    if strategy >= 0:
        out[:, 1:] = _filter_rows(rows, bpp, strategy)
        out[:, 0] = strategy
        return out.tobytes()

    prev = np.zeros(stride, dtype=np.uint8)
    for y in range(height):
        line = rows[y]
        best, best_score, best_bytes = 0, None, line
        for ftype in range(5):
            cand = _filter_row(line, prev, bpp, ftype)
            score = _score(cand)
            if best_score is None or score < best_score:
                best, best_score, best_bytes = ftype, score, cand
        out[y, 0] = best
        out[y, 1:] = best_bytes
        prev = line
    return out.tobytes()


def optimize_png(data: bytes, iterations: int, strip: bool, trials: bool) -> tuple[bytes, str]:
    chunks = _png_chunks(data)
    if not chunks or chunks[0][0] != b"IHDR":
        raise KoshError("PNG is missing its IHDR header")
    types = {c[0] for c in chunks}
    if types & PNG_SKIP_CHUNKS:
        return data, "animated PNG"

    width, height, depth, colour, comp, filt, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    if interlace:
        return data, "interlaced PNG"
    if comp != 0 or filt != 0:
        return data, "non-standard compression"

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colour)
    if channels is None:
        return data, f"colour type {colour}"
    if depth not in (1, 2, 4, 8, 16):
        return data, f"bit depth {depth}"

    idat = b"".join(p for t, p in chunks if t == b"IDAT")
    if not idat:
        return data, "no image data"
    try:
        raw = zlib.decompress(idat)
    except zlib.error as exc:
        raise KoshError(f"PNG image data will not inflate: {exc}") from exc

    bpp = max(1, (channels * depth + 7) // 8)
    stride = (width * channels * depth + 7) // 8
    if len(raw) < height * (stride + 1):
        raise KoshError("PNG image data is shorter than its header claims")

    rows = _decode_rows(raw, height, bpp, stride)

    # Rank candidate filters with fast zlib, then spend zopfli once on the winner.
    strategies = [-1, 0, 1, 2, 3, 4] if trials else [-1]
    ranked = sorted(
        ((len(zlib.compress(_encode_rows(rows, bpp, s), 6)), s) for s in strategies)
    )
    finalists = ranked[:2] if trials and iterations >= 20 else ranked[:1]
    best = None
    for _, strategy in finalists:
        stream, engine = _compress_stream(_encode_rows(rows, bpp, strategy), iterations)
        if best is None or len(stream) < len(best[1]):
            best = (strategy, stream, engine)
    assert best is not None
    new_idat = best[1]
    engine = best[2]

    kept: list[tuple[bytes, bytes]] = []
    for ctype, payload in chunks:
        if ctype == b"IDAT":
            continue
        if strip and ctype in PNG_STRIP_CHUNKS:
            continue
        kept.append((ctype, payload))
    out = bytearray(PNG_SIG)
    inserted = False
    for ctype, payload in kept:
        if ctype == b"IEND" and not inserted:
            out += _pack_chunk(b"IDAT", new_idat)
            inserted = True
        out += _pack_chunk(ctype, payload)
    note = f"IDAT {'auto' if best[0] < 0 else best[0]} · {engine}"
    if strip:
        note += " + stripped"
    return bytes(out), note


# --------------------------------------------------------------------------- GZIP
def optimize_gzip(data: bytes, iterations: int, strip: bool) -> tuple[bytes, str]:
    if len(data) < 18 or not data.startswith(GZIP_SIG):
        raise KoshError("not a gzip file")
    if data[2] != 8:
        return data, "not deflate"
    flags = data[3]
    pos = 10
    extra = b""
    if flags & 0x04:  # FEXTRA
        (xlen,) = struct.unpack("<H", data[pos:pos + 2])
        extra = data[pos + 2:pos + 2 + xlen]
        pos += 2 + xlen
    name = comment = None
    if flags & 0x08:  # FNAME
        end = data.index(b"\x00", pos)
        name = data[pos:end]
        pos = end + 1
    if flags & 0x10:  # FCOMMENT
        end = data.index(b"\x00", pos)
        comment = data[pos:end]
        pos = end + 1
    if flags & 0x02:  # FHCRC
        pos += 2
    body = data[pos:-8]
    try:
        raw = zlib.decompress(body, -15)
    except zlib.error as exc:
        raise KoshError(f"gzip payload will not inflate: {exc}") from exc
    crc, isize = struct.unpack("<II", data[-8:])
    if zlib.crc32(raw) & 0xFFFFFFFF != crc:
        raise KoshError("gzip CRC does not match its payload")

    # Rebuild the header from what we parsed, so no optional field is mangled.
    new_flags = flags & ~0x10 if strip else flags
    if strip:
        new_flags &= ~0x08
    header = bytearray(GZIP_SIG)
    header.append(8)
    header.append(new_flags)
    header += struct.pack("<I", 0 if strip else struct.unpack("<I", data[4:8])[0])
    header.append(data[8])  # XFL
    header.append(data[9])  # OS
    if flags & 0x04 and extra:
        header += struct.pack("<H", len(extra)) + extra
    if new_flags & 0x08 and name is not None:
        header += name + b"\x00"
    if new_flags & 0x10 and comment is not None:
        header += comment + b"\x00"
    body, engine = _deflate(raw, iterations)
    out = bytes(header) + body + struct.pack("<II", crc, isize)
    note = f"re-deflated · {engine}"
    if strip:
        note += " + stripped"
    return out, note


# --------------------------------------------------------------------------- ZIP
def _read_raw_payload(fh, info: zipfile.ZipInfo) -> bytes:
    fh.seek(info.header_offset)
    header = fh.read(30)
    if header[:4] != ZIP_SIG:
        raise KoshError(f"bad local header for {info.filename}")
    name_len, extra_len = struct.unpack("<HH", header[26:30])
    fh.seek(info.header_offset + 30 + name_len + extra_len)
    return fh.read(info.compress_size)


def _dos_time(dt: tuple[int, int, int, int, int, int]) -> tuple[int, int]:
    year, month, day, hour, minute, second = dt
    year = max(1980, min(2107, year))
    dosdate = ((year - 1980) << 9) | (month << 5) | day
    dostime = (hour << 11) | (minute << 5) | (second // 2)
    return dostime, dosdate


def optimize_zip(path: str, data: bytes, iterations: int, strip: bool) -> tuple[bytes, str]:
    import io

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise KoshError(f"zip will not open: {exc}") from exc

    for info in infos:
        if info.file_size > 0xFFFFFFF0 or info.header_offset > 0xFFFFFFF0:
            return data, "ZIP64, skipped"
        if info.flag_bits & 0x1:
            return data, "encrypted, skipped"

    out = bytearray()
    central = bytearray()
    improved = 0
    with open(path, "rb") as src:
        for info in infos:
            raw_payload = _read_raw_payload(src, info)
            name = info.filename.encode("utf-8" if info.flag_bits & 0x800 else "cp437",
                                        errors="replace")
            if info.is_dir():
                method, payload, usize, crc = 0, b"", 0, 0
            else:
                content = zf.read(info)
                usize = len(content)
                crc = zlib.crc32(content) & 0xFFFFFFFF
                if info.compress_type == zipfile.ZIP_DEFLATED:
                    candidate, engine = _deflate(content, iterations)
                    if len(candidate) < len(raw_payload):
                        method, payload = 8, candidate
                        improved += 1
                    else:
                        method, payload = 8, raw_payload
                elif info.compress_type == zipfile.ZIP_STORED:
                    candidate, engine = _deflate(content, iterations)
                    if len(candidate) < usize:
                        method, payload = 8, candidate
                        improved += 1
                    else:
                        method, payload = 0, content
                else:  # bzip2 / lzma / unknown: copy through untouched
                    method, payload = info.compress_type, raw_payload

            extra = b"" if strip else info.extra
            comment = b"" if strip else info.comment
            dostime, dosdate = _dos_time(info.date_time)
            offset = len(out)
            flags = info.flag_bits & ~0x08  # we always know sizes: drop data descriptor
            out += (struct.pack("<4sHHHHHIIIHH", ZIP_SIG, info.extract_version or 20,
                                flags, method, dostime, dosdate, crc, len(payload), usize,
                                len(name), len(extra)) + name + extra + payload)
            central += (struct.pack("<4sHHHHHHIIIHHHHHII", b"PK\x01\x02",
                                    info.create_version or 20, info.extract_version or 20,
                                    flags, method, dostime, dosdate, crc, len(payload), usize,
                                    len(name), len(extra), len(comment), 0, info.internal_attr,
                                    info.external_attr, offset)
                        + name + extra + comment)

    cd_offset = len(out)
    out += central
    out += struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, len(infos), len(infos),
                       len(central), cd_offset, 0)
    return bytes(out), f"{improved}/{len(infos)} entries · {engine}"


# --------------------------------------------------------------------------- JPEG
def optimize_jpeg(data: bytes) -> tuple[bytes, str]:
    if not data.startswith(JPEG_SIG):
        raise KoshError("not a JPEG")
    out = bytearray(data[:2])
    pos = 2
    stripped = 0
    keep_app = {0xE0, 0xEE}  # JFIF and Adobe: colour interpretation depends on them
    while pos < len(data) - 1:
        if data[pos] != 0xFF:
            out += data[pos:]
            break
        marker = data[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            out += data[pos:pos + 2]
            pos += 2
            continue
        if marker == 0xDA:  # start of scan: copy the rest verbatim
            out += data[pos:]
            break
        (length,) = struct.unpack(">H", data[pos + 2:pos + 4])
        segment = data[pos:pos + 2 + length]
        drop = (0xE1 <= marker <= 0xEF and marker not in keep_app) or marker == 0xFE
        if drop:
            stripped += 1
        else:
            out += segment
        pos += 2 + length
    if len(out) < 4:
        return data, "malformed JPEG"
    return bytes(out), f"dropped {stripped} segment(s)"


# --------------------------------------------------------------------------- driver
def optimize(path: str, level: int = 2, strip: bool = False, force: bool = False,
             dry_run: bool = False, backup: bool = False,
             progress: Optional[ProgressFn] = None) -> OptimizeResult:
    if level not in ITERATIONS:
        raise KoshError("level must be 1 (fast), 2 (balanced) or 3 (max)")
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise KoshError(f"not a file: {path}")
    st = os.stat(path)
    before = st.st_size
    with open(path, "rb") as fh:
        data = fh.read()
    kind = sniff(data)
    iterations = ITERATIONS[level]

    try:
        if kind == "png":
            new, note = optimize_png(data, iterations, strip, trials=level >= 2)
        elif kind == "gzip":
            new, note = optimize_gzip(data, iterations, strip)
        elif kind == "zip":
            new, note = optimize_zip(path, data, iterations, strip)
        elif kind == "jpeg":
            new, note = optimize_jpeg(data) if strip else (data, "use --strip for EXIF")
        else:
            return OptimizeResult(path, "unknown", before, before, False,
                                  "unsupported format")
    except KoshError as exc:
        return OptimizeResult(path, kind, before, before, False, str(exc))

    after = len(new)
    changed = after < before or (force and after != before)
    if not changed:
        if kind == "jpeg" and not strip:
            note = "use --strip for EXIF"
        elif after >= before:
            note = "no gain"
        return OptimizeResult(path, kind, before, before, False, note,
                              bits_saved=(before - after) * 8)
    if dry_run:
        return OptimizeResult(path, kind, before, after, True, note + " (dry run)",
                              bits_saved=(before - after) * 8)

    if backup:
        shutil.copy2(path, path + ".bak")
    with atomic_write(path) as fh:
        fh.write(new)
    # atomic_write creates a new inode; put the original timestamps back.
    try:
        os.utime(path, (st.st_atime, st.st_mtime))
        if os.name != "nt":
            os.chmod(path, st.st_mode & 0o7777)
    except OSError:
        pass
    if progress:
        progress(before, before, os.path.basename(path))
    return OptimizeResult(path, kind, before, after, True, note,
                          bits_saved=(before - after) * 8)


def optimize_many(paths: list[str], level: int = 2, strip: bool = False,
                  force: bool = False, dry_run: bool = False, backup: bool = False,
                  jobs: int = 1, progress: Optional[ProgressFn] = None) -> list[OptimizeResult]:
    """Optimise several files, optionally in parallel (zopfli is CPU-bound)."""
    total = len(paths)
    results: list[Optional[OptimizeResult]] = [None] * total
    counter = {"done": 0}
    lock = threading.Lock()

    def work(index: int) -> None:
        results[index] = optimize(paths[index], level=level, strip=strip, force=force,
                                  dry_run=dry_run, backup=backup)
        with lock:
            counter["done"] += 1
            if progress:
                progress(counter["done"], total, os.path.basename(paths[index]))

    if jobs > 1 and total > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(work, range(total)))
    else:
        for index in range(total):
            work(index)
    if progress:
        progress(total, total, "done")
    return [r for r in results if r is not None]
