"""EN-5 — ``kosh run recipe.yaml``: a local pipeline with a manifest of steps.

heavenzip chains *binaries* globally. There was no way to chain a *local* job —
optimise this folder, pack the result, prove the container, in one command, with a
record of what happened. This module is that: a recipe file names an ordered list
of steps, each step names an existing kosh operation and its arguments, and the
run writes a manifest holding, per step, the operation, the arguments, the exit
status and the evidence it produced (output paths, sizes, SHA-256).

Three deliberate choices, stated here because they shape everything below.

**The recipe format is a narrow YAML subset, not YAML.** Kosh has no YAML
dependency and this feature is not a reason to add one: the parser in this file
understands indented mappings, ``- `` sequences, quoted and bare scalars, integers,
booleans, ``null`` and inline lists of scalars — and refuses everything else by
line number (flow mappings, anchors, aliases, tags, block scalars, tabs, multiple
documents). A recipe is a small, flat, human-typed file; the part of YAML it needs
is small enough to read in one sitting, and refusing the rest is better than
half-supporting it. ``RECIPE_SUBSET`` below is the documented contract.

**Steps run in-process, through the same core functions the CLI dispatches to.**
No subprocess is spawned, so ``exit_code`` in the manifest is the status the step
resolved to — 0, or 1 for a failure — not a child process's return code. This is
stated in docs/RECIPES.md rather than implied.

**A bad recipe refuses before anything runs.** An unknown operation, a missing
required argument, an argument the operation does not take, a duplicate key, a
malformed line: each raises :class:`RecipeError` naming the file and line, and no
step executes and no manifest is written. A command that cannot do what it was
asked must say so, not do something adjacent to it.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import engine as engine_mod
from . import optimize as optimize_mod
from . import pack as pack_mod
from . import splitjoin
from . import verify as verify_mod
from .util import KoshError, atomic_write, hash_file, is_within, parse_size

RECIPE_VERSION = 1
MANIFEST_SUFFIX = ".manifest.json"

# The documented subset, quoted in docs/RECIPES.md. Kept next to the parser so the
# two cannot drift: tests assert each refusal named here actually refuses.
RECIPE_SUBSET = (
    "indented `key: value` mappings",
    "`- ` block sequences",
    "bare, single-quoted and double-quoted scalars",
    "integers, true/false, null/~",
    "inline lists of scalars: [a, b, c]",
    "`#` comments (whole line, or after whitespace)",
    "NOT: flow mappings {..}, anchors/aliases/tags, block scalars | or >, "
    "multi-line scalars, tabs, multiple documents, duplicate keys",
)


class RecipeError(KoshError):
    """A recipe that cannot be read, or names something Kosh cannot do."""


# --------------------------------------------------------------------------- subset parser
_INT = re.compile(r"-?\d+\Z")
_KEY = re.compile(r"([A-Za-z_][A-Za-z0-9_.-]*):(?:\s(.*)|)\Z")


def _strip_comment(text: str) -> str:
    """Drop a trailing ``#`` comment, leaving ``#`` inside quotes alone."""
    quote = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index].rstrip()
    return text.rstrip()


def _scan(text: str, where: str) -> list[list]:
    """Lines that carry content, as ``[indent, content, lineno]``."""
    lines: list[list] = []
    for number, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw:
            raise RecipeError(
                f"{where}:{number}: tabs are not part of the recipe subset — indent with spaces"
            )
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        if stripped.strip() in ("---", "..."):
            raise RecipeError(
                f"{where}:{number}: multiple YAML documents are not supported; "
                "one recipe is one document"
            )
        indent = len(stripped) - len(stripped.lstrip(" "))
        lines.append([indent, stripped.strip(), number])
    return lines


