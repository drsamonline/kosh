"""Property-based round-trips (roadmap CT-3).

The hand-written tests cover the cases their author thought of. These generate
the cases instead: arbitrary bytes split and joined, arbitrary file trees packed
and unpacked, and arbitrary pixel rows through every PNG filter — including the
combinations nobody would sit down and write out, such as a one-row image
filtered with Paeth, or a member name that is a single character.

All three properties share one shape, and it is the shape the whole project
claims: what goes in comes out byte for byte.

## How many cases run

The roadmap's exit test is 10 000 generated cases. A round-trip here touches the
filesystem, and every `atomic_write` fsyncs, so 10 000 cases cost about three and
a half minutes on one machine — on six CI jobs, on every push. That is the same
trade CT-4 already made for fuzzing (`docs/FINDINGS.md` §8): a smaller run on
every push, the full run nightly in `fuzz.yml`.

- **Default (`pytest`)**: 1 000 cases, about 25 s.
- **`KOSH_PROPERTY_CASES=full`**: the roadmap's 10 000 cases.

`test_the_roadmap_case_count_is_met` asserts the full-size total is at least
10 000, so a trimmed constant cannot quietly drop the target.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import tempfile

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from kosh.core import pack, splitjoin
from kosh.core.optimize import _decode_rows, _encode_rows

# The roadmap's 10 000 cases, split across the four properties:
# 2 000 + 2 000 + 3 000 + 3 000.
FULL_SPLIT_CASES = 2_000
FULL_PACK_CASES = 3_000
FULL_FILTER_CASES = 3_000

FULL = os.environ.get("KOSH_PROPERTY_CASES", "").strip().lower() in {"1", "full", "all"}
SCALE = 1 if FULL else 10

SPLIT_CASES = FULL_SPLIT_CASES // SCALE
PACK_CASES = FULL_PACK_CASES // SCALE
FILTER_CASES = FULL_FILTER_CASES // SCALE


def slow(max_examples: int) -> settings:
    """One settings object per test — Hypothesis refuses to stack two decorators.

    `deadline=None`: the default 200 ms deadline measures the machine's
    filesystem, not the code, and a slow runner is not a bug.
    """
    return settings(
        max_examples=max_examples,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )


@pytest.fixture(scope="module")
def scratch(tmp_path_factory):
    """One directory for the module; every example makes its own subdirectory.

    Module-scoped rather than function-scoped because Hypothesis re-runs an
    example while shrinking, and a function-scoped fixture would be torn down
    between those runs.
    """
    root = tmp_path_factory.mktemp("properties")
    yield root
    shutil.rmtree(root, ignore_errors=True)


def example_dir(scratch: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp(dir=scratch, prefix="case-"))


# ------------------------------------------------------------------ split / join
@given(
    payload=st.binary(min_size=1, max_size=512),
    parts=st.integers(min_value=2, max_value=4),
)
@slow(SPLIT_CASES)
def test_split_then_join_is_the_same_bytes(scratch, payload, parts):
    """Split by part count, join, compare — including a payload shorter than the count."""
    case = example_dir(scratch)
    try:
        source = case / "source.bin"
        source.write_bytes(payload)
        split = splitjoin.split(str(source), str(case / "parts"), parts=parts)
        # The part count is a request, not a promise: a 3-byte payload in 4 parts
        # yields one part. What must hold is that the manifest describes them all.
        assert len(split.parts) >= 1
        assert split.total == len(payload)
        assert sum(os.path.getsize(p) for p in split.parts) == len(payload)

        joined = splitjoin.join(split.manifest, str(case / "back.bin"))
        assert joined.verified
        assert joined.total == len(payload)
        assert (case / "back.bin").read_bytes() == payload
    finally:
        shutil.rmtree(case, ignore_errors=True)


@given(
    payload=st.binary(min_size=2, max_size=512),
    chunk=st.integers(min_value=1, max_value=256),
)
@slow(SPLIT_CASES)
def test_split_by_size_then_join_is_the_same_bytes(scratch, payload, chunk):
    """Split by part size, join from the last part file rather than the manifest."""
    case = example_dir(scratch)
    try:
        source = case / "source.bin"
        source.write_bytes(payload)
        if chunk >= len(payload):
            # A part size that is not smaller than the file is refused, with its
            # own hand-written test; the generated case has nothing to add.
            return
        split = splitjoin.split(str(source), str(case / "parts"), part_size=chunk)
        assert all(os.path.getsize(p) <= chunk for p in split.parts)
        assert len(split.parts) == -(-len(payload) // chunk)

        joined = splitjoin.join(split.parts[-1], str(case / "back.bin"))
        assert joined.sha256 == split.sha256
        assert (case / "back.bin").read_bytes() == payload
    finally:
        shutil.rmtree(case, ignore_errors=True)


# ------------------------------------------------------------------ pack / unpack
# Windows refuses a handful of names outright — `CON`, `NUL`, `LPT1` and friends
# are devices, not files — so no property about file names can include them.
_WINDOWS_DEVICES = {"con", "prn", "aux", "nul",
                    *(f"com{i}" for i in range(1, 10)),
                    *(f"lpt{i}" for i in range(1, 10))}

FILE_NAMES = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789._-", min_size=1, max_size=12
).filter(lambda name: name not in (".", "..") and name.lower() not in _WINDOWS_DEVICES)


@given(
    files=st.dictionaries(FILE_NAMES, st.binary(max_size=256), min_size=1, max_size=2),
    codec=st.sampled_from(["store", "zstd"]),
)
@slow(PACK_CASES)
def test_pack_then_unpack_is_the_same_tree(scratch, files, codec):
    # Windows and macOS are case-insensitive: two generated names that differ only
    # by case are one file on disk, so there is no round-trip to assert. This is
    # the only example the property declines.
    if len({name.lower() for name in files}) != len(files):
        return

    case = example_dir(scratch)
    try:
        source = case / "in"
        source.mkdir()
        for name, data in files.items():
            (source / name).write_bytes(data)

        # Ask the filesystem what it actually created, and hold kosh to that. A
        # trailing dot or space in a name is silently dropped by Windows when the
        # file is created — before kosh sees it — so the name on disk, not the
        # generated one, is the thing a round-trip can be required to preserve.
        # (This is how the Windows runner first failed this property: a generated
        # `0.` came back as `0`, and the filesystem had done the renaming.)
        stored = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
        assert stored, "the generator produced no file the filesystem kept"

        container = case / "tree.kosh"
        packed = pack.pack([str(source)], str(container), codec=codec)
        assert packed.entries == len(stored)

        result = pack.verify_container(str(container))
        assert result.ok and result.manifest_verified

        out = case / "out"
        pack.unpack(str(container), str(out))
        restored = {p.name: p.read_bytes() for p in out.rglob("*") if p.is_file()}
        assert restored == stored
    finally:
        shutil.rmtree(case, ignore_errors=True)


# ------------------------------------------------------------------ PNG filters
@given(
    units=st.integers(min_value=1, max_value=8),   # stride = bpp * units
    height=st.integers(min_value=1, max_value=8),
    bpp=st.integers(min_value=1, max_value=4),
    strategy=st.sampled_from([-1, 0, 1, 2, 3, 4]),
    seed=st.integers(min_value=0, max_value=2**32 - 1),
)
@slow(FILTER_CASES)
def test_every_filter_decodes_back_to_the_same_rows(units, height, bpp, strategy, seed):
    """The whole matrix: five filters plus the adaptive choice, any bpp, any shape.

    `stride` is a multiple of `bpp` because that is the only geometry a PNG can
    have — stride is `width * channels * depth / 8`, and bpp divides it by
    construction. Generating the two independently would test shapes that cannot
    exist and fail inside `_decode_rows`' `reshape(-1, bpp)`.
    """
    stride = bpp * units
    rng = np.random.default_rng(seed)
    rows = rng.integers(0, 256, size=(height, stride), dtype=np.uint8)

    encoded = _encode_rows(rows, bpp, strategy)
    assert len(encoded) == height * (stride + 1)

    filter_bytes = encoded[:: stride + 1]
    if strategy >= 0:
        assert set(filter_bytes) == {strategy}
    else:
        assert set(filter_bytes) <= {0, 1, 2, 3, 4}

    decoded = _decode_rows(encoded, height, bpp, stride)
    assert decoded.shape == rows.shape
    assert np.array_equal(decoded, rows)


# ------------------------------------------------------------------ the target
def test_the_roadmap_case_count_is_met():
    """10^4 generated cases is the roadmap's exit test, not a nice-to-have."""
    assert FULL_SPLIT_CASES * 2 + FULL_PACK_CASES + FULL_FILTER_CASES >= 10_000
