"""ZIP64 in `verify` (roadmap CT-6).

An archive with more than 65 535 entries cannot write its count in the classic
end-of-central-directory record: the field is 16 bits, so the writer sets it to
0xFFFF and puts the real count in a ZIP64 record beside it. A reader that stops
at the classic record therefore reports 65 535 entries for an archive of any
size above that, and — worse — calls the archive intact without having looked at
the rest of it.

The fixture is built here, record by record, instead of with `zipfile`: 65 536
members take about 20 s and 6 MB to write through `zipfile`, and a 6 MB binary
committed to the repository is a fixture nobody can read. Writing the local
headers and the two end records directly takes a fraction of a second, is
reviewable, and lets a test corrupt one member on purpose.
"""

from __future__ import annotations

import struct
import zipfile
import zlib

import pytest

from kosh.core import verify

ENTRY_COUNT = 65_536  # one more than 0xFFFF, which is the whole point
LOCAL = struct.Struct("<IHHHHHIIIHH")   # local file header, fixed part
CENTRAL = struct.Struct("<IHHHHHHIIIHHHHHII")  # central directory header, fixed part
ZIP64_EOCD = struct.Struct("<IQHHIIQQQQ")
ZIP64_LOCATOR = struct.Struct("<IIQI")
EOCD = struct.Struct("<IHHHHIIH")


def build_zip64(path, count: int, *, corrupt: str | None = None) -> None:
    """Write a ZIP64 archive of *count* stored members, by hand.

    Every member holds ``b"x"`` except *corrupt*, which holds a payload its CRC
    does not match — the only way to prove that deep verify really decompressed
    the entries instead of trusting the directory.
    """
    central = bytearray()
    offset = 0
    with open(path, "wb") as out:
        for index in range(count):
            name = f"e{index:06d}.txt".encode()
            data = b"x"
            if corrupt is not None and name == corrupt.encode():
                data = b"not what the crc says"
                crc = 0xDEADBEEF
            else:
                crc = zlib.crc32(data) & 0xFFFFFFFF
            header = LOCAL.pack(0x04034B50, 45, 0, 0, 0, 0, crc, len(data), len(data),
                                len(name), 0)
            out.write(header)
            out.write(name)
            out.write(data)
            central += CENTRAL.pack(0x02014B50, 45, 45, 0, 0, 0, 0, crc, len(data),
                                    len(data), len(name), 0, 0, 0, 0, 0, offset)
            central += name
            offset += len(header) + len(name) + len(data)

        cd_offset = offset
        out.write(central)
        # ZIP64 end of central directory, its locator, then the classic record
        # with the counts pinned at 0xFFFF — exactly what a writer must emit once
        # the real count no longer fits.
        zip64_offset = cd_offset + len(central)
        out.write(ZIP64_EOCD.pack(0x06064B50, 44, 45, 45, 0, 0, count, count,
                                  len(central), cd_offset))
        out.write(ZIP64_LOCATOR.pack(0x07064B50, 0, zip64_offset, 1))
        out.write(EOCD.pack(0x06054B50, 0, 0, 0xFFFF, 0xFFFF, len(central), cd_offset, 0))


@pytest.fixture(scope="module")
def big_zip64(tmp_path_factory):
    path = tmp_path_factory.mktemp("zip64") / "many.zip"
    build_zip64(path, ENTRY_COUNT)
    return path


def test_the_fixture_really_is_zip64(big_zip64):
    """Without this, every test below would pass on an ordinary zip."""
    raw = big_zip64.read_bytes()
    assert b"PK\x06\x06" in raw, "no ZIP64 end-of-central-directory record"
    assert b"PK\x06\x07" in raw, "no ZIP64 locator"
    classic = raw.rindex(b"PK\x05\x06")
    counts = struct.unpack("<HH", raw[classic + 8:classic + 12])
    assert counts == (0xFFFF, 0xFFFF), "the classic record should be saturated"
    with zipfile.ZipFile(big_zip64) as zf:
        assert len(zf.infolist()) == ENTRY_COUNT


def test_verify_reads_a_zip64_archive(big_zip64):
    """The exit test CT-6 names: intact, and the real entry count."""
    result = verify.verify(str(big_zip64))
    assert result["kind"] == "zip"
    assert result["ok"], result["problems"]
    assert result["entries"] == ENTRY_COUNT
    assert result["entries"] > 0xFFFF
    assert result["bytes"] == ENTRY_COUNT
    assert result["zip64"] is True
    assert result["problems"] == []
    assert result["checked"] == "every entry decompressed"


def test_verify_quick_reads_the_zip64_directory_without_decompressing(big_zip64):
    result = verify.verify(str(big_zip64), deep=False)
    assert result["ok"]
    assert result["entries"] == ENTRY_COUNT
    assert result["zip64"] is True
    assert result["checked"] == "central directory only"


def test_verify_catches_a_corrupt_entry_in_a_zip64_archive(tmp_path):
    """A small archive still carries the ZIP64 records, so this is cheap."""
    path = tmp_path / "small.zip"
    build_zip64(path, 3, corrupt="e000001.txt")
    assert b"PK\x06\x06" in path.read_bytes()

    deep = verify.verify(str(path))
    assert deep["zip64"] is True
    assert not deep["ok"]
    assert any("e000001.txt" in problem and "CRC" in problem
               for problem in deep["problems"]), deep["problems"]

    # `--quick` promises structure only, so it must not report the CRC it never
    # computed — and it must say which check it did.
    quick = verify.verify(str(path), deep=False)
    assert quick["ok"]
    assert quick["checked"] == "central directory only"


def test_a_plain_zip_is_not_called_zip64(tmp_path):
    path = tmp_path / "plain.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("a.txt", "hello")
    result = verify.verify(str(path))
    assert result["ok"] and result["entries"] == 1
    assert result["zip64"] is False