def _split_commas(text: str, where: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quote = None
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
            current.append(char)
        elif char == ",":
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if quote:
        raise RecipeError(f"{where}: unterminated quoted string in list")
    parts.append("".join(current).strip())
    return parts


def _scalar(text: str, where: str) -> Any:
    """One value: quoted string, integer, boolean, null, inline list, or bare string."""
    text = text.strip()
    if text == "":
        return None
    if text.startswith("["):
        if not text.endswith("]"):
            raise RecipeError(
                f"{where}: an inline list must open and close on the same line"
            )
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part, where) for part in _split_commas(inner, where)]
    if text.startswith("{"):
        raise RecipeError(
            f"{where}: inline mappings ({{...}}) are not part of the recipe subset — "
            "write indented keys"
        )
    if text.startswith("&") or text.startswith("*") or text.startswith("!"):
        raise RecipeError(
            f"{where}: anchors, aliases and tags are not part of the recipe subset"
        )
    if text in ("|", ">", "|-", ">-", "|+", ">+"):
        raise RecipeError(
            f"{where}: block scalars (| or >) are not part of the recipe subset — "
            "quote the value instead"
        )
    if text[0] in "'\"":
        quote = text[0]
        if len(text) < 2 or not text.endswith(quote):
            raise RecipeError(f"{where}: unterminated quoted string {text!r}")
        return text[1:-1]
    if text in ("null", "~"):
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    if _INT.match(text):
        return int(text)
    return text


def _as_mapping_line(text: str) -> bool:
    return bool(_KEY.match(text))


def _parse_mapping(lines: list, index: int, indent: int, where: str) -> tuple[dict, int]:
    out: dict = {}
    while index < len(lines):
        line_indent, content, number = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise RecipeError(
                f"{where}:{number}: unexpected indent — this line is deeper than the key "
                "it appears under"
            )
        if content.startswith("- "):
            break
        match = _KEY.match(content)
        if not match:
            raise RecipeError(
                f"{where}:{number}: expected `key: value` (a colon followed by a space), "
                f"found {content!r}"
            )
        key, rest = match.group(1), (match.group(2) or "").strip()
        if key in out:
            raise RecipeError(f"{where}:{number}: duplicate key {key!r}")
        if rest:
            out[key] = _scalar(rest, f"{where}:{number}")
            index += 1
            continue
        # `key:` with nothing after it: either a nested block or an empty value.
        if index + 1 < len(lines) and lines[index + 1][0] > indent:
            value, index = _parse_block(lines, index + 1, lines[index + 1][0], where)
            out[key] = value
        else:
            out[key] = None
            index += 1
    return out, index


def _parse_sequence(lines: list, index: int, indent: int, where: str) -> tuple[list, int]:
    out: list = []
    while index < len(lines):
        line_indent, content, number = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise RecipeError(
                f"{where}:{number}: unexpected indent — this line is deeper than the item "
                "it appears under"
            )
        if not content.startswith("- "):
            break
        body = content[2:].strip()
        if body == "":
            if index + 1 < len(lines) and lines[index + 1][0] > indent:
                value, index = _parse_block(lines, index + 1, lines[index + 1][0], where)
            else:
                value, index = None, index + 1
        elif _as_mapping_line(body):
            # `- op: pack` opens a mapping item whose keys line up after the dash.
            lines[index] = [indent + 2, body, number]
            value, index = _parse_mapping(lines, index, indent + 2, where)
        else:
            value, index = _scalar(body, f"{where}:{number}"), index + 1
        out.append(value)
    return out, index


def _parse_block(lines: list, index: int, indent: int, where: str):
    if lines[index][1].startswith("- "):
        return _parse_sequence(lines, index, indent, where)
    return _parse_mapping(lines, index, indent, where)


def parse_subset(text: str, where: str = "<recipe>") -> Any:
    """Parse the documented YAML subset. Raises RecipeError for anything outside it."""
    lines = _scan(text, where)
    if not lines:
        raise RecipeError(f"{where}: the recipe is empty")
    value, index = _parse_block(lines, 0, lines[0][0], where)
    if index < len(lines):
        raise RecipeError(f"{where}:{lines[index][2]}: could not read this line")
    return value


