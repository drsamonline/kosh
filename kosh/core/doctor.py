"""Environment self-check with a live round-trip.

`doctor` exists because a compression tool that silently falls back to a weaker
codec looks exactly like one that is working.  Every check here does something
and reports what actually happened.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

from . import pack as pack_mod
from . import engine as engine_mod
from .engine import find_engines
from .util import KoshError

MIN_PYTHON = (3, 10)


def _check(name: str, ok: bool, detail: str, fatal: bool = False) -> dict:
    return {"name": name, "ok": ok, "detail": detail, "fatal": fatal}


def run(quick: bool = False) -> dict:
    checks: list[dict] = []

    version = sys.version_info
    checks.append(_check(
        "python", version >= MIN_PYTHON,
        f"{version.major}.{version.minor}.{version.micro} (needs ≥ "
        f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]})", fatal=True,
    ))

    for module, label in (("zstandard", "zstd"), ("brotli", "brotli"),
                          ("lzma", "lzma"), ("zopfli.zlib", "zopfli"),
                          ("numpy", "numpy")):
        try:
            __import__(module)
            checks.append(_check(label, True, "imports"))
        except ImportError as exc:
            checks.append(_check(label, False, f"missing: {exc}", fatal=label in ("zstd", "zopfli")))

    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        checks.append(_check("web-ui", True, "fastapi + uvicorn present"))
    except ImportError as exc:
        checks.append(_check("web-ui", False, f"missing: {exc} (CLI still works)"))

    # Write a real file through the real codec, then read it back.
    try:
        with tempfile.TemporaryDirectory(prefix="kosh-doctor-") as tmp:
            source = os.path.join(tmp, "probe.txt")
            with open(source, "wb") as fh:
                fh.write(b"kosh round-trip probe\n" * 512)
            container = os.path.join(tmp, "probe.kosh")
            packed = pack_mod.pack([source], container, codec="zstd", level=3)
            extracted = pack_mod.unpack(container, os.path.join(tmp, "out"))
            with open(os.path.join(tmp, "out", "probe.txt"), "rb") as fh:
                same = fh.read() == open(source, "rb").read()
            checks.append(_check(
                "round-trip", same and extracted.verified,
                f"{packed.raw_size} B → {packed.stored_size} B, verified={extracted.verified}",
                fatal=True,
            ))
            writable = os.access(tmp, os.W_OK)
            checks.append(_check("temp-space", writable,
                                 f"{shutil.disk_usage(tmp).free // (1024**3)} GiB free"))
    except (KoshError, OSError) as exc:
        checks.append(_check("round-trip", False, str(exc), fatal=True))

    if not quick:
        try:
            import zopfli.zlib
            payload = b"compress me, i am repetitive. " * 200
            stream = zopfli.zlib.compress(payload, numiterations=2)
            import zlib
            checks.append(_check("zopfli", zlib.decompress(stream) == payload,
                                 f"{len(payload)} B → {len(stream)} B"))
        except Exception as exc:  # noqa: BLE001
            checks.append(_check("zopfli", False, str(exc)))

    engines = find_engines()
    for engine in engines:
        # Presence is half the answer; the version is the other half, and it is the
        # half that decides whether a `.dr` from this machine will open on another.
        detail = "not on PATH (optional — native codecs are used)"
        if engine.available:
            version = engine_mod.probe_version(engine.name)
            detail = f"{version or 'no version output'} ({engine.path})"
        checks.append(_check(f"engine:{engine.name}", engine.available, detail))

    fatal_failure = any(not c["ok"] and c["fatal"] for c in checks)
    return {
        "ok": not fatal_failure,
        "checks": checks,
        "seconds": None,
        "platform": f"{sys.platform} · {os.cpu_count()} cores",
    }
