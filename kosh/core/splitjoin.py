"""Lossless file splitting and joining.

Replaces SplitSat's "split into N parts" with something verifiable: every part
carries its own SHA-256 in a manifest, so a join can prove it reassembled the
original file byte for byte instead of trusting that the parts arrived intact.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Optional

from .util import (
    BLOCK, KoshError, ProgressFn, atomic_write, copy_meta, hash_file,
    human_size, new_hash, parse_size, safe_relpath,
)

MANIFEST_SUFFIX = ".koshsplit.json"
PART_RE = re.compile(r"^(?P<prefix>.+)\.part(?P<num>\d{3,})$")
FORMAT_VERSION = 1


@dataclass
class SplitResult:
    manifest: str
    parts: list[str]
    total: int
    part_size: int
    sha256: str


@dataclass
class JoinResult:
    output: str
    total: int
    sha256: str
    verified: bool


def _part_name(prefix: str, index: int) -> str:
    return f"{prefix}.part{index:03d}"


def split(path: str, out_dir: Optional[str] = None, part_size: str | int | None = None,
          parts: int | None = None, prefix: Optional[str] = None,
          algo: str = "sha256", progress: Optional[ProgressFn] = None) -> SplitResult:
    """Split *path* by byte size or by number of parts.

    Exactly one of *part_size* / *parts* must be given.  A manifest describing
    every part is written next to them so ``join`` is self-describing.
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise KoshError(f"not a file: {path}")
    total = os.path.getsize(path)
    if total == 0:
        raise KoshError("refusing to split an empty file")

    if (part_size is None) == (parts is None):
        raise KoshError("give either a part size or a part count")

    if parts is not None:
        if parts < 2:
            raise KoshError("a split needs at least 2 parts")
        chunk = -(-total // parts)  # ceil
    else:
        chunk = parse_size(part_size)  # type: ignore[arg-type]
        if chunk >= total:
            raise KoshError(
                f"part size {human_size(chunk)} is not smaller than the file ({human_size(total)})"
            )

    out_dir = os.path.abspath(out_dir or os.path.dirname(path) or ".")
    os.makedirs(out_dir, exist_ok=True)
    base = prefix or os.path.splitext(os.path.basename(path))[0]
    if not base:
        raise KoshError("cannot derive a part name; pass --prefix")

    source_hash = new_hash(algo)
    part_records: list[dict] = []
    written = 0
    index = 1

    with open(path, "rb") as src:
        current = None
        current_hash = None
        current_name = None
        current_size = 0

        def close_current():
            nonlocal current, current_hash, current_name, current_size
            if current is None:
                return
            current.flush()
            os.fsync(current.fileno())
            current.close()
            part_records.append({
                "name": current_name,
                "size": current_size,
                algo: current_hash.hexdigest(),
            })
            current = None
            current_hash = None
            current_name = None
            current_size = 0

        while True:
            buf = src.read(BLOCK)
            if not buf:
                break
            source_hash.update(buf)
            pos = 0
            while pos < len(buf):
                if current is None:
                    current_name = _part_name(base, index)
                    index += 1
                    current = open(os.path.join(out_dir, current_name), "wb")
                    current_hash = new_hash(algo)
                    current_size = 0
                room = chunk - current_size
                piece = buf[pos:pos + room]
                current.write(piece)
                current_hash.update(piece)
                current_size += len(piece)
                written += len(piece)
                pos += len(piece)
                if current_size >= chunk:
                    close_current()
            if progress:
                progress(written, total, f"writing part {index - 1}")
        close_current()

    digest = source_hash.hexdigest()
    manifest = {
        "format": "kosh-split",
        "version": FORMAT_VERSION,
        "algo": algo,
        "source": {"name": os.path.basename(path), "size": total, algo: digest},
        "part_size": chunk,
        "parts": part_records,
    }
    manifest_path = os.path.join(out_dir, f"{base}{MANIFEST_SUFFIX}")
    with atomic_write(manifest_path, "w") as fh:  # type: ignore[arg-type]
        json.dump(manifest, fh, indent=1)
        fh.write("\n")

    return SplitResult(
        manifest=manifest_path,
        parts=[os.path.join(out_dir, r["name"]) for r in part_records],
        total=total,
        part_size=chunk,
        sha256=digest,
    )


def _load_manifest(target: str) -> tuple[dict, str]:
    """Find and read the manifest for a manifest path, a part, or a directory."""
    target = os.path.abspath(target)
    if os.path.isdir(target):
        found = [f for f in sorted(os.listdir(target)) if f.endswith(MANIFEST_SUFFIX)]
        if len(found) != 1:
            raise KoshError(
                f"expected exactly one *{MANIFEST_SUFFIX} in {target}, found {len(found)}"
            )
        manifest_path = os.path.join(target, found[0])
    elif target.endswith(MANIFEST_SUFFIX):
        manifest_path = target
    else:
        m = PART_RE.match(os.path.basename(target))
        if not m:
            raise KoshError(f"not a Kosh part or manifest: {target}")
        candidate = os.path.join(
            os.path.dirname(target), m.group("prefix") + MANIFEST_SUFFIX
        )
        if not os.path.exists(candidate):
            raise KoshError(f"missing manifest {os.path.basename(candidate)} next to the parts")
        manifest_path = candidate

    with open(manifest_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("format") != "kosh-split":
        raise KoshError(f"{manifest_path} is not a Kosh split manifest")
    if data.get("version", 0) > FORMAT_VERSION:
        raise KoshError("manifest was written by a newer Kosh; please upgrade")
    return data, manifest_path


def join(target: str, out: Optional[str] = None, verify: bool = True,
         overwrite: bool = False, progress: Optional[ProgressFn] = None) -> JoinResult:
    """Reassemble parts described by a manifest (or any part file)."""
    manifest, manifest_path = _load_manifest(target)
    base_dir = os.path.dirname(manifest_path)
    algo = manifest.get("algo", "sha256")
    total = int(manifest["source"]["size"])

    if out is None:
        out = os.path.join(base_dir, manifest["source"]["name"])
    out = os.path.abspath(out)
    if os.path.exists(out) and not overwrite:
        raise KoshError(f"output already exists: {out} (use --force to overwrite)")

    source_digest = new_hash(algo)
    written = 0
    with atomic_write(out) as dst:
        for record in manifest["parts"]:
            name = safe_relpath(record["name"])
            part_path = os.path.join(base_dir, name)
            if not os.path.isfile(part_path):
                raise KoshError(f"missing part: {name}")
            size = os.path.getsize(part_path)
            if size != int(record["size"]):
                raise KoshError(
                    f"part {name} is {human_size(size)}, expected {human_size(record['size'])}"
                )
            part_hash = new_hash(algo)
            with open(part_path, "rb") as src:
                while True:
                    buf = src.read(BLOCK)
                    if not buf:
                        break
                    part_hash.update(buf)
                    source_digest.update(buf)
                    dst.write(buf)
                    written += len(buf)
                    if progress:
                        progress(written, total, f"joining {name}")
            if verify and part_hash.hexdigest() != record.get(algo):
                raise KoshError(f"checksum mismatch in {name}; the part is corrupt")

    digest = source_digest.hexdigest()
    if verify and digest != manifest["source"].get(algo):
        os.unlink(out)
        raise KoshError("reassembled file does not match the original checksum")
    if progress:
        progress(total, total, "joined")
    return JoinResult(output=out, total=written, sha256=digest, verified=verify)


def inspect(target: str) -> dict:
    """Describe a split set without touching the data."""
    manifest, manifest_path = _load_manifest(target)
    base_dir = os.path.dirname(manifest_path)
    parts = []
    for record in manifest["parts"]:
        p = os.path.join(base_dir, safe_relpath(record["name"]))
        parts.append({
            "name": record["name"],
            "size": record["size"],
            "present": os.path.isfile(p),
        })
    return {
        "manifest": manifest_path,
        "source": manifest["source"]["name"],
        "total": manifest["source"]["size"],
        "part_size": manifest.get("part_size"),
        "algo": manifest.get("algo"),
        "parts": parts,
        "complete": all(p["present"] for p in parts),
    }
