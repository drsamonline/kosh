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
    """Parse just enough of a CHCE stream to describe it.

    The layout is version-dependent and this follows ziptastic's
    `docs/FORMAT.md` §"Stream layout", which is normative:

        v8: "CHCE" | version u8 | flags u8 | layers u8 | layers2 u8 | layers3 u8
            | original_size varint | true_size varint | block_count varint
            | crc32 u32

    The fields arrived over time — crc32 in v2, `layers` in v3, `layers2` in v5,
    `true_size` in v6, `layers3` in v7 — so reading a v8 stream as if it were v1
    is not a small error: the three layer bytes land where `original_size` is
    expected, every field after them shifts, and the result reads 0 blocks for a
    one-block stream. That is what this used to do.
    """
    if not head.startswith(CHCE_MAGIC):
        raise KoshError("not a CHCE stream")
    if len(head) < 6:
        raise KoshError("CHCE stream is truncated")
    version = head[4]
    flags = head[5]
    pos = 6

    layer_bytes = 3 if version >= 7 else 2 if version >= 5 else 1 if version >= 3 else 0
    if len(head) < pos + layer_bytes:
        raise KoshError("CHCE header ends before the layer bytes")
    layers = list(head[pos:pos + layer_bytes])
    pos += layer_bytes

    original_size, pos = _read_varint(head, pos)
    true_size = original_size
    if version >= 6:
        true_size, pos = _read_varint(head, pos)
    block_count, pos = _read_varint(head, pos)
    crc32 = None
    if version >= 2 and len(head) >= pos + 4:
        (crc32,) = struct.unpack("<I", head[pos:pos + 4])
        pos += 4

    return {
        "kind": "chce-stream", "version": version, "flags": flags,
        "layers": " ".join(f"0x{b:02x}" for b in layers) or "none",
        "original_size": original_size, "true_size": true_size,
        "block_count": block_count, "crc32": f"0x{crc32:08x}" if crc32 is not None else "none",
        "decoder": "ziptastic",
    }


