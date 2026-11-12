"""Features added for the CHCE family: verify, manifest digest, doctor, bench, engines."""

from __future__ import annotations

import json
import os
import stat
import struct
import subprocess
import sys

import pytest

from kosh.core import bench, doctor, engine, pack, verify
from kosh.core.util import KoshError


@pytest.fixture()
def container(tmp_path):
    src = tmp_path / "notes.txt"
    src.write_bytes(b"a verifiable payload\n" * 4000)
    out = tmp_path / "c.kosh"
    pack.pack([str(src)], str(out), codec="zstd", level=6)
    return out


def test_container_is_version_two_with_manifest_digest(container):
    toc = pack.read_toc(str(container))
    assert toc["version"] == 2
    assert toc["manifest_sha256"] == pack.manifest_digest(toc)


def test_tampered_manifest_is_rejected(container, tmp_path):
    data = bytearray(container.read_bytes())
    # Flip a byte inside the JSON table of contents, not the payload.
    marker = data.find(b'"notes.txt"')
    assert marker > 0
    data[marker + 1] = ord("N")
    container.write_bytes(bytes(data))
    with pytest.raises(KoshError, match="manifest digest"):
        pack.read_toc(str(container))


def test_verify_container_ok(container):
    result = pack.verify_container(str(container))
    assert result.ok and result.manifest_verified and result.entries == 1


def test_verify_container_detects_payload_damage(container):
    data = bytearray(container.read_bytes())
    data[-40] ^= 0xFF  # inside the last block, before the TOC
    container.write_bytes(bytes(data))
    result = pack.verify_container(str(container))
    assert not result.ok


def test_verify_dispatch_and_quick_mode(container):
    result = verify.verify(str(container), deep=False)
    assert result["kind"] == "kosh" and result["ok"]


def test_verify_split_set(tmp_path):
    from kosh.core import splitjoin
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(300_000))
    result = splitjoin.split(str(src), str(tmp_path / "parts"), parts=3)
    good = verify.verify(result.manifest)
    assert good["kind"] == "kosh-split" and good["ok"]
    with open(result.parts[0], "r+b") as fh:
        fh.write(b"corrupt")
    bad = verify.verify(result.manifest)
    assert not bad["ok"] and any("checksum" in p for p in bad["problems"])


def test_verify_zip(tmp_path):
    import zipfile
    path = tmp_path / "a.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("a.txt", "hello")
    assert verify.verify(str(path))["ok"]


def test_doctor_reports_a_real_round_trip():
    report = doctor.run(quick=True)
    names = {c["name"]: c for c in report["checks"]}
    assert report["ok"]
    assert names["round-trip"]["ok"]
    assert "verified" in names["round-trip"]["detail"]


def test_bench_is_deterministic_and_has_a_baseline(tmp_path):
    first = bench.run()
    second = bench.run()
    sizes = lambda r: [[row["stored"] for row in item["rows"]] for item in r["results"]]
    assert sizes(first) == sizes(second)
    codecs = {row["codec"] for item in first["results"] for row in item["rows"]}
    assert {"gzip -9", "xz -9", "kosh zstd 19"} <= codecs
    assert "| file |" in bench.markdown(first)


def test_chce_header_is_parsed():
    def varint(value: int) -> bytes:
        out = bytearray()
        while True:
            byte = value & 0x7F
            value >>= 7
            out.append(byte | (0x80 if value else 0))
            if not value:
                return bytes(out)

    head = b"CHCE" + bytes([1, 0]) + varint(123456) + varint(7)
    parsed = engine.read_chce_header(head)
    assert parsed["version"] == 1 and parsed["original_size"] == 123456
    assert parsed["block_count"] == 7 and parsed["decoder"] == "ziptastic"


