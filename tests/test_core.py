"""End-to-end tests: every operation must round-trip byte for byte."""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import zipfile

import pytest

from kosh.core import archive, optimize, pack, splitjoin
from kosh.core.util import KoshError


def tree_digest(root: str) -> dict[str, str]:
    out = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


@pytest.fixture()
def corpus(tmp_path):
    root = tmp_path / "corpus"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "notes.txt").write_bytes(b"redundant text line\n" * 5000)
    (root / "docs" / "blob.bin").write_bytes(os.urandom(300_000))
    (root / "empty.txt").write_bytes(b"")
    return root


# --------------------------------------------------------------------------- split
def test_split_by_size_and_join(corpus, tmp_path):
    source = corpus / "docs" / "blob.bin"
    original = source.read_bytes()
    result = splitjoin.split(str(source), str(tmp_path / "parts"), part_size="100K")
    assert len(result.parts) == 3
    joined = splitjoin.join(str(tmp_path / "parts"), str(tmp_path / "back.bin"))
    assert joined.verified
    assert (tmp_path / "back.bin").read_bytes() == original


def test_split_by_parts_and_join_from_any_part(corpus, tmp_path):
    source = corpus / "docs" / "notes.txt"
    original = source.read_bytes()
    result = splitjoin.split(str(source), str(tmp_path / "parts"), parts=4)
    assert len(result.parts) == 4
    joined = splitjoin.join(result.parts[2], str(tmp_path / "back.txt"))
    assert (tmp_path / "back.txt").read_bytes() == original


def test_join_detects_corruption(corpus, tmp_path):
    source = corpus / "docs" / "notes.txt"
    result = splitjoin.split(str(source), str(tmp_path / "parts"), parts=3)
    with open(result.parts[1], "r+b") as fh:
        fh.seek(10)
        fh.write(b"XXXX")
    with pytest.raises(KoshError, match="checksum|corrupt"):
        splitjoin.join(str(tmp_path / "parts"), str(tmp_path / "back.txt"))


def test_split_rejects_bad_arguments(corpus, tmp_path):
    with pytest.raises(KoshError):
        splitjoin.split(str(corpus / "docs" / "blob.bin"), str(tmp_path), part_size="10G")
    with pytest.raises(KoshError):
        splitjoin.split(str(corpus / "docs" / "blob.bin"), str(tmp_path), parts=1)


def test_inspect_reports_missing_parts(corpus, tmp_path):
    source = corpus / "docs" / "notes.txt"
    result = splitjoin.split(str(source), str(tmp_path / "parts"), parts=3)
    os.unlink(result.parts[1])
    info = splitjoin.inspect(str(tmp_path / "parts"))
    assert info["complete"] is False


# --------------------------------------------------------------------------- pack
@pytest.mark.parametrize("codec", ["zstd", "brotli", "lzma", "store"])
def test_pack_round_trip(corpus, tmp_path, codec):
    before = tree_digest(str(corpus))
    out = tmp_path / f"c-{codec}.kosh"
    result = pack.pack([str(corpus)], str(out), codec=codec, level=None, block_size=64 * 1024)
    assert result.entries == 3
    dest = tmp_path / f"x-{codec}"
    unpacked = pack.unpack(str(out), str(dest))
    assert unpacked.verified
    assert tree_digest(str(dest / "corpus")) == before


def test_pack_compresses_redundant_data(corpus, tmp_path):
    out = tmp_path / "c.kosh"
    result = pack.pack([str(corpus / "docs" / "notes.txt")], str(out), codec="zstd", level=9)
    assert result.stored_size < result.raw_size / 10


def test_unpack_selective(corpus, tmp_path):
    out = tmp_path / "c.kosh"
    pack.pack([str(corpus)], str(out))
    dest = tmp_path / "sel"
    pack.unpack(str(out), str(dest), members=["corpus/docs/notes.txt"])
    assert (dest / "corpus" / "docs" / "notes.txt").exists()
    assert not (dest / "corpus" / "docs" / "blob.bin").exists()


def test_unpack_detects_truncation(corpus, tmp_path):
    out = tmp_path / "c.kosh"
    pack.pack([str(corpus)], str(out))
    data = out.read_bytes()
    out.write_bytes(data[:-40])
    with pytest.raises(KoshError):
        pack.unpack(str(out), str(tmp_path / "x"))


def test_container_lists_entries(corpus, tmp_path):
    out = tmp_path / "c.kosh"
    pack.pack([str(corpus)], str(out))
    info = pack.list_container(str(out))
    assert info["codec"] == "zstd"
    assert any(e["path"].endswith("notes.txt") for e in info["entries"])


