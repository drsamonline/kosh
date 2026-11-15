# Recipes — `kosh run recipe.yaml`

heavenzip chains *binaries* globally. Kosh now chains a *local* job: a recipe
file names an ordered list of steps, each step names an existing kosh operation
and its arguments, and the run records what every step was asked to do and what
it produced.

```bash
kosh run recipe.yaml                       # manifest next to the recipe
kosh run recipe.yaml --manifest run.json   # somewhere else
kosh run recipe.yaml --json                # the manifest on stdout
```

Roadmap item **EN-5**. The exit test it was written against: *a recipe file
reproduces a multi-step run and records each step's evidence.*

## The file

```yaml
version: 1
name: shrink-then-prove
description: optional, and only for humans

steps:
  - op: optimize
    name: re-deflate                      # optional label, used in the output
    paths: [data]
    level: 2

  - op: pack
    paths: [data]
    out: build/bundle.kosh
    codec: zstd

  - op: verify
    target: build/bundle.kosh
```

Top-level keys: `version` (required, must be `1`), `name`, `description`,
`steps` (required, a non-empty list). Each step needs `op`, and every key that is
not `op` or `name` is an argument to that operation.

A step that uses an engine looks like any other pack step — the engine is an
argument, not a different command:

```yaml
  - op: pack
    paths: [data/big.bin]
    out: build/big.dr
    engine: ziptastic        # native | ziptastic | heavenzip
```

## The format is a narrow subset of YAML, on purpose

Kosh has no YAML dependency and this feature was not a reason to add one. The
parser in `kosh/core/recipe.py` reads a small, documented subset and **refuses
everything else, naming the file and line**. A half-supported YAML is worse than a
small honest one: it silently reinterprets the file you wrote.

Supported:

- indented `key: value` mappings — a colon followed by a space
- `- ` block sequences
- bare, single-quoted and double-quoted scalars
- integers, `true`/`false`, `null`/`~`
- inline lists of scalars: `[data, build]`
- `#` comments, on their own line or after whitespace

Refused, with a line number:

- flow mappings `{...}` (write indented keys)
- anchors, aliases and tags (`&a`, `*a`, `!tag`)
- block scalars (`|`, `>`) and multi-line scalars
- tabs as indentation
- duplicate keys, and more than one document per file

The refusal list is the constant `RECIPE_SUBSET` in `kosh/core/recipe.py`, and
`tests/test_recipe.py` asserts each refusal actually refuses.

## Operations a step may name

Every one of these is an existing kosh operation; a recipe adds no new
compression, no new container and no new verification rule.

| `op` | required | optional | what it does |
| --- | --- | --- | --- |
| `optimize` | `paths` | `level`, `strip`, `dry_run`, `backup`, `force`, `jobs`, `recursive` | `kosh optimize` |
| `pack` | `paths`, `out` | `codec`, `level`, `block`, `threads`, `engine` | `kosh pack` |
| `unpack` | `container` | `dest`, `only`, `verify`, `engine`, `max_size` | `kosh unpack` |
| `verify` | `target` | `quick`, `jobs` | `kosh verify` |
| `split` | `path` | `size`, `parts`, `out`, `prefix`, `algo` | `kosh split` |
| `join` | `target` | `out`, `verify`, `overwrite` | `kosh join` |
| `ls` | `target` | — | `kosh ls` |
| `info` | `container` | — | `kosh info` |

An operation's arguments are checked while the recipe is read: a missing required
argument, an argument the operation does not take (a typo like `ou:`), or a value
of the wrong type is refused before any step runs. `pack` with an `engine` also
refuses `codec`, `level`, `block` and `threads`, because the engine chooses its
own codec and block size — an argument that cannot take effect is a mistake, not
a step to attempt.

Not available as steps, and why: `scan`, `doctor`, `bench`, `ui`, `engines` and
`archive` are not pipeline stages (two are interactive or diagnostic, and the
others are a different vocabulary for the same files). `scan` in particular lives
in the CLI rather than in a core function, and moving it is a separate change.

## Paths

A relative path in a recipe is resolved against **the recipe's own directory**,
not the shell's working directory: a recipe reproduces the same run wherever it is
invoked from. `--cwd DIR` overrides that base. Absolute paths are left alone.

## The manifest

Written next to the recipe as `<recipe>.manifest.json`, or wherever `--manifest`
points. JSON, because it is evidence to be read by a machine as well as a person:

```json
{
  "kosh_version": "1.1.0",
  "recipe": {"path": "…/recipe.yaml", "name": "shrink-then-prove",
             "description": "", "version": 1, "steps": 3},
  "base_dir": "…",
  "started": "2026-11-15T07:32:09Z",
  "finished": "2026-11-15T07:32:09Z",
  "seconds": 0.142,
  "ok": true,
  "failed_step": null,
  "failure": null,
  "steps": [
    {
      "index": 1,
      "op": "optimize",
      "name": "re-deflate",
      "args": {"paths": ["data"], "level": 2},
      "status": "ok",
      "exit_code": 0,
      "seconds": 0.131,
      "evidence": {"before": 132, "after": 90, "changed": 1, "files": ["…"]},
      "outputs": [
        {"path": "data/notes.txt.gz", "kind": "file", "size": 90, "sha256": "f8bd…"}
      ],
      "error": null
    }
  ]
}
```

Per step:

- `op`, `name` — what the recipe said to do
- `args` — **as the recipe wrote them**, not as they resolved, so the manifest is
  evidence about the recipe rather than about the machine that ran it
- `status` — `ok`, `failed`, or `not_run` for a step the run never reached
- `exit_code` — `0`, or `1` for a failed step
- `evidence` — what the operation itself reported: sizes and savings for
  `optimize`, entries and ratio for `pack`, the verification result for `verify`
- `outputs` — the files the step produced, each with `size` and `sha256` (a
  directory is recorded as its file count and total size instead)
- `error` — the message, when the step failed

`exit_code` is the status the step resolved to, **not a child process's return
code**: steps run in-process, through the same core functions the CLI dispatches
to. Nothing is spawned.

## Failure

The first failing step stops the run. `kosh run` exits non-zero, the manifest is
still written (a failed run is the one whose evidence matters most), `ok` is
`false`, `failed_step` names the step, and every step after it is recorded as
`not_run`. A `verify` step that finds damage counts as a failure — reporting it as
a success because the function returned would be the pretence this project
refuses everywhere else.

A recipe that cannot be read — unknown operation, missing argument, malformed
line — is refused *before* the first step runs, exits non-zero, and writes no
manifest: there is nothing to record evidence about.

## An example, committed

[`examples/recipes/optimize-pack-verify.yaml`](../examples/recipes/optimize-pack-verify.yaml)
runs the shape the roadmap names over a small sample, and is the recipe CI runs:

```bash
kosh run examples/recipes/optimize-pack-verify.yaml
kosh run examples/recipes/optimize-pack-verify.yaml --json | head -20
```