def _stand_in_engine(tmp_path):
    """A path that `shutil.which("ziptastic")` finds on this platform.

    On POSIX that is a script with a shebang. On Windows a shebang script is not
    executable at all: the file needs an extension from PATHEXT, so the script is
    `ziptastic.py` and the thing on PATH is `ziptastic.cmd` that calls it.
    """
    directory = tmp_path / "bin"
    directory.mkdir()
    if os.name == "nt":
        script = directory / "ziptastic.py"
        shim = directory / "ziptastic.cmd"
        shim.write_text(f'@"{sys.executable}" "%~dp0ziptastic.py" %*\r\n', encoding="utf-8")
        return script
    return directory / "ziptastic"


def test_engine_pack_spawns_a_binary(tmp_path, monkeypatch):
    """A stand-in engine proves the process boundary works without ziptastic installed.

    The stand-in is Python, not a shell script: a `#!/bin/sh` file is not runnable
    on Windows at all, and the boundary being tested — spawn, wait, move the
    output — has nothing to do with which language is on the other side.
    """
    fake = _stand_in_engine(tmp_path)
    fake.write_text(
        f"#!{sys.executable}\n"
        "import gzip, sys\n"
        "verb, src = sys.argv[1], sys.argv[2]\n"
        "if verb != 'c':\n"
        "    sys.exit(2)\n"
        "open(src + '.dr', 'wb').write(gzip.compress(open(src, 'rb').read(), 9))\n"
        "print('compressed')\n"
    )
    if os.name != "nt":
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    # os.pathsep, not ":": on Windows the separator is ";" and a ":" joined PATH is
    # one bogus entry, so shutil.which finds nothing.
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")

    src = tmp_path / "input.txt"
    src.write_bytes(b"engine boundary test\n" * 500)
    out = tmp_path / "input.dr"
    info = engine.pack_with_engine("ziptastic", [str(src)], str(out))
    assert out.exists() and out.stat().st_size > 0
    assert info["engine"] == "ziptastic"


def test_engine_missing_binary_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(KoshError, match="not on PATH"):
        engine.pack_with_engine("ziptastic", [str(tmp_path)], str(tmp_path / "x.dr"))


# --- the CHCE header, read the way ziptastic writes it ------------------------
#
# The first 20 bytes of a real v8 stream, produced by ziptastic 1.4.1:
#   magic | version 8 | flags 0x40 | layers 0,0,0 | original 643340 |
#   true 645120 | blocks 1 | crc32 0x46ac486d
REAL_V8_HEADER = bytes.fromhex("4348434508400000008ca22780b027016d48ac46")


def test_chce_header_is_read_with_the_layer_bytes_skipped():
    """A v8 header read as if it were v1 reports 0 blocks for a one-block stream."""
    header = engine.read_chce_header(REAL_V8_HEADER)
    assert header["version"] == 8
    assert header["flags"] == 0x40
    assert header["layers"] == "0x00 0x00 0x00"
    assert header["original_size"] == 643340
    assert header["true_size"] == 645120
    assert header["block_count"] == 1
    assert header["crc32"] == "0x46ac486d"


def test_chce_header_of_an_older_version_skips_only_its_own_bytes():
    # v3: one layer byte, no true_size, no layers2/3 — original 300, 2 blocks.
    v3 = bytes.fromhex("43484345") + bytes([3, 0, 0x02]) + bytes([0xAC, 0x02, 0x02])
    header = engine.read_chce_header(v3)
    assert header["layers"] == "0x02"
    assert header["original_size"] == 300
    assert header["true_size"] == 300
    assert header["block_count"] == 2


def test_verify_does_not_claim_a_checked_stream_for_a_chce_file(tmp_path):
    """`pack --engine ziptastic` writes a CHCE stream, not a container.

    verify used to fall through to the generic branch and report "intact" — a
    hash with nothing to compare against — while unpack/info/ls refused the file
    as "not a Kosh container". It must say what it actually checked.
    """
    stream = tmp_path / "payload.dr"
    stream.write_bytes(REAL_V8_HEADER + b"\x00" * 32)
    result = verify.verify(str(stream))
    assert result["kind"] == "chce-stream"
    assert result["checked"] == "header only"
    assert result["entries"] == 1
    assert "ziptastic" in result["note"]


