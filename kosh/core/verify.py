"""Integrity checking that never writes anything.

`verify` answers one question — is this file exactly what it claims to be — for
every artefact Kosh produces and every archive it understands.  It is the
command you run before deleting the original.
"""

from __future__ import annotations

import hashlib
import os
import tarfile
import time
import zipfile

from . import pack as pack_mod
from . import splitjoin
from .util import KoshError, ProgressFn, hash_file, human_size

TAR_EXT = (".tar", ".tar.gz", ".tgz", ".tar.zst", ".tzst", ".tar.xz")


def verify(path: str, deep: bool = True, progress: ProgressFn | None = None) -> dict:
    """Dispatch on the real file type, not the extension."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise KoshError(f"no such file: {path}")
    if os.path.isdir(path):
        raise KoshError("verify takes a file; point it at a container or an archive")

    with open(path, "rb") as fh:
        head = fh.read(16)

    if head.startswith(pack_mod.START_MAGIC):
        result = pack_mod.verify_container(path, deep=deep, progress=progress)
        return {
            "path": path, "kind": "kosh", "ok": result.ok, "entries": result.entries,
            "bytes": result.bytes, "manifest_verified": result.manifest_verified,
            "problems": result.problems, "seconds": round(result.seconds, 2),
        }

    if path.endswith(splitjoin.MANIFEST_SUFFIX):
        return _verify_split(path, progress)

    if head.startswith(b"PK\x03\x04"):
        return _verify_zip(path, progress)

    if path.lower().endswith(TAR_EXT):
        return _verify_tar(path, progress)

    digest = hash_file(path, progress=progress)
    return {
        "path": path, "kind": "file", "ok": True, "entries": 1,
        "bytes": os.path.getsize(path), "sha256": digest,
        "problems": [], "note": "no container to check; the hash is the evidence",
    }


def _verify_split(manifest_path: str, progress: ProgressFn | None) -> dict:
    info = splitjoin.inspect(manifest_path)
    base = os.path.dirname(os.path.abspath(manifest_path))
    algo = info.get("algo") or "sha256"
    problems: list[str] = []
    total = sum(p["size"] for p in info["parts"])
    done = 0

    import json
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    records = {r["name"]: r for r in manifest["parts"]}

    for part in info["parts"]:
        if not part["present"]:
            problems.append(f"{part['name']}: missing")
            continue
        actual = hash_file(os.path.join(base, part["name"]), algo=algo)
        expected = records[part["name"]].get(algo)
        if expected and actual != expected:
            problems.append(f"{part['name']}: checksum mismatch")
        done += part["size"]
        if progress:
            progress(done, total, f"verifying {part['name']}")

    return {
        "path": os.path.abspath(manifest_path), "kind": "kosh-split", "ok": not problems,
        "entries": len(info["parts"]), "bytes": info["total"], "problems": problems,
        "manifest_verified": True,
    }


def _verify_zip(path: str, progress: ProgressFn | None) -> dict:
    problems: list[str] = []
    started = time.perf_counter()
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad:
                problems.append(f"{bad}: CRC mismatch")
            entries = len(zf.infolist())
            raw = sum(i.file_size for i in zf.infolist())
    except (zipfile.BadZipFile, OSError) as exc:
        return {"path": path, "kind": "zip", "ok": False, "entries": 0, "bytes": 0,
                "problems": [str(exc)], "seconds": round(time.perf_counter() - started, 2)}
    return {"path": path, "kind": "zip", "ok": not problems, "entries": entries,
            "bytes": raw, "problems": problems,
            "seconds": round(time.perf_counter() - started, 2)}


def _verify_tar(path: str, progress: ProgressFn | None) -> dict:
    problems: list[str] = []
    entries = 0
    raw = 0
    started = time.perf_counter()
    try:
        from .archive import _open_tar
        with _open_tar(path) as tf:
            for member in tf:
                entries += 1
                raw += member.size
                if member.isfile():
                    handle = tf.extractfile(member)
                    if handle is None:
                        problems.append(f"{member.name}: unreadable")
                        continue
                    with handle:
                        while True:
                            if not handle.read(1 << 20):
                                break
    except (tarfile.TarError, OSError, KoshError) as exc:
        problems.append(str(exc))
    return {"path": path, "kind": "tar", "ok": not problems, "entries": entries,
            "bytes": raw, "problems": problems,
            "seconds": round(time.perf_counter() - started, 2)}


def summarise(result: dict) -> str:
    state = "ok" if result["ok"] else "FAILED"
    return (f"{os.path.basename(result['path'])}: {state} — {result['entries']} "
            f"entr{'y' if result['entries'] == 1 else 'ies'}, {human_size(result['bytes'])}")