# --------------------------------------------------------------------------- recipe model
@dataclass
class Step:
    index: int
    op: str
    args: dict
    name: Optional[str] = None


@dataclass
class Recipe:
    path: str
    version: int
    name: str
    steps: list[Step]
    description: str = ""
    base_dir: str = ""

    @property
    def label(self) -> str:
        return self.name or os.path.basename(self.path)


@dataclass
class StepOutcome:
    """What a step produced: the evidence to record, and the files to describe."""

    evidence: dict
    outputs: list[str] = field(default_factory=list)
    ok: bool = True


@dataclass
class RunResult:
    manifest: dict
    manifest_path: str

    @property
    def ok(self) -> bool:
        return bool(self.manifest["ok"])

    @property
    def failed_step(self) -> Optional[int]:
        return self.manifest.get("failed_step")


# --------------------------------------------------------------------------- operations
def expand_paths(paths: list[str], recursive: bool = True) -> list[str]:
    """The same expansion `kosh optimize` applies: directories walked, files kept."""
    out: list[str] = []
    for raw in paths:
        if os.path.isdir(raw):
            for root, _dirs, files in os.walk(raw):
                for name in sorted(files):
                    out.append(os.path.join(root, name))
                if not recursive:
                    break
        elif os.path.isfile(raw):
            out.append(raw)
        else:
            raise KoshError(f"no such file or directory: {raw}")
    return out


def _op_optimize(args: dict) -> StepOutcome:
    targets = expand_paths(args["paths"], args.get("recursive", True))
    if not targets:
        raise KoshError("no supported files found")
    results = optimize_mod.optimize_many(
        targets,
        args.get("level", 2),
        args.get("strip", False),
        args.get("force", False),
        args.get("dry_run", False),
        args.get("backup", False),
        args.get("jobs", 1),
    )
    return StepOutcome(
        evidence={
            "files": [r.as_dict() for r in results],
            "before": sum(r.before for r in results),
            "after": sum(r.after for r in results),
            "changed": sum(1 for r in results if r.changed),
        },
        outputs=targets,
    )


ENGINE_IGNORES = ("codec", "level", "block", "threads")


def engine_conflicts(args: dict) -> list[str]:
    """Arguments a pack step cannot take when an engine does the compressing."""
    if args.get("engine", "native") == "native":
        return []
    return sorted(key for key in ENGINE_IGNORES if key in args)


def _op_pack(args: dict) -> StepOutcome:
    engine = args.get("engine", "native")
    out = args["out"]
    if engine != "native":
        conflicts = engine_conflicts(args)
        if conflicts:
            raise KoshError(
                f"pack with engine {engine!r} takes no {', '.join(conflicts)} — "
                "the engine chooses its own codec and block size"
            )
        info = engine_mod.pack_with_engine(engine, args["paths"], out)
        return StepOutcome(evidence=dict(info, engine=engine), outputs=[out])

    result = pack_mod.pack(
        args["paths"],
        out,
        args.get("codec", "zstd"),
        args.get("level"),
        parse_size(args.get("block", "8M")),
        args.get("threads"),
    )
    return StepOutcome(
        evidence={
            "engine": "native",
            "output": result.output,
            "entries": result.entries,
            "codec": result.codec,
            "raw_size": result.raw_size,
            "stored_size": result.stored_size,
            "ratio": round(result.ratio, 4),
            "seconds": round(result.seconds, 2),
        },
        outputs=[out],
    )


