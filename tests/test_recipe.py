"""EN-5 — `kosh run recipe.yaml`: a local pipeline with a manifest of steps.

What is asserted here is the exit test the roadmap names: "a recipe file
reproduces a multi-step run and records each step's evidence". Plus the two
refusals that keep the command honest — a failing step stops the run with a
non-zero exit and the manifest says which step it was, and a recipe naming
something Kosh cannot do is refused before any step runs.
"""

from __future__ import annotations

import gzip
import json
import os
import stat
import sys

import pytest
from typer.testing import CliRunner

from kosh.cli import app
from kosh.core import recipe as recipe_mod
from kosh.core.recipe import RecipeError
from kosh.core.util import hash_file

# --- recipes used below ------------------------------------------------------

GOOD_RECIPE = """\
version: 1
name: shrink-then-prove
description: the shape the roadmap asks for
steps:
  - op: optimize
    name: re-deflate
    paths: [data]
    level: 2
  - op: pack
    paths: [data]
    out: build/bundle.kosh
    codec: zstd
  - op: verify
    target: build/bundle.kosh
"""

FAILING_RECIPE = """\
version: 1
name: one-step-too-far
steps:
  - op: pack
    paths: [data]
    out: build/first.kosh
  - op: pack
    paths: [data/does-not-exist.txt]
    out: build/second.kosh
  - op: verify
    target: build/second.kosh
"""

EVERY_OP_RECIPE = """\
version: 1
name: every-operation
steps:
  - op: pack
    paths: [data]
    out: build/bundle.kosh
  - op: ls
    target: build/bundle.kosh
  - op: info
    container: build/bundle.kosh
  - op: split
    path: build/bundle.kosh
    size: 400
    out: parts
  - op: join
    target: parts
    out: build/joined.kosh
  - op: unpack
    container: build/bundle.kosh
    dest: restored
"""

ENGINE_RECIPE = """\
version: 1
name: through-the-engine
steps:
  - op: optimize
    paths: [data]
  - op: pack
    paths: [data/notes.txt.gz]
    out: build/bundle.dr
    engine: ziptastic
  - op: verify
    target: build/bundle.dr
"""

#: Recipes that must be refused while being read, and the reason each one is here.
BAD_RECIPES = {
    "unknown-operation": "version: 1\nsteps:\n  - op: frobnicate\n    path: x\n",
    "missing-required-argument": "version: 1\nsteps:\n  - op: pack\n    paths: [a]\n",
    "argument-it-does-not-take": (
        "version: 1\nsteps:\n  - op: pack\n    paths: [a]\n    ou: b.kosh\n"
    ),
    "engine-cannot-honour-codec": (
        "version: 1\nsteps:\n  - op: pack\n    paths: [a]\n    out: b.dr\n"
        "    engine: ziptastic\n    codec: zstd\n"
    ),
    "missing-version": "steps:\n  - op: pack\n    paths: [a]\n    out: b.kosh\n",
    "unsupported-version": "version: 99\nsteps:\n  - op: pack\n    paths: [a]\n    out: b\n",
    "empty-steps": "version: 1\nsteps: []\n",
    "no-steps": "version: 1\nname: nothing\n",
    "flow-mapping": "version: 1\nsteps: [{op: pack}]\n",
    "tab-indent": "version: 1\nsteps:\n\t- op: pack\n",
    "anchor": "version: 1\nsteps:\n  - op: pack\n    paths: &p [a]\n    out: b\n",
    "block-scalar": "version: 1\nsteps:\n  - op: pack\n    paths: [a]\n    out: |\n      b\n",
    "duplicate-key": "version: 1\nsteps:\n  - op: pack\n    op: pack\n",
    "wrong-type": (
        "version: 1\nsteps:\n  - op: pack\n    paths: [a]\n    out: b\n    level: high\n"
    ),
    "step-without-op": "version: 1\nsteps:\n  - paths: [a]\n    out: b\n",
    "unknown-top-level-key": "version: 1\nstep:\n  - op: pack\n",
}


@pytest.fixture()
def workspace(tmp_path):
    """A directory holding one small optimisable file, and nothing else."""
    data = tmp_path / "data"
    data.mkdir()
    with gzip.open(data / "notes.txt.gz", "wb", compresslevel=1) as handle:
        handle.write(b"the quick brown fox jumps over the lazy dog\n" * 400)
    return tmp_path


def _write(root, name: str, text: str):
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


def _stand_in_engine(root):
    """A `ziptastic` on PATH, as a script. Same shape the family tests use."""
    directory = root / "bin"
    directory.mkdir(exist_ok=True)
    if os.name == "nt":
        script = directory / "ziptastic.py"
        (directory / "ziptastic.cmd").write_text(
            f'@"{sys.executable}" "%~dp0ziptastic.py" %*\r\n', encoding="utf-8"
        )
        return script
    return directory / "ziptastic"


# --- the roadmap's exit test -------------------------------------------------

