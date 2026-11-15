"""Integrity checking that never writes anything.

`verify` answers one question — is this file exactly what it claims to be — for
every artefact Kosh produces and every archive it understands.  It is the
command you run before deleting the original.
"""

from __future__ import annotations

import hashlib
import os
import struct
import tarfile
import time
import zipfile

from . import engine as engine_mod
from . import pack as pack_mod
from . import splitjoin
from .util import KoshError, ProgressFn, hash_file, human_size

TAR_EXT = (".tar", ".tar.gz", ".tgz", ".tar.zst", ".tzst", ".tar.xz")

# ZIP64 is not a different format to read: it is the same records with 64-bit
# fields, used when a count or an offset no longer fits in the 32-bit ones. A
# reader that only knows the classic end-of-central-directory sees a count of
# 0xFFFF and reports 65 535 entries for an archive holding any number above
# that. `zipfile` reads it correctly, so the job here is to make sure Kosh asks
# it to, and then says which archive it just read.
ZIP64_EOCD = b"PK\x06\x06"
ZIP64_LOCATOR = b"PK\x06\x07"
ZIP64_EXTRA_ID = 0x0001
# The classic EOCD is 22 bytes plus a comment of at most 65 535; the ZIP64 EOCD
# and its locator sit immediately before it, so the tail is where both live.
ZIP_EOCD_WINDOW = 22 + 0xFFFF + 64


