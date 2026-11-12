"""Corruption fuzzing (CT-4): `verify` must fail closed on any mutation.

The property is narrow on purpose. For a mutation of a container, `verify` may:

  * refuse it — raise KoshError or return ok=False; or
  * accept it **only** if the container still parses to exactly the same structure,
    which is what a change to insignificant whitespace inside the table of
    contents produces.

Anything else is a false "intact", and that is the failure this file exists to
catch. Two real ones were found the first time it ran: renaming the `version` key
switched the manifest-digest check off, and damaging the START magic dropped the
file into the generic "intact (hash only)" branch.

Size: 400 mutations per run here, 20 000 with KOSH_FUZZ=20000. The roadmap asks
for 10⁵; at ~2 ms per verify that is a three-minute job, so it belongs in its own
workflow rather than on every push. The bound is stated rather than implied.
"""
from __future__ import annotations

import os
import pathlib
import random
import struct

import pytest

from kosh.core import pack, verify
from kosh.core.util import KoshError

RUNS = int(os.environ.get("KOSH_FUZZ", "400"))


@pytest.fixture()
def container(tmp_path) -> pathlib.Path:
    src = tmp_path / "payload.txt"
    src.write_bytes(b"a verifiable payload\n" * 400)
    out = tmp_path / "c.kosh"
    pack.pack([str(src)], str(out), codec="zstd", level=3)
    return out


def _mutations(data: bytes, count: int, seed: int = 7):
    """Bit flips, byte replacements, truncations and splices."""
    rng = random.Random(seed)
    for _ in range(count):
        kind = rng.choice(("bit", "byte", "truncate", "zero-run", "duplicate"))
        mutated = bytearray(data)
        if kind == "bit":
            pos = rng.randrange(len(mutated))
            mutated[pos] ^= 1 << rng.randrange(8)
        elif kind == "byte":
            pos = rng.randrange(len(mutated))
            mutated[pos] = rng.randrange(256)
        elif kind == "truncate":
            mutated = mutated[: rng.randrange(1, len(mutated))]
        elif kind == "zero-run":
            start = rng.randrange(len(mutated))
            length = min(rng.randrange(1, 64), len(mutated) - start)
            mutated[start:start + length] = b"\x00" * length
        else:
            pos = rng.randrange(len(mutated))
            mutated = mutated[:pos] + mutated[pos:pos + 16] + mutated[pos:]
        yield kind, bytes(mutated)


def _parsed(data: bytes):
    """The structure a container parses to, or None if it does not parse."""
    try:
        return pack.read_toc_bytes(data) if hasattr(pack, "read_toc_bytes") else None
    except KoshError:
        return None


def test_verify_never_calls_a_damaged_container_intact(container, tmp_path):
    data = container.read_bytes()
    original = pack.read_toc(str(container))
    target = tmp_path / "mutated.kosh"
    false_intact = []
    other_exceptions = []

    for kind, mutated in _mutations(data, RUNS):
        target.write_bytes(mutated)
        try:
            result = verify.verify(str(target))
        except KoshError:
            continue  # refused: correct
        except Exception as exc:  # noqa: BLE001 - the point is to catch everything
            other_exceptions.append((kind, type(exc).__name__, str(exc)[:80]))
            continue

        if not result["ok"]:
            continue
        # Accepted. Legal only when the container still means exactly the same thing.
        try:
            parsed = pack.read_toc(str(target))
        except KoshError:
            false_intact.append((kind, "accepted but no longer parses"))
            continue
        if parsed != original:
            false_intact.append((kind, "accepted with a different table of contents"))

    assert not other_exceptions, f"verify raised something other than KoshError: {other_exceptions[:3]}"
    assert not false_intact, f"verify called a damaged container intact: {false_intact[:3]}"


def test_a_container_with_a_broken_header_is_not_intact(container, tmp_path):
    """The footer says KOSH; the header does not. That is damage, not an unknown file."""
    data = bytearray(container.read_bytes())
    data[0] ^= 0xFF
    broken = tmp_path / "broken.kosh"
    broken.write_bytes(bytes(data))
    result = verify.verify(str(broken))
    assert result["ok"] is False
    assert "footer says KOSH" in result["problems"][0]


def test_renaming_the_version_key_cannot_switch_the_digest_off(container, tmp_path):
    """A one-byte edit used to bypass the manifest digest entirely."""
    data = container.read_bytes()
    position = data.index(b'"version"')
    mutated = bytearray(data)
    mutated[position + 1] = ord("w")  # "version" -> "wersion"
    broken = tmp_path / "renamed.kosh"
    broken.write_bytes(bytes(mutated))
    with pytest.raises(KoshError, match="no version"):
        pack.read_toc(str(broken))


def test_every_single_bit_flip_is_refused(container, tmp_path):
    """The exhaustive version of the property: every bit of every byte.

    3 688 mutations on a 461-byte container, and not one may be accepted unless the
    container still parses to the same structure. This found the `OSError` that the
    random fuzzer only hit intermittently: a flipped bit in the table-of-contents
    length field made the reader seek before the start of the file, which raises
    something that is not a KoshError — a crash rather than a refusal.
    """
    data = container.read_bytes()
    original = pack.read_toc(str(container))
    target = tmp_path / "bitflip.kosh"
    failures = []

    for position in range(len(data)):
        for bit in range(8):
            mutated = bytearray(data)
            mutated[position] ^= 1 << bit
            target.write_bytes(bytes(mutated))
            try:
                result = verify.verify(str(target))
            except KoshError:
                continue
            except Exception as exc:  # noqa: BLE001 - anything else is a crash
                failures.append((position, bit, f"{type(exc).__name__}: {exc}"))
                continue
            if not result["ok"]:
                continue
            try:
                parsed = pack.read_toc(str(target))
            except KoshError:
                failures.append((position, bit, "accepted, does not parse"))
                continue
            if parsed != original:
                failures.append((position, bit, "accepted with a different table of contents"))

    assert not failures, f"{len(failures)} mutation(s) not refused: {failures[:5]}"