def test_a_recipe_runs_end_to_end_and_records_each_step(workspace):
    path = _write(workspace, "recipe.yaml", GOOD_RECIPE)

    result = recipe_mod.run_recipe(recipe_mod.load_recipe(str(path)))

    assert result.ok, result.manifest["failure"]
    manifest = json.loads((workspace / "recipe.manifest.json").read_text(encoding="utf-8"))
    assert manifest["ok"] is True
    assert manifest["failed_step"] is None
    assert manifest["recipe"]["name"] == "shrink-then-prove"
    assert manifest["recipe"]["steps"] == 3
    assert [step["op"] for step in manifest["steps"]] == ["optimize", "pack", "verify"]
    assert all(step["status"] == "ok" for step in manifest["steps"])
    assert all(step["exit_code"] == 0 for step in manifest["steps"])

    # The arguments are recorded as the recipe wrote them, not as they resolved:
    # the manifest is evidence about the recipe, not about this machine.
    assert manifest["steps"][1]["args"] == {
        "paths": ["data"], "out": "build/bundle.kosh", "codec": "zstd",
    }
    assert manifest["steps"][0]["name"] == "re-deflate"

    # Evidence per step: what optimize saved, what pack produced, what verify found.
    optimize_step = manifest["steps"][0]
    assert optimize_step["evidence"]["changed"] == 1
    assert optimize_step["evidence"]["after"] < optimize_step["evidence"]["before"]
    assert optimize_step["outputs"][0]["path"] == os.path.join("data", "notes.txt.gz")

    container = workspace / "build" / "bundle.kosh"
    pack_step = manifest["steps"][1]
    assert pack_step["evidence"]["entries"] == 1
    assert pack_step["evidence"]["stored_size"] == container.stat().st_size
    assert pack_step["outputs"] == [{
        "path": os.path.join("build", "bundle.kosh"),
        "kind": "file",
        "size": container.stat().st_size,
        "sha256": hash_file(str(container)),
    }]

    verify_step = manifest["steps"][2]
    assert verify_step["evidence"]["ok"] is True
    assert verify_step["evidence"]["manifest_verified"] is True


def test_every_operation_in_the_table_can_run(workspace):
    """The op table is not a menu of promises: each entry is exercised here."""
    path = _write(workspace, "every.yaml", EVERY_OP_RECIPE)

    result = recipe_mod.run_recipe(recipe_mod.load_recipe(str(path)))

    assert result.ok, result.manifest["failure"]
    steps = result.manifest["steps"]
    assert [step["op"] for step in steps] == [
        "pack", "ls", "info", "split", "join", "unpack",
    ]
    assert steps[1]["evidence"]["format"] == "kosh"
    assert steps[2]["evidence"]["kind"] == "kosh-container"
    assert steps[3]["evidence"]["parts"] >= 2
    assert steps[4]["evidence"]["verified"] is True
    assert steps[5]["evidence"]["entries"] == 1
    assert (workspace / "restored").is_dir()


def test_the_run_command_exits_zero_and_says_what_it_wrote(workspace):
    path = _write(workspace, "recipe.yaml", GOOD_RECIPE)

    completed = CliRunner().invoke(app, ["run", str(path)])

    assert completed.exit_code == 0, completed.output
    assert "3 step(s) ok" in completed.output
    assert (workspace / "recipe.manifest.json").exists()


def test_a_manifest_path_can_be_chosen(workspace):
    path = _write(workspace, "recipe.yaml", GOOD_RECIPE)
    target = workspace / "evidence" / "run.json"

    completed = CliRunner().invoke(app, ["run", str(path), "--manifest", str(target)])

    assert completed.exit_code == 0, completed.output
    assert target.exists()
    assert not (workspace / "recipe.manifest.json").exists()


# --- the first failure stops the run, and the manifest names it ---------------

def test_a_failing_step_stops_the_run_and_the_manifest_names_it(workspace):
    path = _write(workspace, "fails.yaml", FAILING_RECIPE)

    result = recipe_mod.run_recipe(recipe_mod.load_recipe(str(path)))

    assert result.ok is False
    assert result.failed_step == 2
    manifest = json.loads((workspace / "fails.manifest.json").read_text(encoding="utf-8"))
    steps = manifest["steps"]
    assert steps[0]["status"] == "ok" and steps[0]["exit_code"] == 0
    assert steps[1]["status"] == "failed"
    assert steps[1]["exit_code"] == 1
    assert "does-not-exist.txt" in steps[1]["error"]
    # The step that never ran is recorded as such, not invented as a success.
    assert steps[2]["status"] == "not_run"
    assert steps[2]["exit_code"] is None
    assert steps[2]["evidence"] == {}
    assert manifest["failure"]


def test_the_run_command_exits_non_zero_when_a_step_fails(workspace):
    path = _write(workspace, "fails.yaml", FAILING_RECIPE)

    completed = CliRunner().invoke(app, ["run", str(path)])

    assert completed.exit_code == 1
    # The manifest is written either way: a failed run is the one whose evidence
    # matters most.
    manifest = json.loads((workspace / "fails.manifest.json").read_text(encoding="utf-8"))
    assert manifest["failed_step"] == 2