def verify(path: str, deep: bool = True, progress: ProgressFn | None = None,
           jobs: int | None = None) -> dict:
    """Dispatch on the real file type, not the extension."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise KoshError(f"no such file: {path}")
    if os.path.isdir(path):
        raise KoshError("verify takes a file; point it at a container or an archive")

    with open(path, "rb") as fh:
        head = fh.read(64)

    if head.startswith(pack_mod.START_MAGIC):
        result = pack_mod.verify_container(path, deep=deep, progress=progress, jobs=jobs)
        return {
            "path": path, "kind": "kosh", "ok": result.ok, "entries": result.entries,
            "bytes": result.bytes, "manifest_verified": result.manifest_verified,
            "problems": result.problems, "seconds": round(result.seconds, 2),
        }

    if head.startswith(engine_mod.CHCE_MAGIC):
        return _verify_chce(path, head)

    # A file whose footer says KOSH but whose header does not is a damaged
    # container, not an arbitrary file: reporting it as "intact (hash only)" tells a
    # user their backup is fine when the container around it has been rewritten.
    if not head.startswith(pack_mod.START_MAGIC) and os.path.getsize(path) >= len(pack_mod.END_MAGIC) + 4:
        with open(path, "rb") as fh:
            fh.seek(-len(pack_mod.END_MAGIC), os.SEEK_END)
            tail = fh.read(len(pack_mod.END_MAGIC))
        if tail == pack_mod.END_MAGIC:
            return {
                "path": path, "kind": "kosh", "ok": False, "entries": 0,
                "bytes": os.path.getsize(path), "problems": [
                    "the footer says KOSH but the header does not: the container was damaged or truncated"
                ], "checked": "header only",
            }

    # A `.kosh` that does not parse is a damaged container, not an unknown file.
    # The extension is evidence here: Kosh writes `.kosh` and nothing else does, so
    # "intact (hash only)" on one is the reassurance a truncated backup must not get.
    if path.lower().endswith(".kosh"):
        return {
            "path": path, "kind": "kosh", "ok": False, "entries": 0,
            "bytes": os.path.getsize(path), "problems": [
                "a .kosh file that does not parse as a container: truncated or damaged"
            ], "checked": "header only",
        }

    if path.endswith(splitjoin.MANIFEST_SUFFIX):
        return _verify_split(path, progress)

    if head.startswith(b"PK\x03\x04"):
        return _verify_zip(path, progress, deep=deep)

    if path.lower().endswith(TAR_EXT):
        return _verify_tar(path, progress)

    digest = hash_file(path, progress=progress)
    return {
        "path": path, "kind": "file", "ok": True, "entries": 1,
        "bytes": os.path.getsize(path), "sha256": digest,
        "problems": [], "checked": "hash only",
        "note": "no container to check; the hash is the evidence",
    }


def _verify_chce(path: str, head: bytes) -> dict:
    """A CHCE stream is the engine's file, not a Kosh container.

    `pack --engine ziptastic` writes one, so this is a file Kosh produced. It used
    to fall through to the generic branch and report "intact" — which was a hash
    with nothing to compare it against, and read as if the stream had been
    checked. The block digests are only verified by decoding, which needs the
    engine; `unpack --engine` (roadmap EN-2) does not exist yet.
    """
    try:
        header = engine_mod.read_chce_header(head)
    except KoshError as exc:
        return {
            "path": path, "kind": "chce-stream", "ok": False, "entries": 0,
            "bytes": os.path.getsize(path), "problems": [str(exc)],
            "checked": "header only",
        }

    installed = any(e.name == "ziptastic" and e.available for e in engine_mod.find_engines())
    note = (
        "block digests are verified by decoding: run `ziptastic d` (the engine is on PATH)"
        if installed else
        "install ziptastic to decode and verify this stream"
    )
    return {
        "path": path, "kind": "chce-stream", "ok": True,
        "entries": header["block_count"], "bytes": os.path.getsize(path),
        "problems": [], "checked": "header only", "note": note,
        "chce": {"version": header["version"], "flags": header["flags"],
                 "original_size": header["original_size"]},
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


def _zip64_end_records(path: str) -> bool:
    """True when the archive ends with a ZIP64 end-of-central-directory record.

    This is the marker a >65 535-entry or >4 GB archive must carry, and the one a
    reader that stops at the classic EOCD never looks for. The search is confined
    to the tail because that is the only place the pair can be: a stored entry's
    compressed bytes can contain any four bytes at all, including this signature.
    """
    size = os.path.getsize(path)
    window = min(size, ZIP_EOCD_WINDOW)
    with open(path, "rb") as fh:
        fh.seek(size - window)
        tail = fh.read(window)
    return ZIP64_EOCD in tail or ZIP64_LOCATOR in tail


def _zip64_extra(info: zipfile.ZipInfo) -> bool:
    """True when one central-directory entry carries a ZIP64 extra field (0x0001).

    `zipfile` writes one for a member that asks for 64-bit fields even when the
    archive as a whole is small, so this catches the per-entry form that has no
    end record.
    """
    extra = info.extra
    pos = 0
    while pos + 4 <= len(extra):
        header_id, length = struct.unpack("<HH", extra[pos:pos + 4])
        if header_id == ZIP64_EXTRA_ID:
            return True
        pos += 4 + length
    return False


def _verify_zip(path: str, progress: ProgressFn | None, deep: bool = True) -> dict:
    problems: list[str] = []
    started = time.perf_counter()
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            entries = len(infos)
            raw = sum(i.file_size for i in infos)
            # The count and the sizes above come from the central directory, which
            # `zipfile` reads through the ZIP64 records when they are there — so an
            # archive of 70 000 entries reports 70 000, not the 0xFFFF the classic
            # end record holds. Say so, rather than leaving the reader to guess
            # which of the two was read.
            zip64 = _zip64_end_records(path) or any(_zip64_extra(i) for i in infos)
            if deep:
                bad = zf.testzip()
                if bad:
                    problems.append(f"{bad}: CRC mismatch")
    except (zipfile.BadZipFile, OSError) as exc:
        return {"path": path, "kind": "zip", "ok": False, "entries": 0, "bytes": 0,
                "problems": [str(exc)], "seconds": round(time.perf_counter() - started, 2)}
    return {"path": path, "kind": "zip", "ok": not problems, "entries": entries,
            "bytes": raw, "problems": problems, "zip64": zip64,
            # `--quick` has always been documented as "structure only; do not
            # decompress", and this path decompressed everything anyway. On a
            # 65 536-entry archive that is the difference between a second and ten.
            "checked": "every entry decompressed" if deep else "central directory only",
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
