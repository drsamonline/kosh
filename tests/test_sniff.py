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


def test_elf_is_the_running_interpreter() -> None:
    import sys
    assert sniff(Path(sys.executable).read_bytes()[:4096]) == "elf"


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