def test_a_verify_that_finds_damage_fails_its_step(workspace):
    """A step that reports a problem is a failed step, not a successful one."""
    recipe_mod.run_recipe(recipe_mod.load_recipe(str(_write(workspace, "build.yaml", GOOD_RECIPE))))

    container = workspace / "build" / "bundle.kosh"
    data = bytearray(container.read_bytes())
    data[len(data) // 2] ^= 0xFF  # one byte somewhere in the container
    container.write_bytes(bytes(data))

    # A second recipe that only checks the container: re-packing would overwrite
    # the damage this test just made.
    check = _write(workspace, "check.yaml",
                   "version: 1\nsteps:\n  - op: verify\n    target: build/bundle.kosh\n")
    result = recipe_mod.run_recipe(recipe_mod.load_recipe(str(check)))

    assert result.ok is False
    assert result.failed_step == 1
    step = result.manifest["steps"][0]
    assert step["status"] == "failed"
    assert step["exit_code"] == 1
    assert step["evidence"]["ok"] is False
    assert step["error"]


# --- a bad recipe is refused, before anything runs ---------------------------

@pytest.mark.parametrize("case", sorted(BAD_RECIPES))
def test_a_bad_recipe_is_refused_before_any_step_runs(workspace, case):
    path = _write(workspace, f"{case}.yaml", BAD_RECIPES[case])

    with pytest.raises(RecipeError):
        recipe_mod.load_recipe(str(path))

    completed = CliRunner().invoke(app, ["run", str(path)])
    assert completed.exit_code == 1
    assert not (workspace / f"{case}.manifest.json").exists(), (
        "a refused recipe must not leave a manifest claiming a run happened"
    )


def test_a_refusal_names_the_line_or_the_operation(workspace):
    path = _write(workspace, "unknown.yaml", BAD_RECIPES["unknown-operation"])
    with pytest.raises(RecipeError, match="unknown operation 'frobnicate'"):
        recipe_mod.load_recipe(str(path))

    path = _write(workspace, "tabbed.yaml", BAD_RECIPES["tab-indent"])
    with pytest.raises(RecipeError, match=r"tabbed\.yaml:3"):
        recipe_mod.load_recipe(str(path))


def test_a_missing_recipe_file_is_refused(tmp_path):
    with pytest.raises(RecipeError, match="no such recipe"):
        recipe_mod.load_recipe(str(tmp_path / "absent.yaml"))


# --- paths, and the engine step ----------------------------------------------

def test_relative_paths_resolve_against_the_recipe_directory(tmp_path, monkeypatch):
    """A recipe reproduces a run wherever it is invoked from."""
    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    (project / "data" / "notes.txt").write_bytes(b"a payload worth packing\n" * 200)
    _write(project, "recipe.yaml", "version: 1\nsteps:\n"
                                   "  - op: pack\n    paths: [data]\n    out: build/bundle.kosh\n")
    monkeypatch.chdir(tmp_path)  # run from somewhere else entirely

    result = recipe_mod.run_recipe(recipe_mod.load_recipe(str(project / "recipe.yaml")))

    assert result.ok, result.manifest["failure"]
    assert (project / "build" / "bundle.kosh").exists()
    assert result.manifest["base_dir"] == str(project)


def test_a_pack_step_can_drive_a_chce_engine(workspace, monkeypatch):
    """`engine:` on a pack step is how the roadmap's engine stage is expressed."""
    fake = _stand_in_engine(workspace)
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
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    path = _write(workspace, "engine.yaml", ENGINE_RECIPE)

    result = recipe_mod.run_recipe(recipe_mod.load_recipe(str(path)))

    assert result.ok, result.manifest["failure"]
    step = result.manifest["steps"][1]
    assert step["op"] == "pack"
    assert step["evidence"]["engine"] == "ziptastic"
    assert step["evidence"]["stored_size"] > 0
    assert (workspace / "build" / "bundle.dr").exists()
    assert step["outputs"][0]["sha256"] == hash_file(str(workspace / "build" / "bundle.dr"))


# --- the documented subset ---------------------------------------------------

def test_the_parser_reads_the_documented_subset():
    document = recipe_mod.parse_subset(
        "# a comment\n"
        "version: 1\n"
        "name: 'quoted: value'\n"
        "flag: true\n"
        "empty:\n"
        "steps:\n"
        "  - op: pack\n"
        "    paths: [a, 'b c']   # trailing comment\n"
        "    level: 2\n",
        "x",
    )

    assert document["name"] == "quoted: value"
    assert document["flag"] is True
    assert document["empty"] is None
    assert document["steps"] == [{"op": "pack", "paths": ["a", "b c"], "level": 2}]


def test_the_documented_subset_says_what_it_refuses():
    """The contract in RECIPE_SUBSET is the contract the parser enforces."""
    contract = " ".join(recipe_mod.RECIPE_SUBSET)
    for refused in ("anchors/aliases/tags", "flow mappings", "block scalars", "tabs"):
        assert refused in contract
    assert set(recipe_mod.OPS) >= {"optimize", "pack", "unpack", "verify", "split", "join"}