def _op_unpack(args: dict) -> StepOutcome:
    container = args["container"]
    dest = args.get("dest", ".")
    with open(container, "rb") as handle:
        head = handle.read(64)
    if head.startswith(engine_mod.CHCE_MAGIC):
        engine = args.get("engine", "auto")
        if engine == "native":
            raise KoshError(
                "this is a CHCE stream, not a Kosh container; unpack it with "
                "engine: ziptastic (or run `ziptastic d` yourself)"
            )
        chosen = "ziptastic" if engine == "auto" else engine
        info = engine_mod.unpack_with_engine(chosen, container, dest)
        return StepOutcome(evidence=dict(info, engine=chosen), outputs=[dest])

    budget = parse_size(args["max_size"]) if args.get("max_size") else None
    result = pack_mod.unpack(
        container, dest, args.get("only"), verify=args.get("verify", True), max_bytes=budget
    )
    return StepOutcome(
        evidence={
            "output_dir": result.output_dir,
            "entries": result.entries,
            "bytes": result.bytes,
            "verified": result.verified,
            "skipped": result.skipped,
        },
        outputs=[dest],
    )


def _op_verify(args: dict) -> StepOutcome:
    result = verify_mod.verify(args["target"], deep=not args.get("quick", False),
                               jobs=args.get("jobs"))
    # A verify that says "damaged" is a failed step. Reporting it as a success
    # because the function returned would be the pretence this project refuses.
    return StepOutcome(evidence=dict(result), outputs=[], ok=bool(result["ok"]))


def _op_split(args: dict) -> StepOutcome:
    result = splitjoin.split(
        args["path"], args.get("out"), args.get("size"), args.get("parts"),
        args.get("prefix"), args.get("algo", "sha256"),
    )
    return StepOutcome(
        evidence={
            "manifest": result.manifest,
            "parts": len(result.parts),
            "part_size": result.part_size,
            "total": result.total,
            "sha256": result.sha256,
        },
        outputs=[result.manifest],
    )


def _op_join(args: dict) -> StepOutcome:
    result = splitjoin.join(
        args["target"], args.get("out"),
        verify=args.get("verify", True), overwrite=args.get("overwrite", False),
    )
    return StepOutcome(
        evidence={"output": result.output, "total": result.total,
                  "sha256": result.sha256, "verified": result.verified},
        outputs=[result.output],
    )


def _op_ls(args: dict) -> StepOutcome:
    target = args["target"]
    with open(target, "rb") as handle:
        head = handle.read(64)
    if head.startswith(engine_mod.CHCE_MAGIC):
        raise KoshError(
            f"{os.path.basename(target)} is a CHCE stream, not an archive: one payload and "
            "a block table, no named entries. Use an `info` step instead."
        )
    from . import archive as archive_mod

    info = archive_mod.list_archive(target)
    return StepOutcome(
        evidence={"format": info["format"], "stored_size": info["stored_size"],
                  "entries": info["entries"]},
        outputs=[],
    )


def _op_info(args: dict) -> StepOutcome:
    container = args["container"]
    with open(container, "rb") as handle:
        head = handle.read(64)
    if head.startswith(engine_mod.CHCE_MAGIC):
        header = engine_mod.read_chce_header(head)
        trailer = engine_mod.read_chce_trailer(container)
        blocks = engine_mod.read_blocks(container, trailer)
        return StepOutcome(
            evidence={
                "kind": "chce-stream",
                "version": header["version"],
                "layers": header["layers"],
                "original_size": header["original_size"],
                "true_size": header["true_size"],
                "stored_size": os.path.getsize(container),
                "block_count": trailer["count"],
                "manifest_sha256": trailer["manifest_sha256"],
            },
            outputs=[],
        )
    toc = pack_mod.read_toc(container)
    rows = pack_mod.block_index(container)
    return StepOutcome(
        evidence={
            "kind": "kosh-container",
            "codec": toc.get("codec"),
            "level": toc.get("level"),
            "block_size": toc.get("block_size"),
            "version": toc.get("version"),
            "created": toc.get("created"),
            "stored_size": os.path.getsize(container),
            "manifest_sha256": toc.get("manifest_sha256"),
            "entries": rows,
        },
        outputs=[],
    )


