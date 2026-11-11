"""Interop with the rest of the CHCE family.

Kosh is the workbench, not the engine: it does not implement BWT/rANS/PPMd and
it must not try to.  Where a CHCE engine is installed, Kosh drives it across a
process boundary — the same rule heavenzip follows, which is what keeps GPL
tools usable without linking them.

It also recognises a CHCE bitstream (magic ``CHCE``) so `kosh scan` can say what
a ``.dr`` file is instead of calling it "unknown".
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass

from .util import KoshError, ProgressFn, human_size

CHCE_MAGIC = b"CHCE"

# engine name -> (binary, compress verb, output suffix)
ENGINES: dict[str, tuple[str, str, str]] = {
    "ziptastic": ("ziptastic", "c", ".dr"),
    "heavenzip": ("heavenzip", "compress", ".hzpkg"),
}


@dataclass
class EngineInfo:
    name: str
    binary: str
    path: str | None

    @property
    def available(self) -> bool:
        return self.path is not None


def find_engines() -> list[EngineInfo]:
    out = []
    for name, (binary, _verb, _suffix) in ENGINES.items():
        out.append(EngineInfo(name=name, binary=binary, path=shutil.which(binary)))
    return out


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while pos < len(buf):
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            break
    raise KoshError("CHCE header ends mid-varint")


def read_chce_header(head: bytes) -> dict:
    """Parse just enough of a CHCE stream to describe it (docs/FORMAT.md)."""
    if not head.startswith(CHCE_MAGIC):
        raise KoshError("not a CHCE stream")
    if len(head) < 6:
        raise KoshError("CHCE stream is truncated")
    version = head[4]
    flags = head[5]
    pos = 6
    original_size, pos = _read_varint(head, pos)
    block_count, pos = _read_varint(head, pos)
    return {
        "kind": "chce-stream", "version": version, "flags": flags,
        "original_size": original_size, "block_count": block_count,
        "decoder": "ziptastic",
    }


def pack_with_engine(engine: str, inputs: list[str], out: str,
                     progress: ProgressFn | None = None) -> dict:
    """Compress with an external CHCE engine, then move its output into place.

    The engine is spawned, never imported: no linking, no licence entanglement.
    """
    if engine not in ENGINES:
        raise KoshError(f"unknown engine {engine!r} (known: {', '.join(ENGINES)})")
    binary, verb, suffix = ENGINES[engine]
    exe = shutil.which(binary)
    if not exe:
        raise KoshError(
            f"{binary} is not on PATH. Build it from the CHCE family "
            f"(github.com/drsamonline/{engine}) or use --engine native."
        )
    if len(inputs) != 1:
        raise KoshError(f"{engine} takes one input at a time; pack the folder instead")

    source = os.path.abspath(inputs[0])
    if progress:
        progress(0, 1, f"running {binary}")

    with tempfile.TemporaryDirectory(prefix="kosh-engine-") as work:
        staged = os.path.join(work, os.path.basename(source))
        if os.path.isdir(source):
            shutil.copytree(source, staged)
        else:
            shutil.copy2(source, staged)
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, resolved binary
                [exe, verb, staged], cwd=work, capture_output=True, text=True, timeout=6 * 3600,
            )
        except OSError as exc:
            raise KoshError(f"could not run {binary}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise KoshError(f"{binary} did not finish within six hours") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            raise KoshError(f"{binary} failed: {detail[-1] if detail else 'no output'}")

        produced = None
        for root, _dirs, files in os.walk(work):
            for name in files:
                if name.endswith(suffix):
                    produced = os.path.join(root, name)
                    break
            if produced:
                break
        if not produced:
            raise KoshError(f"{binary} reported success but wrote no {suffix} file")
        os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
        shutil.move(produced, out)

    if progress:
        progress(1, 1, "done")
    return {
        "engine": engine, "output": os.path.abspath(out),
        "stored_size": os.path.getsize(out),
        "raw_size": os.path.getsize(source) if os.path.isfile(source) else None,
        "binary": exe,
        "stdout": (completed.stdout or "").strip()[:2000],
    }
