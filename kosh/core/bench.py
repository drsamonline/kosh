"""Reproducible benchmarks against the baselines that matter.

The family rule is that a number in a README must be reproducible by a command
in the repository.  `kosh bench` generates its corpus from a fixed seed, runs
every codec on the same bytes, and prints a markdown table that can be pasted
into docs/FINDINGS.md unchanged.
"""

from __future__ import annotations

import gzip
import io
import lzma
import os
import random
import tempfile
import time
import zipfile
from dataclasses import dataclass

from . import pack as pack_mod
from .util import human_size

SEED = 20261111
# A fixed mtime keeps the container byte-identical between runs: file times are
# stored in the table of contents, so a wall-clock value would move the size.
FIXED_MTIME = 1762862400.0


@dataclass
class Row:
    codec: str
    stored: int
    seconds: float
    raw: int

    @property
    def ratio(self) -> float:
        return self.stored / self.raw if self.raw else 0.0

    @property
    def speed(self) -> float:
        return (self.raw / (1024 * 1024)) / self.seconds if self.seconds else 0.0


def build_corpus(directory: str) -> dict[str, int]:
    """Deterministic corpus: prose, records, log lines and incompressible bytes."""
    rng = random.Random(SEED)
    words = ("compression block stream entropy window literal match offset length "
             "archive container manifest digest verify codec ratio").split()
    prose = " ".join(rng.choice(words) for _ in range(60_000)) + "\n"
    rows = ["id,name,region,value,note"]
    for i in range(20_000):
        rows.append(f"{i},item-{i % 500},R{i % 12},{i * 7 % 997},line {i}")
    csv = "\n".join(rows) + "\n"
    log = "".join(
        f"2026-11-11T{h:02d}:{m:02d}:{s:02d}Z INFO worker-{i % 8} "
        f"processed batch {i} in {rng.randint(1, 900)}ms\n"
        for i, (h, m, s) in enumerate((i % 24, i % 60, i % 60) for i in range(40_000))
    )
    noise = bytes(rng.getrandbits(8) for _ in range(1_000_000))

    files = {
        "prose.txt": prose.encode(),
        "records.csv": csv.encode(),
        "service.log": log.encode(),
        "noise.bin": noise,
    }
    sizes = {}
    for name, data in files.items():
        path = os.path.join(directory, name)
        with open(path, "wb") as fh:
            fh.write(data)
        os.utime(path, (FIXED_MTIME, FIXED_MTIME))
        sizes[name] = len(data)
    return sizes


def _timed(fn) -> tuple[int, float]:
    started = time.perf_counter()
    size = fn()
    return size, time.perf_counter() - started


def run(paths: list[str] | None = None, include_images: bool = False) -> dict:
    tmp = None
    if not paths:
        tmp = tempfile.mkdtemp(prefix="kosh-bench-")
        build_corpus(tmp)
        paths = [os.path.join(tmp, name) for name in sorted(os.listdir(tmp))]
        if include_images:
            try:
                from PIL import Image  # noqa: PLC0415
                img = Image.new("RGB", (900, 600))
                px = img.load()
                for y in range(600):
                    for x in range(900):
                        px[x, y] = ((x * 255) // 900, (y * 255) // 600, (x * y) % 256)
                png = os.path.join(tmp, "image.png")
                img.save(png, compress_level=6)
                paths.append(png)
            except ImportError:
                pass

    results = []
    for path in paths:
        raw = os.path.getsize(path)
        name = os.path.basename(path)
        rows: list[Row] = []

        def add(label: str, fn) -> None:
            try:
                size, seconds = _timed(fn)
                rows.append(Row(label, size, seconds, raw))
            except Exception:  # noqa: BLE001 - a codec that fails is simply not reported
                pass

        add("gzip -9", lambda p=path: _gzip_size(p))
        add("xz -9", lambda p=path: _xz_size(p))
        add("zip -9", lambda p=path: _zip_size(p))
        for codec, level in (("zstd", 19), ("brotli", 11), ("lzma", 9)):
            add(f"kosh {codec} {level}",
                lambda p=path, c=codec, l=level: _kosh_size(p, c, l))

        best = min(rows, key=lambda r: r.stored) if rows else None
        results.append({
            "file": name, "raw": raw,
            "rows": [{"codec": r.codec, "stored": r.stored,
                      "ratio": round(r.ratio, 4), "seconds": round(r.seconds, 2),
                      "speed_mbps": round(r.speed, 2)} for r in rows],
            "best": best.codec if best else None,
        })

    if tmp:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return {"seed": SEED, "results": results}


def _gzip_size(path: str) -> int:
    with open(path, "rb") as src, io.BytesIO() as buf:
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9) as dst:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                dst.write(chunk)
        return len(buf.getvalue())


def _xz_size(path: str) -> int:
    with open(path, "rb") as src:
        data = src.read()
    return len(lzma.compress(data, preset=9))


def _zip_size(path: str) -> int:
    with io.BytesIO() as buf:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            zf.write(path, os.path.basename(path))
        return len(buf.getvalue())


def _kosh_size(path: str, codec: str, level: int) -> int:
    with tempfile.NamedTemporaryFile(suffix=".kosh", delete=False) as handle:
        out = handle.name
    try:
        pack_mod.pack([path], out, codec=codec, level=level)
        return os.path.getsize(out)
    finally:
        os.unlink(out)


def markdown(report: dict) -> str:
    lines = [
        "| file | raw | " + " | ".join(
            ["gzip -9", "xz -9", "zip -9", "kosh zstd 19", "kosh brotli 11", "kosh lzma 9"]
        ) + " | best |",
        "| --- | --- | " + " | ".join(["---"] * 7) + " |",
    ]
    for item in report["results"]:
        cells = {r["codec"]: r for r in item["rows"]}
        row = [item["file"], human_size(item["raw"])]
        for codec in ("gzip -9", "xz -9", "zip -9", "kosh zstd 19", "kosh brotli 11", "kosh lzma 9"):
            entry = cells.get(codec)
            row.append(f"{entry['stored']:,}" if entry else "—")
        row.append(item["best"] or "—")
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)