@dataclass(frozen=True)
class OpSpec:
    """One operation a recipe step may name: its handler and the arguments it takes."""

    handler: Callable[[dict], StepOutcome]
    required: frozenset
    optional: frozenset = frozenset()
    paths: frozenset = frozenset()   # argument names resolved against the recipe's directory
    integers: frozenset = frozenset()
    booleans: frozenset = frozenset()
    lists: frozenset = frozenset()


OPS: dict[str, OpSpec] = {
    "optimize": OpSpec(
        handler=_op_optimize,
        required=frozenset({"paths"}),
        optional=frozenset({"level", "strip", "dry_run", "backup", "force", "jobs", "recursive"}),
        paths=frozenset({"paths"}),
        integers=frozenset({"level", "jobs"}),
        booleans=frozenset({"strip", "dry_run", "backup", "force", "recursive"}),
        lists=frozenset({"paths"}),
    ),
    "pack": OpSpec(
        handler=_op_pack,
        required=frozenset({"paths", "out"}),
        optional=frozenset({"codec", "level", "block", "threads", "engine"}),
        paths=frozenset({"paths", "out"}),
        integers=frozenset({"level", "threads"}),
        lists=frozenset({"paths"}),
    ),
    "unpack": OpSpec(
        handler=_op_unpack,
        required=frozenset({"container"}),
        optional=frozenset({"dest", "only", "verify", "engine", "max_size"}),
        paths=frozenset({"container", "dest"}),
        booleans=frozenset({"verify"}),
        lists=frozenset({"only"}),
    ),
    "verify": OpSpec(
        handler=_op_verify,
        required=frozenset({"target"}),
        optional=frozenset({"quick", "jobs"}),
        paths=frozenset({"target"}),
        integers=frozenset({"jobs"}),
        booleans=frozenset({"quick"}),
    ),
    "split": OpSpec(
        handler=_op_split,
        required=frozenset({"path"}),
        optional=frozenset({"size", "parts", "out", "prefix", "algo"}),
        paths=frozenset({"path", "out"}),
        integers=frozenset({"parts"}),
    ),
    "join": OpSpec(
        handler=_op_join,
        required=frozenset({"target"}),
        optional=frozenset({"out", "verify", "overwrite"}),
        paths=frozenset({"target", "out"}),
        booleans=frozenset({"verify", "overwrite"}),
    ),
    "ls": OpSpec(handler=_op_ls, required=frozenset({"target"}), paths=frozenset({"target"})),
    "info": OpSpec(handler=_op_info, required=frozenset({"container"}),
                   paths=frozenset({"container"})),
}

TOP_LEVEL = frozenset({"version", "name", "description", "steps"})
RESERVED_STEP_KEYS = frozenset({"op", "name"})


# --------------------------------------------------------------------------- load
def load_recipe(path: str) -> Recipe:
    """Read and validate a recipe. Nothing runs; a bad recipe never gets that far."""
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise RecipeError(f"no such recipe: {path}")
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise RecipeError(f"cannot read {path}: {exc}") from exc

    where = os.path.basename(path)
    document = parse_subset(text, where)
    if not isinstance(document, dict):
        raise RecipeError(
            f"{where}: a recipe is a mapping with a `steps:` list at the top level"
        )

    unknown = sorted(set(document) - TOP_LEVEL)
    if unknown:
        raise RecipeError(
            f"{where}: unknown top-level key(s) {', '.join(unknown)} — "
            f"a recipe has {', '.join(sorted(TOP_LEVEL))}"
        )

    version = document.get("version")
    if version is None:
        raise RecipeError(f"{where}: `version: {RECIPE_VERSION}` is required")
    if version != RECIPE_VERSION:
        raise RecipeError(
            f"{where}: recipe version {version!r} is not supported; this kosh reads "
            f"version {RECIPE_VERSION}"
        )

    raw_steps = document.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise RecipeError(f"{where}: `steps:` must be a non-empty list")

    steps: list[Step] = []
    for position, raw in enumerate(raw_steps, 1):
        steps.append(_build_step(raw, position, where))

    return Recipe(
        path=path,
        version=version,
        name=str(document.get("name") or os.path.splitext(os.path.basename(path))[0]),
        description=str(document.get("description") or ""),
        steps=steps,
        base_dir=os.path.dirname(path),
    )