def read_chce_trailer(path: str) -> dict:
    """The v8 trailer: block offsets, and the digest that binds them.

    ziptastic's docs/FORMAT.md, "Version 8: the block offset table":

        "ZTIX" | block_count varint | sha256(manifest)[32]
              | block_offset[0..count-1] as u64 little-endian | "ZTOX"

    The trailer is a fixed size for a given block count, so a reader that has the
    header can compute where it starts instead of walking the stream — which is the
    entire point of the table. Every offset is absolute, counted from the `C` of
    `CHCE`.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(64)
        header = read_chce_header(head)
        count = header["block_count"]
        if header["version"] < 8:
            raise KoshError(
                f"CHCE format v{header['version']} has no block table; v8 added it"
            )

        # "ZTIX" + varint(count) + 32-byte digest + 8*count + "ZTOX"
        tail_bytes = 4 + len(_varint_bytes(count)) + 32 + 8 * count + 4
        if size < tail_bytes:
            raise KoshError("CHCE stream is shorter than its own trailer")
        fh.seek(size - tail_bytes)
        trailer = fh.read(tail_bytes)

    if not trailer.startswith(b"ZTIX"):
        raise KoshError("CHCE trailer does not start with ZTIX (truncated or not v8)")
    if not trailer.endswith(b"ZTOX"):
        raise KoshError("CHCE trailer does not end with ZTOX (truncated inside the table)")

    pos = 4
    found_count, pos = _read_varint(trailer, pos)
    if found_count != count:
        raise KoshError(
            f"trailer says {found_count} blocks, header says {count}"
        )
    manifest = trailer[pos:pos + 32]
    pos += 32

    offsets = list(struct.unpack(f"<{count}Q", trailer[pos:pos + 8 * count]))
    for earlier, later in zip(offsets, offsets[1:]):
        if later <= earlier:
            raise KoshError("block offsets are not strictly increasing")

    return {
        "count": count,
        "manifest_sha256": manifest.hex(),
        "offsets": offsets,
        "trailer_bytes": tail_bytes,
    }


def read_blocks(path: str, trailer: dict | None = None) -> list[dict]:
    """Each block's offset, mode byte and sizes, read from its own header.

        mode u8 | uncompressed_size varint | compressed_size varint | sha256[32] | ...

    The metadata that follows depends on the mode and is not parsed here: the
    question `kosh info` answers is where the blocks are and how big they are.
    """
    if trailer is None:
        trailer = read_chce_trailer(path)
    blocks = []
    with open(path, "rb") as fh:
        for index, offset in enumerate(trailer["offsets"]):
            fh.seek(offset)
            head = fh.read(64)
            if not head:
                raise KoshError(f"block {index} is past the end of the file")
            mode = head[0]
            uncompressed, pos = _read_varint(head, 1)
            compressed, pos = _read_varint(head, pos)
            blocks.append(
                {
                    "index": index,
                    "offset": offset,
                    "mode": mode,
                    "uncompressed_size": uncompressed,
                    "compressed_size": compressed,
                    "digest": head[pos:pos + 32].hex(),
                }
            )
    return blocks


def _varint_bytes(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


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


def unpack_with_engine(engine: str, stream: str, dest: str,
                       progress: ProgressFn | None = None) -> dict:
    """Decompress a CHCE stream by spawning the engine that wrote it.

    The mirror of `pack_with_engine`, and the other half of the boundary: before
    this, `kosh pack --engine ziptastic` produced a file that `kosh unpack`
    refused as "not a Kosh container", so the workbench could write what it could
    not read. The engine is spawned, never imported.
    """
    if engine not in ENGINES:
        raise KoshError(f"unknown engine {engine!r} (known: {', '.join(ENGINES)})")
    binary, _verb, suffix = ENGINES[engine]

    # Look at the file before looking for the binary: telling someone to install
    # an engine when the file is not a stream at all sends them the wrong way.
    source = os.path.abspath(stream)
    if not os.path.isfile(source):
        raise KoshError(f"no such file: {stream}")
    with open(source, "rb") as fh:
        head = fh.read(64)
    if not head.startswith(CHCE_MAGIC):
        raise KoshError(f"{os.path.basename(stream)} is not a CHCE stream; unpack it as a container")
    header = read_chce_header(head)

    exe = shutil.which(binary)
    if not exe:
        raise KoshError(
            f"{binary} is not on PATH. Build it from the CHCE family "
            f"(github.com/drsamonline/{engine}), or unpack the stream with "
            f"`{binary} d` yourself."
        )

    if progress:
        progress(0, 1, f"running {binary} d")

    with tempfile.TemporaryDirectory(prefix="kosh-engine-") as work:
        # The engine writes beside its input and names it after it, so stage the
        # stream in an empty directory and take whatever appears that is not it.
        staged = os.path.join(work, os.path.basename(source))
        shutil.copy2(source, staged)
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, resolved binary
                [exe, "d", staged], cwd=work, capture_output=True, text=True, timeout=6 * 3600,
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
                candidate = os.path.join(root, name)
                if os.path.abspath(candidate) != os.path.abspath(staged):
                    produced = candidate
                    break
            if produced:
                break
        if not produced:
            raise KoshError(f"{binary} reported success but wrote no output")

        # A CHCE stream carries no filename, so the engine names its output after
        # the input (payload.dr → payload, anything else → <name>.out). If the
        # caller gave a path that is not an existing directory, that path is the
        # name they want.
        if os.path.isdir(dest) or dest in ("", "."):
            os.makedirs(os.path.abspath(dest) or ".", exist_ok=True)
            target = os.path.join(os.path.abspath(dest), os.path.basename(produced))
        else:
            target = os.path.abspath(dest)
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        shutil.move(produced, target)

    size = os.path.getsize(target)
    if size != header["true_size"]:
        raise KoshError(
            f"decoded size {size} does not match the stream header's true_size "
            f"{header['true_size']}; the stream is damaged"
        )

    if progress:
        progress(1, 1, "done")
    return {
        "engine": engine, "output": target, "bytes": size,
        "true_size": header["true_size"], "block_count": header["block_count"],
        "binary": exe, "stdout": (completed.stdout or "").strip()[:2000],
    }
