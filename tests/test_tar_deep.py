"""`verify --quick` on a tar must not decompress it.

The zip path had this defect and it was fixed under CT-6: `--quick` is
documented as "structure only; do not decompress", and it was decompressing
every entry anyway. The tar path had the same defect and was left behind, so a
quick check of a large tarball still cost a full read.

These tests hold the flag to its word by making the decompression call
observable rather than by timing it.
"""
from __future__ import annotations

import io
import tarfile

import pytest

from kosh.core import verify as verify_mod


def _tarball(path, members=3, size=4096):
    """A real .tar with compressible members, built at test time."""
    with tarfile.open(path, "w") as tf:
        for i in range(members):
            data = (b"payload-%d " % i) * (size // 10)
            info = tarfile.TarInfo(name=f"member-{i}.bin")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


@pytest.fixture()
def tar_path(tmp_path):
    return _tarball(tmp_path / "sample.tar")


def test_quick_tar_does_not_read_member_data(tar_path, monkeypatch):
    """--quick must never call extractfile: that is what decompresses."""
    calls = []

    def refuse(self, member):
        calls.append(member.name)
        raise AssertionError(f"extractfile called for {member.name} under --quick")

    monkeypatch.setattr(tarfile.TarFile, "extractfile", refuse)
    result = verify_mod.verify(str(tar_path), deep=False)

    assert calls == []
    assert result["ok"] is True
    assert result["entries"] == 3
    assert result["checked"] == "headers only"


def test_deep_tar_reads_every_member(tar_path, monkeypatch):
    """The same archive under deep verify does read the data, and says so."""
    calls = []
    original = tarfile.TarFile.extractfile

    def counted(self, member):
        calls.append(member.name)
        return original(self, member)

    monkeypatch.setattr(tarfile.TarFile, "extractfile", counted)
    result = verify_mod.verify(str(tar_path), deep=True)

    assert len(calls) == 3
    assert result["ok"] is True
    assert result["checked"] == "every entry read (no per-member checksum in tar)"


def test_quick_and_deep_agree_on_a_healthy_archive(tar_path):
    """The flag changes how much is read, not what a healthy archive reports."""
    quick = verify_mod.verify(str(tar_path), deep=False)
    deep = verify_mod.verify(str(tar_path), deep=True)

    assert quick["ok"] is deep["ok"] is True
    assert quick["entries"] == deep["entries"] == 3
    assert quick["bytes"] == deep["bytes"]
    assert quick["checked"] != deep["checked"]


def test_a_truncated_tar_is_refused_in_both_modes(tmp_path):
    """Headers-only still has to notice an archive that cannot be walked."""
    path = _tarball(tmp_path / "cut.tar")
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])

    for deep in (False, True):
        result = verify_mod.verify(str(path), deep=deep)
        assert result["ok"] is False, f"truncated tar passed with deep={deep}"
        assert result["problems"]