def _build_step(raw: Any, position: int, where: str) -> Step:
    label = f"{where}: step {position}"
    if not isinstance(raw, dict):
        raise RecipeError(f"{label}: a step is a mapping with an `op:` key")

    op = raw.get("op")
    if not isinstance(op, str) or not op:
        raise RecipeError(f"{label}: `op:` is required and names an operation")
    if op not in OPS:
        raise RecipeError(
            f"{label}: unknown operation {op!r} — a recipe step can be "
            f"{', '.join(sorted(OPS))}"
        )

    spec = OPS[op]
    name = raw.get("name")
    if name is not None and not isinstance(name, str):
        raise RecipeError(f"{label}: `name:` must be a string")

    given = {key: value for key, value in raw.items() if key not in RESERVED_STEP_KEYS}
    unknown = sorted(set(given) - spec.required - spec.optional)
    if unknown:
        allowed = sorted(spec.required | spec.optional)
        raise RecipeError(
            f"{label}: `{op}` takes no {', '.join(unknown)} — it takes "
            f"{', '.join(allowed)}"
        )
    missing = sorted(spec.required - set(given))
    if missing:
        raise RecipeError(f"{label}: `{op}` needs {', '.join(missing)}")

    conflicts = engine_conflicts(given) if op == "pack" else []
    if conflicts:
        # Refused while reading the recipe, not halfway through the run: an
        # argument that cannot take effect is a mistake, not a step to attempt.
        raise RecipeError(
            f"{label}: `pack` with engine {given['engine']!r} takes no "
            f"{', '.join(conflicts)} — the engine chooses its own codec and block size"
        )

    args: dict = {}
    for key, value in given.items():
        args[key] = _checked_value(key, value, spec, label)

    return Step(index=position, op=op, args=args, name=name)


def _checked_value(key: str, value: Any, spec: OpSpec, label: str) -> Any:
    if value is None:
        raise RecipeError(f"{label}: `{key}` has no value")
    if key in spec.integers:
        if isinstance(value, bool) or not isinstance(value, int):
            raise RecipeError(f"{label}: `{key}` must be a whole number, found {value!r}")
        return value
    if key in spec.booleans:
        if not isinstance(value, bool):
            raise RecipeError(f"{label}: `{key}` must be true or false, found {value!r}")
        return value
    if key in spec.lists:
        items = value if isinstance(value, list) else [value]
        for item in items:
            if not isinstance(item, (str, int)):
                raise RecipeError(f"{label}: `{key}` is a list of paths")
        return [str(item) for item in items]
    if isinstance(value, bool) or isinstance(value, (list, dict)):
        raise RecipeError(f"{label}: `{key}` takes a single value, found {value!r}")
    return str(value)


# --------------------------------------------------------------------------- run
def _describe(path: str, base_dir: str) -> dict:
    """Evidence for one output: where it is, how big, and its SHA-256 if it is a file."""
    shown = os.path.relpath(path, base_dir) if is_within(base_dir, path) else path
    if not os.path.exists(path):
        return {"path": shown, "present": False}
    if os.path.isdir(path):
        files = 0
        total = 0
        for root, _dirs, names in os.walk(path):
            for entry in names:
                files += 1
                total += os.path.getsize(os.path.join(root, entry))
        return {"path": shown, "kind": "directory", "files": files, "size": total}
    return {
        "path": shown,
        "kind": "file",
        "size": os.path.getsize(path),
        "sha256": hash_file(path),
    }