# --------------------------------------------------------------------------- archive
@pytest.mark.parametrize("name", ["a.zip", "a.tar", "a.tar.gz", "a.tar.zst", "a.kosh"])
def test_archive_round_trip(corpus, tmp_path, name):
    before = tree_digest(str(corpus))
    out = tmp_path / name
    archive.create(str(out), [str(corpus)])
    listing = archive.list_archive(str(out))
    assert listing["entries"]
    dest = tmp_path / ("x-" + name.replace(".", "_"))
    archive.extract(str(out), str(dest))
    assert tree_digest(str(dest / "corpus")) == before


def test_extract_blocks_path_traversal(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escaped.txt", "nope")
    with pytest.raises(KoshError, match="unsafe"):
        archive.extract(str(evil), str(tmp_path / "out"))


# --------------------------------------------------------------------------- optimize
def _png(tmp_path, compress_level=1):
    PIL = pytest.importorskip("PIL.Image")
    import numpy as np
    rng = np.random.default_rng(7)
    arr = np.zeros((160, 240, 3), dtype=np.uint8)
    arr[:, :, 0] = np.arange(240)[None, :]
    arr[:, :, 1] = np.arange(160)[:, None]
    arr[:, :, 2] = (np.arange(160)[:, None] * np.arange(240)[None, :]) % 256
    noise = rng.integers(0, 12, size=arr.shape, dtype=np.uint8)
    arr = np.clip(arr.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    path = tmp_path / "img.png"
    PIL.fromarray(arr).save(path, compress_level=compress_level)
    return path, arr, PIL


def test_png_optimize_is_lossless_and_smaller(tmp_path):
    path, arr, PIL = _png(tmp_path)
    before = path.stat().st_size
    result = optimize.optimize(str(path), level=1)
    assert result.changed
    assert path.stat().st_size < before
    with PIL.open(path) as img:
        img.load()
        import numpy as np
        assert np.array_equal(np.asarray(img), arr)


def test_gzip_optimize_keeps_payload(tmp_path):
    raw = b"compressible payload\n" * 4000
    path = tmp_path / "x.gz"
    with gzip.open(path, "wb", compresslevel=1) as fh:
        fh.write(raw)
    before = path.stat().st_size
    result = optimize.optimize(str(path), level=1, strip=True)
    assert result.changed and path.stat().st_size < before
    assert gzip.open(path, "rb").read() == raw


def test_zip_optimize_keeps_entries(tmp_path):
    src = tmp_path / "a.txt"
    src.write_bytes(b"zip me\n" * 3000)
    path = tmp_path / "x.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        zf.write(src, "a.txt")
        zf.writestr("b.bin", os.urandom(1000))
    before = path.stat().st_size
    result = optimize.optimize(str(path), level=1)
    assert result.changed and path.stat().st_size < before
    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None
        assert zf.read("a.txt") == src.read_bytes()


def test_jpeg_metadata_strip(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    path = tmp_path / "p.jpg"
    PIL.new("RGB", (64, 64), (20, 120, 90)).save(path, quality=90)
    exif = PIL.Exif()
    exif[0x010E] = "x" * 4000
    PIL.new("RGB", (64, 64), (20, 120, 90)).save(path, quality=90, exif=exif.tobytes())
    before = path.stat().st_size
    result = optimize.optimize(str(path), strip=True)
    assert result.changed and path.stat().st_size < before
    with PIL.open(path) as img:
        assert img.size == (64, 64)


def test_dry_run_does_not_write(tmp_path):
    path, _arr, _PIL = _png(tmp_path)
    before = path.read_bytes()
    result = optimize.optimize(str(path), level=1, dry_run=True)
    assert result.changed
    assert path.read_bytes() == before


def test_optimize_ignores_plain_files(tmp_path):
    path = tmp_path / "plain.txt"
    path.write_bytes(b"hello")
    result = optimize.optimize(str(path))
    assert result.changed is False and result.kind == "unknown"


# --------------------------------------------------------------------------- png codec
def test_png_filter_round_trip_all_strategies():
    import numpy as np
    from kosh.core.optimize import _decode_rows, _encode_rows
    rng = np.random.default_rng(11)
    rows = rng.integers(0, 256, size=(29, 96), dtype=np.uint8)
    for bpp in (1, 3, 4):
        for strategy in (-1, 0, 1, 2, 3, 4):
            encoded = _encode_rows(rows, bpp, strategy)
            assert np.array_equal(_decode_rows(encoded, 29, bpp, 96), rows), (bpp, strategy)
