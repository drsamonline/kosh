# The PyPI name is taken — publishing under `kosh` cannot work

Written 2026-11-18, while checking the release path. `CT-9` in the release workflow says the
tag path "uploads to PyPI", and `pyproject.toml` declares `name = "kosh"`. That name belongs to
someone else.

## The evidence

```
$ pip index versions kosh
kosh (3.6)
Available versions: 3.6, 3.5, 3.4, 3.3.0, 3.2.0.1, 3.1, 3.0.1, 3.0, 2.2, 2.1, 2.0, 1.2, 1.1, 1.0, 0.0.0
```

This project's `pyproject.toml` says `version = "1.1.0"`. The published `1.1` is not ours. Its
metadata, from the wheel itself:

```
Name: kosh
Version: 3.6
Summary: Manages Data Store with External Bulk Data
Author: Charles Doutriaux, Renee Olson
Project-URL: Homepage, https://github.com/LLNL/Kosh
```

That is **LLNL's Kosh**, a data-store tool from Lawrence Livermore National Laboratory. It has
been on PyPI since before this project existed and is eleven minor versions ahead of it.

## What that means

1. **`python -m build` succeeds, `twine check` succeeds, and the upload fails.** The failure is a
   403 from the index on a name the token does not own — which is exactly the "403 nobody can
   read" the workflow comment says it wants to avoid.
2. **Even with a token it cannot be published**, so `PYPI_API_TOKEN` being absent is the smaller
   of the two blockers.
3. **The name collision is a user-facing hazard, not just a technical one.** `pip install kosh`
   installs a national-lab data-store tool, and someone who has read this project's README will
   reasonably think it is this one.

## Decided: the distribution is `zipkosh` (2026-11-18)

The owner chose **`zipkosh`**. It is free on PyPI — `pip index versions zipkosh` finds no
distribution — and it keeps the `kosh` command, which is what the docs and the CLI use.

`pyproject.toml` now declares `name = "zipkosh"`; `[project.scripts]` still installs `kosh`, and
the import package is still `kosh`. So `pip install zipkosh` gives you the `kosh` command, which
is the usual arrangement for a project whose name was taken.

## The candidates that were considered

The distribution name has to change; the `kosh` **command** can stay, since that is what the CLI
installs and what the docs use. Candidates, in the order they read best:

| name | note |
|---|---|
| `kosh-chce` | keeps the command and the family name; states the CHCE connection |
| `chce-kosh` | matches the `chce` vocabulary the engine already uses |
| `kosh-cli` | describes what it is, says nothing about CHCE |

The change was `name` in `pyproject.toml`, the install line in `README.md`, and this document —
as expected, small.

## The state of the two items this was part of

- **Pages: done.** `gh api repos/drsamonline/kosh/pages` reports `status: built`,
  `html_url: https://drsamonline.github.io/kosh/`.
- **PyPI: blocked on the name**, and then on the token.