def _resolve_args(step: Step, base_dir: str) -> dict:
    spec = OPS[step.op]
    resolved = dict(step.args)
    for key in spec.paths:
        if key not in resolved:
            continue
        value = resolved[key]
        if isinstance(value, list):
            resolved[key] = [v if os.path.isabs(v) else os.path.join(base_dir, v) for v in value]
        else:
            resolved[key] = value if os.path.isabs(value) else os.path.join(base_dir, value)
    return resolved


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def default_manifest_path(recipe_path: str) -> str:
    stem, _extension = os.path.splitext(os.path.abspath(recipe_path))
    return stem + MANIFEST_SUFFIX


def run_recipe(
    recipe: Recipe,
    manifest_path: Optional[str] = None,
    base_dir: Optional[str] = None,
    on_step: Optional[Callable[[dict], None]] = None,
) -> RunResult:
    """Execute a recipe's steps in order and write the manifest.

    Stops on the first failure: the remaining steps are recorded as ``not_run`` so
    the manifest says exactly how far the run got. The manifest is written either
    way — a failed run is the one whose evidence matters most.
    """
    base = os.path.abspath(base_dir or recipe.base_dir or os.getcwd())
    manifest_path = os.path.abspath(manifest_path or default_manifest_path(recipe.path))

    started_at = _now()
    started = time.perf_counter()
    records: list[dict] = []
    failed_step: Optional[int] = None
    failure: Optional[str] = None

    for step in recipe.steps:
        if failed_step is not None:
            records.append({
                "index": step.index, "op": step.op, "name": step.name,
                "args": step.args, "status": "not_run", "exit_code": None,
                "seconds": None, "evidence": {}, "outputs": [], "error": None,
            })
            continue

        step_started = time.perf_counter()
        record = {
            "index": step.index, "op": step.op, "name": step.name, "args": step.args,
            "status": "ok", "exit_code": 0, "seconds": None,
            "evidence": {}, "outputs": [], "error": None,
        }
        try:
            outcome = OPS[step.op].handler(_resolve_args(step, base))
            record["evidence"] = outcome.evidence
            record["outputs"] = [_describe(path, base) for path in outcome.outputs]
            if not outcome.ok:
                record["status"] = "failed"
                record["exit_code"] = 1
                record["error"] = _failure_note(step.op, outcome.evidence)
        except KoshError as exc:
            record["status"] = "failed"
            record["exit_code"] = 1
            record["error"] = str(exc)
        except OSError as exc:
            record["status"] = "failed"
            record["exit_code"] = 1
            record["error"] = f"{type(exc).__name__}: {exc}"
        record["seconds"] = round(time.perf_counter() - step_started, 3)

        if record["status"] != "ok":
            failed_step = step.index
            failure = record["error"]
        records.append(record)
        if on_step:
            on_step(record)

    manifest = {
        "kosh_version": _version(),
        "recipe": {
            "path": recipe.path,
            "name": recipe.name,
            "description": recipe.description,
            "version": recipe.version,
            "steps": len(recipe.steps),
        },
        "base_dir": base,
        "manifest_path": manifest_path,
        "started": started_at,
        "finished": _now(),
        "seconds": round(time.perf_counter() - started, 3),
        "ok": failed_step is None,
        "failed_step": failed_step,
        "failure": failure,
        "steps": records,
    }

    with atomic_write(manifest_path, "w") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=False)
        handle.write("\n")

    return RunResult(manifest=manifest, manifest_path=manifest_path)


def _failure_note(op: str, evidence: dict) -> str:
    problems = evidence.get("problems") or []
    if problems:
        return f"{op} reported: " + "; ".join(str(p) for p in problems)
    return f"{op} did not succeed"


def _version() -> str:
    from .. import __version__

    return __version__


def summarise(result: RunResult) -> str:
    """One line for the CLI: what ran, and what it wrote."""
    steps = result.manifest["steps"]
    ran = sum(1 for step in steps if step["status"] != "not_run")
    if result.ok:
        return f"{ran} step(s) ok → {result.manifest_path}"
    return (
        f"step {result.failed_step} ({steps[result.failed_step - 1]['op']}) failed after "
        f"{ran - 1} ok step(s) → {result.manifest_path}"
    )