def test_engine_unpack_is_the_mirror_of_engine_pack(tmp_path, monkeypatch):
    """Pack with a stand-in engine, unpack with it, get the bytes back.

    EN-2 in the roadmap: packing without unpacking is half a boundary. The
    stand-in writes a real 16-byte CHCE v8 header (one-byte varints) and gzips the
    payload, so the size check against true_size has something to check.
    """
    fake = _stand_in_engine(tmp_path)
    fake.write_text(
        f"#!{sys.executable}\n"
        "import gzip, sys\n"
        "verb, src = sys.argv[1], sys.argv[2]\n"
        "if verb == 'c':\n"
        "    data = open(src, 'rb').read()\n"
        "    head = b'CHCE' + bytes([8, 0x40, 0, 0, 0, len(data), len(data), 1]) + b'\\0' * 4\n"
        "    open(src + '.dr', 'wb').write(head + gzip.compress(data, 9))\n"
        "elif verb == 'd':\n"
        "    raw = open(src, 'rb').read()[16:]\n"
        "    out = src[:-3] if src.endswith('.dr') else src + '.out'\n"
        "    open(out, 'wb').write(gzip.decompress(raw))\n"
        "else:\n"
        "    sys.exit(2)\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    # os.pathsep, not ":": on Windows the separator is ";" and a ":" joined PATH is
    # one bogus entry, so shutil.which finds nothing.
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")

    src = tmp_path / "input.txt"
    src.write_bytes(b"engine boundary\n" * 5)   # 80 bytes
    stream = tmp_path / "input.dr"
    engine.pack_with_engine("ziptastic", [str(src)], str(stream))

    out = tmp_path / "restored"
    result = engine.unpack_with_engine("ziptastic", str(stream), str(out))
    assert out.read_bytes() == src.read_bytes()
    assert result["bytes"] == 80
    assert result["block_count"] == 1
    assert result["engine"] == "ziptastic"


def test_engine_unpack_refuses_a_file_that_is_not_a_stream(tmp_path):
    not_a_stream = tmp_path / "payload.bin"
    not_a_stream.write_bytes(b"just bytes, no magic")
    with pytest.raises(KoshError, match="not a CHCE stream"):
        engine.unpack_with_engine("ziptastic", str(not_a_stream), str(tmp_path / "out"))


def test_engine_unpack_without_the_engine_names_it(tmp_path, monkeypatch):
    stream = tmp_path / "x.dr"
    stream.write_bytes(REAL_V8_HEADER + b"\x00" * 16)
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(KoshError, match="not on PATH"):
        engine.unpack_with_engine("ziptastic", str(stream), str(tmp_path / "out"))


# --- EN-4: the v8 block table ------------------------------------------------

def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _stream(blocks, version=8):
    """A synthetic CHCE stream: header, blocks, and a v8 trailer."""
    payload = bytearray()
    for _ in range(blocks):
        payload += bytes([0x05]) + _varint(1000) + _varint(400) + b"\x11" * 32 + b"p" * 400
    header = b"CHCE" + bytes([version, 0x40, 0, 0, 0]) + _varint(1000 * blocks) + _varint(1000 * blocks) + _varint(blocks) + b"\x00" * 4
    # v7 and below have no table, so the stream simply ends.
    if version < 8:
        return header + bytes(payload)

    body = header
    offsets = []
    for _ in range(blocks):
        offsets.append(len(body))
        body += bytes([0x05]) + _varint(1000) + _varint(400) + b"\x11" * 32 + b"p" * 400
    trailer = b"ZTIX" + _varint(blocks) + b"\x22" * 32
    trailer += b"".join(offset.to_bytes(8, "little") for offset in offsets)
    trailer += b"ZTOX"
    return body + trailer


def test_block_table_is_read(tmp_path):
    path = tmp_path / "two.dr"
    path.write_bytes(_stream(2))
    trailer = engine.read_chce_trailer(str(path))
    assert trailer["count"] == 2
    assert len(trailer["offsets"]) == 2
    assert trailer["offsets"][0] < trailer["offsets"][1]
    assert trailer["trailer_bytes"] == 4 + 1 + 32 + 16 + 4

    blocks = engine.read_blocks(str(path), trailer)
    assert [b["index"] for b in blocks] == [0, 1]
    assert blocks[0]["offset"] == trailer["offsets"][0]
    assert blocks[0]["uncompressed_size"] == 1000
    assert blocks[0]["compressed_size"] == 400
    assert blocks[0]["mode"] == 0x05
    assert blocks[0]["digest"] == "11" * 32


def test_a_v7_stream_says_it_has_no_table(tmp_path):
    path = tmp_path / "old.dr"
    path.write_bytes(_stream(1, version=7))
    with pytest.raises(KoshError, match="no block table"):
        engine.read_chce_trailer(str(path))


def test_a_truncated_trailer_is_refused(tmp_path):
    data = _stream(2)
    path = tmp_path / "cut.dr"
    path.write_bytes(data[:-2])  # lose part of ZTOX
    # Cutting bytes moves where the trailer is expected to start, so the reader may
    # fail on ZTIX rather than ZTOX — either way it refuses instead of guessing.
    with pytest.raises(KoshError, match="ZTIX|ZTOX|shorter|truncated"):
        engine.read_chce_trailer(str(path))


def test_a_trailer_that_disagrees_with_the_header_is_refused(tmp_path):
    # Rewrite the trailer's count so it no longer matches the header's.
    data = bytearray(_stream(2))
    index = data.rfind(b"ZTIX")
    data[index + 4] = 3
    path = tmp_path / "lying.dr"
    path.write_bytes(bytes(data))
    with pytest.raises(KoshError, match="header says"):
        engine.read_chce_trailer(str(path))


# --- CT-1: the console encoding Windows actually uses -------------------------

def test_doctor_survives_a_legacy_console_codepage():
    """Windows consoles default to a legacy codepage; the UI is UTF-8.

    `kosh doctor` died on windows-latest with
    `UnicodeEncodeError: 'charmap' codec can't encode character '\\u2713'`.
    Reproducing it here means it cannot come back unnoticed: run the real CLI with
    PYTHONIOENCODING set to cp1252 and require a clean exit.
    """
    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    completed = subprocess.run(
        [sys.executable, "-m", "kosh.cli", "doctor"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=120,
    )
    assert "UnicodeEncodeError" not in (completed.stderr or ""), completed.stderr
    assert completed.returncode == 0, completed.stderr or completed.stdout


# --- EN-6: the engines command and the version probe -------------------------

def test_probe_version_is_none_without_the_engine(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert engine.probe_version("ziptastic") is None
    assert engine.probe_version("no-such-engine") is None


def test_probe_version_reads_the_engines_own_answer(tmp_path, monkeypatch):
    """Presence is half the answer; the version is the half that matters."""
    fake = _stand_in_engine(tmp_path)
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if sys.argv[1] != 'version':\n"
        "    sys.exit(2)\n"
        "print('ziptastic 9.9.9 (CHCE format v8)')\n"
    )
    if os.name != "nt":
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    assert engine.probe_version("ziptastic") == "ziptastic 9.9.9 (CHCE format v8)"


def test_probe_version_survives_an_engine_that_answers_nothing(tmp_path, monkeypatch):
    fake = _stand_in_engine(tmp_path)
    fake.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(3)\n")
    if os.name != "nt":
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    assert engine.probe_version("ziptastic") is None


def test_engines_command_lists_every_known_engine():
    from typer.testing import CliRunner

    from kosh.cli import app

    result = CliRunner().invoke(app, ["engines", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    names = {row["engine"] for row in rows}
    assert names == set(engine.ENGINES)
    for row in rows:
        assert set(row) >= {"engine", "available", "path", "version", "suffix"}
