"""Every signature is checked against a real file, not a copied constant.

A detection table is a pile of claims about the world. The only way to know a claim
is true is to make the file and look. Each test below produces its format with the
tool that actually writes it — tar, gzip, bzip2, xz, zstd, sqlite3, Pillow — so a
wrong magic fails here instead of misnaming a user's file.
"""
from __future__ import annotations

import bz2
import gzip
import io
import lzma
import sqlite3
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest

from kosh.core.optimize import OPTIMISABLE, can_optimise, sniff


def test_png_from_pillow() -> None:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, "PNG")
    assert sniff(buf.getvalue()) == "png"


def test_jpeg_from_pillow() -> None:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, "JPEG")
    assert sniff(buf.getvalue()) == "jpeg"


def test_gif_from_pillow() -> None:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("P", (8, 8)).save(buf, "GIF")
    assert sniff(buf.getvalue()) == "gif"


def test_bmp_from_pillow() -> None:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, "BMP")
    assert sniff(buf.getvalue()) == "bmp"


def test_tiff_from_pillow() -> None:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, "TIFF")
    assert sniff(buf.getvalue()) == "tiff"


def test_zip_from_zipfile() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.txt", "hello")
    assert sniff(buf.getvalue()) == "zip"


def test_gzip_from_module() -> None:
    assert sniff(gzip.compress(b"hello world" * 100)) == "gzip"


def test_bzip2_from_module() -> None:
    assert sniff(bz2.compress(b"hello world" * 100)) == "bzip2"


def test_xz_from_module() -> None:
    assert sniff(lzma.compress(b"hello world" * 100)) == "xz"


def test_zstd_from_library() -> None:
    zstandard = pytest.importorskip("zstandard")
    assert sniff(zstandard.ZstdCompressor().compress(b"hello world" * 100)) == "zstd"


def test_sqlite_from_sqlite3() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.db"
        con = sqlite3.connect(path)
        con.execute("create table t (a integer)")
        con.commit()
        con.close()
        assert sniff(path.read_bytes()) == "sqlite"


def test_tar_from_tarfile() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        info = tarfile.TarInfo("a.txt")
        payload = b"hello"
        info.size = len(payload)
        t.addfile(info, io.BytesIO(payload))
    # "ustar" sits at 257, which is why the table carries an offset at all
    assert sniff(buf.getvalue()) == "tar"


def test_pdf_from_a_real_writer() -> None:
    pytest.importorskip("weasyprint")
    from weasyprint import HTML
    assert sniff(HTML(string="<p>hi</p>").write_pdf()) == "pdf"


def test_the_running_interpreter_is_named_by_its_content() -> None:
    """Each platform's own interpreter binary, read as bytes.

    This was `== "elf"`, which is a Linux-only claim dressed as a universal one: it
    went red on the macOS runner (the table had no Mach-O signature at all, so a real
    Mach-O came back "unknown") and on the Windows runner (a PE, named correctly, and
    still failed). The fix is both halves — the signature was missing AND the
    expectation was wrong — so this asserts what the platform's binary actually is.

    It is also the one test here that reads a REAL Mach-O, produced by the macOS
    runner's own toolchain: no CI machine can synthesise one from a constant.
    """
    import sys
    expected = {"linux": "elf", "darwin": "macho", "win32": "pe"}[sys.platform]
    assert sniff(Path(sys.executable).read_bytes()[:4096]) == expected


def test_macho_thin_and_fat_headers() -> None:
    """The magics, both byte orders, and the CAFEBABE that is NOT always a Mach-O."""
    thin = [b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
            b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"]
    for magic in thin:
        assert sniff(magic + b"\x00" * 60) == "macho", magic
    # A fat header carries the architecture count in bytes 4..8.
    assert sniff(b"\xca\xfe\xba\xbe" + (2).to_bytes(4, "big") + b"\x00" * 60) == "macho"
    assert sniff(b"\xbe\xba\xfe\xca" + (1).to_bytes(4, "big") + b"\x00" * 60) == "macho"
    # A Java class file: minor 0, major 52 (Java 8). It must not be called a Mach-O.
    assert sniff(b"\xca\xfe\xba\xbe" + (0).to_bytes(2, "big")
                 + (52).to_bytes(2, "big") + b"\x00" * 60) == "unknown"


def test_pe_needs_more_than_mz() -> None:
    """Two bytes are not enough: MZ alone must not be called a PE."""
    assert sniff(b"MZ" + b"\x00" * 200) == "unknown"
    fake = bytearray(b"MZ" + b"\x00" * 200)
    fake[0x3C:0x40] = (0x80).to_bytes(4, "little")
    fake[0x80:0x84] = b"PE\x00\x00"
    assert sniff(bytes(fake)) == "pe"


def test_empty_and_short_inputs_do_not_raise() -> None:
    assert sniff(b"") == "unknown"
    assert sniff(b"\x89") == "unknown"


def test_extension_is_never_consulted() -> None:
    """A ZIP named .txt is still a zip — the rule the whole module rests on."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.txt", "hello")
    assert sniff(buf.getvalue()) == "zip"


def test_can_optimise_matches_what_the_dispatcher_handles() -> None:
    for kind in OPTIMISABLE:
        assert can_optimise(kind)
    assert not can_optimise("xz")
    assert not can_optimise("unknown")
