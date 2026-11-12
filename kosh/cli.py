"""Kosh command line interface.

One binary, one vocabulary: split, join, pack, unpack, archive, optimize, scan,
ui.  Every command streams, verifies and reports what it actually saved.
"""

from __future__ import annotations

import json as jsonlib
import os
import sys
from functools import wraps
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn, DownloadColumn, Progress, SpinnerColumn, TextColumn,
    TimeElapsedColumn, TransferSpeedColumn,
)
from rich.table import Table

from . import __version__
from .core import archive as archive_mod
from .core import bench as bench_mod
from .core import doctor as doctor_mod
from .core import engine as engine_mod
from .core import optimize as optimize_mod
from .core import pack as pack_mod
from .core import splitjoin
from .core import verify as verify_mod
from .core.util import KoshError, human_size, parse_size

def _utf8_output() -> None:
    """Make stdout and stderr UTF-8 before anything is printed.

    The UI is UTF-8 — a tick, a cross, an arrow — and a Windows console defaults to
    a legacy codepage where those are not encodable, so `kosh doctor` died with
    `UnicodeEncodeError: 'charmap' codec can't encode character '\u2713'`. Found by
    running the suite on windows-latest (CT-1); reproducible anywhere with
    `PYTHONIOENCODING=cp1252`.

    `errors="replace"` rather than strict: a console that cannot render a glyph
    should print a placeholder, not end the command.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # a captured stream in a test harness
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - stream already detached
            pass


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help=(
        "Kosh — one toolkit for splitting, squeezing, archiving and inspecting files.\n\n"
        "Built as a modern replacement for DeflOpt, ECT, WABBIE, SplitSat and WinRK."
    ),
)
archive_app = typer.Typer(no_args_is_help=True, help="Create, list and extract archives.")
app.add_typer(archive_app, name="archive")

console = Console()
err_console = Console(stderr=True)


def handle(fn):
    """Turn KoshError into a clean message and a non-zero exit code."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except KoshError as exc:
            err_console.print(f"[bold red]error[/] {exc}")
            raise typer.Exit(1)
        except KeyboardInterrupt:
            err_console.print("\n[dim]interrupted[/]")
            raise typer.Exit(130)
    return wrapper


class RichProgress:
    """Progress callback adapter for the core engine."""

    def __init__(self, label: str):
        self.label = label
        self.progress = Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=None, complete_style="cyan", finished_style="green"),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeElapsedColumn(),
            console=console,
        )
        self.task_id = None

    def __enter__(self):
        self.progress.start()
        self.task_id = self.progress.add_task(self.label, total=None)
        return self

    def __exit__(self, *exc):
        self.progress.stop()

    def __call__(self, done: int, total: int, message: str):
        self.progress.update(self.task_id, completed=done,
                             total=total or None, description=message or self.label)


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        console.print_json(jsonlib.dumps(payload, default=str))


# --------------------------------------------------------------------------- split / join
@app.command()
@handle
def split(
    path: str = typer.Argument(..., help="File to split."),
    size: Optional[str] = typer.Option(None, "--size", "-s", help="Maximum part size, e.g. 700M, 4G."),
    parts: Optional[int] = typer.Option(None, "--parts", "-n", help="Split into exactly this many parts."),
    out: Optional[str] = typer.Option(None, "--out", "-o", help="Directory for the parts."),
    prefix: Optional[str] = typer.Option(None, "--prefix", "-p", help="Part filename prefix."),
    algo: str = typer.Option("sha256", "--algo", help="Checksum algorithm."),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
):
    """Split a file into verifiable parts (replaces SplitSat)."""
    if quiet or as_json:
        result = splitjoin.split(path, out, size, parts, prefix, algo)
    else:
        with RichProgress(f"splitting {os.path.basename(path)}") as prog:
            result = splitjoin.split(path, out, size, parts, prefix, algo, progress=prog)

    payload = {
        "manifest": result.manifest, "parts": result.parts, "part_size": result.part_size,
        "total": result.total, "sha256": result.sha256,
    }
    if as_json:
        _emit(payload, True)
        return
    table = Table(title="Split complete", show_header=True, header_style="bold cyan")
    table.add_column("Field")
    table.add_column("Value", overflow="fold")
    table.add_row("source", f"{human_size(result.total)}")
    table.add_row("parts", f"{len(result.parts)} × {human_size(result.part_size)}")
    table.add_row("manifest", result.manifest)
    table.add_row("sha256", result.sha256)
    console.print(table)


@app.command()
@handle
def join(
    target: str = typer.Argument(..., help="A part, a manifest, or a directory of parts."),
    out: Optional[str] = typer.Option(None, "--out", "-o", help="Output file."),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip checksum verification."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing output file."),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Reassemble split parts and prove the result matches the original."""
    if quiet or as_json:
        result = splitjoin.join(target, out, verify=not no_verify, overwrite=force)
    else:
        with RichProgress("joining parts") as prog:
            result = splitjoin.join(target, out, verify=not no_verify, overwrite=force, progress=prog)
    payload = {"output": result.output, "total": result.total,
               "sha256": result.sha256, "verified": result.verified}
    if as_json:
        _emit(payload, True)
        return
    mark = "[green]verified[/]" if result.verified else "[yellow]unverified[/]"
    console.print(f"[green]joined[/] {result.output} — {human_size(result.total)}, {mark}")


@app.command("parts")
@handle
def parts_cmd(
    target: str = typer.Argument(..., help="Manifest, part or directory."),
    as_json: bool = typer.Option(False, "--json"),
):
    """Show what a split set contains and whether every part is present."""
    info = splitjoin.inspect(target)
    if as_json:
        _emit(info, True)
        return
    table = Table(title=f"{info['source']} — {human_size(info['total'])}", header_style="bold cyan")
    for col in ("part", "size", "present"):
        table.add_column(col)
    for record in info["parts"]:
        table.add_row(record["name"], human_size(record["size"]),
                      "[green]yes[/]" if record["present"] else "[red]missing[/]")
    console.print(table)


# --------------------------------------------------------------------------- pack / unpack
@app.command()
@handle
def pack(
    paths: list[str] = typer.Argument(..., help="Files or directories to pack."),
    out: str = typer.Option(..., "--out", "-o", help="Output .kosh file."),
    codec: str = typer.Option("zstd", "--codec", "-c", help="zstd | brotli | lzma | store"),
    level: Optional[int] = typer.Option(None, "--level", "-l", help="Codec level (higher = smaller)."),
    block: str = typer.Option("8M", "--block", "-b", help="Block size, e.g. 8M."),
    threads: Optional[int] = typer.Option(None, "--threads", "-t", help="Worker threads."),
    engine: str = typer.Option("native", "--engine", "-e",
                               help="native | ziptastic | heavenzip (CHCE engines, spawned)."),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Pack files or folders into a verified .kosh container (replaces WABBIE)."""
    block_size = parse_size(block)
    if engine != "native":
        if quiet or as_json:
            info = engine_mod.pack_with_engine(engine, paths, out)
        else:
            with RichProgress(f"{engine} → {os.path.basename(out)}") as prog:
                info = engine_mod.pack_with_engine(engine, paths, out, progress=prog)
        if as_json:
            _emit(info, True)
            return
        console.print(f"[green]packed[/] with {info['engine']} → {info['output']} "
                      f"({human_size(info['stored_size'])})")
        return
    if quiet or as_json:
        result = pack_mod.pack(paths, out, codec, level, block_size, threads)
    else:
        with RichProgress(f"packing into {os.path.basename(out)}") as prog:
            result = pack_mod.pack(paths, out, codec, level, block_size, threads, progress=prog)

    payload = {
        "output": result.output, "entries": result.entries, "codec": result.codec,
        "raw_size": result.raw_size, "stored_size": result.stored_size,
        "ratio": round(result.ratio, 4), "seconds": round(result.seconds, 2),
    }
    if as_json:
        _emit(payload, True)
        return
    console.print(
        f"[green]packed[/] {result.entries} file(s) → {result.output}\n"
        f"  {human_size(result.raw_size)} → {human_size(result.stored_size)} "
        f"({result.ratio * 100:.1f}% of original) in {result.seconds:.1f}s"
    )


@app.command()
@handle
def unpack(
    container: str = typer.Argument(..., help="A .kosh container, or a CHCE stream with --engine."),
    dest: str = typer.Option(".", "--out", "-o", help="Destination directory."),
    only: Optional[list[str]] = typer.Option(None, "--only", help="Extract just these entries (repeatable)."),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip per-file checksum verification."),
    engine: str = typer.Option("auto", "--engine", "-e",
                               help="auto | native | ziptastic | heavenzip. auto detects a CHCE stream."),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Extract a .kosh container, verifying every file as it lands.

    A stream written by `pack --engine ziptastic` is a CHCE stream, not a
    container. With `--engine auto` (the default) it is decoded by the engine that
    wrote it; `--engine native` refuses it.
    """
    with open(container, "rb") as fh:
        head = fh.read(64)
    is_chce = head.startswith(engine_mod.CHCE_MAGIC)

    if is_chce:
        if engine == "native":
            raise KoshError(
                "this is a CHCE stream, not a Kosh container; unpack it with "
                "--engine ziptastic (or run `ziptastic d` yourself)"
            )
        chosen = "ziptastic" if engine == "auto" else engine
        result = engine_mod.unpack_with_engine(chosen, container, dest)
        if as_json:
            _emit(result, True)
            return
        console.print(
            f"[green]unpacked[/] with {result['engine']} → {result['output']} "
            f"({human_size(result['bytes'])}, {result['block_count']} block(s))"
        )
        return

    if engine not in ("auto", "native"):
        raise KoshError(
            f"--engine {engine} is for CHCE streams; {os.path.basename(container)} is a Kosh container"
        )

    if quiet or as_json:
        result = pack_mod.unpack(container, dest, only, verify=not no_verify)
    else:
        with RichProgress(f"unpacking {os.path.basename(container)}") as prog:
            result = pack_mod.unpack(container, dest, only, verify=not no_verify, progress=prog)
    payload = {"output_dir": result.output_dir, "entries": result.entries,
               "bytes": result.bytes, "verified": result.verified, "skipped": result.skipped}
    if as_json:
        _emit(payload, True)
        return
    console.print(f"[green]unpacked[/] {result.entries} file(s), {human_size(result.bytes)} → {result.output_dir}")
    for note in result.skipped:
        console.print(f"  [yellow]skipped[/] {note}")


@app.command("ls")
@handle
def ls(
    target: str = typer.Argument(..., help="A .kosh, .zip or .tar[.gz|.zst] archive."),
    as_json: bool = typer.Option(False, "--json"),
):
    """List the contents of any archive Kosh understands.

    A CHCE stream is not an archive: it holds one payload and a block table, not
    named entries. `kosh info` shows the blocks; extracting is `kosh unpack -e
    ziptastic`.
    """
    with open(target, "rb") as fh:
        head = fh.read(64)
    if head.startswith(engine_mod.CHCE_MAGIC):
        raise KoshError(
            f"{os.path.basename(target)} is a CHCE stream, not an archive: one payload and a "
            "block table, no named entries. `kosh info` shows the blocks; `kosh unpack "
            "--engine ziptastic` extracts it."
        )

    info = archive_mod.list_archive(target)
    if as_json:
        _emit(info, True)
        return
    table = Table(title=f"{os.path.basename(target)} — {info['format']}, "
                        f"{human_size(info['stored_size'])} on disk", header_style="bold cyan")
    for col in ("name", "size", "type"):
        table.add_column(col, overflow="fold")
    for entry in info["entries"]:
        table.add_row(entry["name"], human_size(entry.get("size", 0)), entry["type"])
    console.print(table)


# --------------------------------------------------------------------------- archive
@archive_app.command("create")
@handle
def archive_create(
    out: str = typer.Argument(..., help="Archive to write (.kosh, .zip, .tar.gz, .tar.zst)."),
    paths: list[str] = typer.Argument(..., help="Files or directories to add."),
    level: int = typer.Option(9, "--level", "-l", min=1, max=22),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Create an archive in the format its extension implies (replaces WinRK)."""
    if quiet or as_json:
        result = archive_mod.create(out, paths, level=level)
    else:
        with RichProgress(f"archiving into {os.path.basename(out)}") as prog:
            result = archive_mod.create(out, paths, level=level, progress=prog)
    payload = {"output": result.output, "format": result.fmt, "entries": result.entries,
               "raw_size": result.raw_size, "stored_size": result.stored_size}
    if as_json:
        _emit(payload, True)
        return
    console.print(f"[green]created[/] {result.output} ({result.fmt}) — "
                  f"{result.entries} file(s), {human_size(result.raw_size)} → "
                  f"{human_size(result.stored_size)}")


@archive_app.command("extract")
@handle
def archive_extract(
    archive: str = typer.Argument(..., help="Archive to extract."),
    dest: str = typer.Option(".", "--out", "-o"),
    only: Optional[list[str]] = typer.Option(None, "--only"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
):
    """Extract any supported archive, safely (no path traversal)."""
    if quiet:
        result = archive_mod.extract(archive, dest, only)
    else:
        with RichProgress(f"extracting {os.path.basename(archive)}") as prog:
            result = archive_mod.extract(archive, dest, only, progress=prog)
    console.print(f"[green]extracted[/] {result.entries} file(s), "
                  f"{human_size(result.bytes)} → {result.output_dir}")
    for note in result.skipped:
        console.print(f"  [yellow]skipped[/] {note}")


@archive_app.command("list")
@handle
def archive_list(archive: str = typer.Argument(...),
                 as_json: bool = typer.Option(False, "--json")):
    """List an archive's entries."""
    ls(archive, as_json)


# --------------------------------------------------------------------------- optimize
@app.command()
@handle
def optimize(
    paths: list[str] = typer.Argument(..., help="PNG, GZIP, ZIP or JPEG files, or directories."),
    level: int = typer.Option(2, "--level", "-l", min=1, max=3,
                              help="1 fast, 2 balanced, 3 max (slow)."),
    strip: bool = typer.Option(False, "--strip", help="Drop metadata (text chunks, EXIF, comments)."),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Report savings without writing."),
    backup: bool = typer.Option(False, "--backup", help="Keep a .bak copy of every rewritten file."),
    force: bool = typer.Option(False, "--force", "-f", help="Rewrite even when nothing is saved."),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", "-r"),
    jobs: int = typer.Option(1, "--jobs", "-j", min=1, max=16, help="Files to optimise in parallel."),
    as_json: bool = typer.Option(False, "--json"),
):
    """Re-compress PNG/GZIP/ZIP/JPEG losslessly (replaces DeflOpt and ECT)."""
    targets = _expand(paths, recursive)
    if not targets:
        raise KoshError("no supported files found")

    if as_json:
        results = optimize_mod.optimize_many(targets, level, strip, force, dry_run, backup, jobs)
    else:
        with RichProgress(f"optimising {len(targets)} file(s)") as prog:
            results = optimize_mod.optimize_many(targets, level, strip, force, dry_run, backup,
                                                 jobs, progress=prog)

    payload = {
        "files": [r.as_dict() for r in results],
        "before": sum(r.before for r in results),
        "after": sum(r.after for r in results),
        "changed": sum(1 for r in results if r.changed),
    }
    if as_json:
        _emit(payload, True)
        return

    table = Table(title="Optimisation report", header_style="bold cyan", expand=True)
    table.add_column("file", ratio=4, overflow="ellipsis", no_wrap=True)
    table.add_column("size", width=19, no_wrap=True)
    table.add_column("saved", justify="right", width=9, no_wrap=True)
    table.add_column("cut", justify="right", width=5, no_wrap=True)
    table.add_column("note", ratio=3, overflow="ellipsis", no_wrap=True)
    for r in results:
        if r.kind == "unknown" and not r.changed:
            table.add_row(os.path.basename(r.path), "[dim]not optimisable[/]", "-", "-",
                          "[dim]skipped[/]")
            continue
        note = r.note
        if dry_run and r.changed:
            note = "[yellow]dry run[/] " + note
        table.add_row(
            os.path.basename(r.path),
            f"{human_size(r.before)} → {human_size(r.after)}" if r.changed else human_size(r.before),
            f"−{human_size(r.saved)}" if r.saved > 0 else "-",
            f"{r.percent:.1f}%" if r.saved > 0 else "-",
            note,
        )
    console.print(table)
    total_before, total_after = payload["before"], payload["after"]
    console.print(
        f"[green]{payload['changed']} file(s) rewritten[/] · "
        f"{human_size(total_before)} → {human_size(total_after)} · "
        f"saved {human_size(total_before - total_after)} "
        f"({(total_before - total_after) * 100 / total_before if total_before else 0:.2f}%)"
    )


def _expand(paths: list[str], recursive: bool) -> list[str]:
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


# --------------------------------------------------------------------------- scan
@app.command()
@handle
def scan(
    path: str = typer.Argument(..., help="Any file: container, split set, or plain file."),
    as_json: bool = typer.Option(False, "--json"),
):
    """Identify a file and estimate how much smaller it could get."""
    import math

    if not os.path.exists(path):
        raise KoshError(f"no such file: {path}")
    if os.path.isdir(path):
        files = [os.path.join(r, f) for r, _d, fs in os.walk(path) for f in fs]
        total = sum(os.path.getsize(f) for f in files)
        payload = {"path": os.path.abspath(path), "kind": "directory",
                   "files": len(files), "size": total}
        if as_json:
            _emit(payload, True)
            return
        console.print(f"{path}: directory, {len(files)} files, {human_size(total)}")
        return

    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(1 << 16)
    payload: dict = {"path": os.path.abspath(path), "size": size}

    try:
        if head.startswith(engine_mod.CHCE_MAGIC):
            payload.update(engine_mod.read_chce_header(head))
        elif head.startswith(pack_mod.START_MAGIC):
            info = pack_mod.list_container(path)
            payload.update(kind="kosh-container", codec=info["codec"], entries=len(info["entries"]),
                           raw_size=info["raw_size"])
        elif path.endswith(splitjoin.MANIFEST_SUFFIX):
            info = splitjoin.inspect(path)
            payload.update(kind="kosh-split", parts=len(info["parts"]), raw_size=info["total"],
                           complete=info["complete"])
        else:
            kind = optimize_mod.sniff(head)
            payload.update(kind=kind if kind != "unknown" else "file")
            sample = head[: 1 << 16]
            counts = [0] * 256
            for byte in sample:
                counts[byte] += 1
            entropy = -sum((c / len(sample)) * math.log2(c / len(sample)) for c in counts if c)
            payload["entropy_bits_per_byte"] = round(entropy, 3)
            import zstandard
            probe = zstandard.ZstdCompressor(level=3).compress(sample)
            payload["compressible"] = len(probe) < len(sample) * 0.95
            payload["sample_ratio"] = round(len(probe) / len(sample), 3)
    except KoshError as exc:
        payload["note"] = str(exc)

    if as_json:
        _emit(payload, True)
        return

    table = Table(title=os.path.basename(path), header_style="bold cyan")
    table.add_column("field")
    table.add_column("value", overflow="fold")
    for key, value in payload.items():
        if key != "path":
            table.add_row(key, str(value))
    console.print(table)
    console.print(f"[dim]{payload['path']}[/]")


# --------------------------------------------------------------------------- verify / info
@app.command()
@handle
def verify(
    target: str = typer.Argument(..., help="A .kosh container, split manifest, zip or tar."),
    quick: bool = typer.Option(False, "--quick", help="Check structure only; do not decompress."),
    as_json: bool = typer.Option(False, "--json"),
):
    """Prove an artefact is intact, without extracting it."""
    result = verify_mod.verify(target, deep=not quick)
    if as_json:
        _emit(result, True)
        raise typer.Exit(0 if result["ok"] else 1)

    table = Table(title=os.path.basename(target), header_style="bold cyan")
    table.add_column("check")
    table.add_column("result", overflow="fold")
    table.add_row("type", result["kind"])
    table.add_row("entries", str(result["entries"]))
    table.add_row("bytes", human_size(result["bytes"]))
    if "manifest_verified" in result:
        table.add_row("manifest digest", "[green]verified[/]" if result["manifest_verified"]
                      else "[dim]not present (v1)[/]")
    # Say what was actually checked. "intact" from a hash nobody compared to
    # anything, or from a header that merely parsed, is the kind of assurance
    # this project does not give elsewhere.
    checked = result.get("checked")
    label = "intact" if result["ok"] else "damaged"
    if checked:
        label = f"{label} ({checked})"
    colour = "green" if result["ok"] else "red"
    table.add_row("integrity", f"[{colour}]{label}[/]")
    console.print(table)
    if result.get("note"):
        console.print(f"  [dim]{result['note']}[/]")
    for problem in result["problems"]:
        console.print(f"  [red]✗[/] {problem}")
    if not result["ok"]:
        raise typer.Exit(1)


@app.command()
@handle
def info(
    container: str = typer.Argument(..., help="A .kosh container."),
    as_json: bool = typer.Option(False, "--json"),
):
    """Show a container's header and physical block layout.

    A CHCE stream has the same question and a different answer: its layout is a
    header, blocks and a v8 trailer holding the block offsets. Roadmap EN-4.
    """
    with open(container, "rb") as fh:
        head = fh.read(64)
    if head.startswith(engine_mod.CHCE_MAGIC):
        header = engine_mod.read_chce_header(head)
        try:
            trailer = engine_mod.read_chce_trailer(container)
            blocks = engine_mod.read_blocks(container, trailer)
        except KoshError as exc:
            raise KoshError(f"{exc}") from exc

        payload = {
            "path": os.path.abspath(container),
            "kind": "chce-stream",
            "version": header["version"],
            "flags": header["flags"],
            "layers": header["layers"],
            "original_size": header["original_size"],
            "true_size": header["true_size"],
            "crc32": header["crc32"],
            "stored_size": os.path.getsize(container),
            "block_count": trailer["count"],
            "trailer_bytes": trailer["trailer_bytes"],
            "manifest_sha256": trailer["manifest_sha256"],
            "blocks": blocks,
        }
        if as_json:
            _emit(payload, True)
            return

        head_table = Table(title=os.path.basename(container), header_style="bold cyan", show_header=False)
        head_table.add_column("field")
        head_table.add_column("value", overflow="fold")
        for key in ("kind", "version", "flags", "layers", "original_size", "true_size", "crc32",
                    "stored_size", "block_count", "trailer_bytes"):
            value = payload[key]
            head_table.add_row(key, human_size(value) if key in ("original_size", "true_size", "stored_size") else str(value))
        head_table.add_row("manifest", (payload["manifest_sha256"] or "-")[:16] + "…")
        console.print(head_table)

        table = Table(title="blocks", header_style="bold cyan")
        for column in ("#", "offset", "compressed", "uncompressed", "mode", "digest"):
            table.add_column(column, overflow="fold")
        for block in blocks:
            table.add_row(
                str(block["index"]),
                f"0x{block['offset']:08x}",
                human_size(block["compressed_size"]),
                human_size(block["uncompressed_size"]),
                f"0x{block['mode']:02x}",
                block["digest"][:16] + "…",
            )
        console.print(table)
        return

    toc = pack_mod.read_toc(container)
    rows = pack_mod.block_index(container)
    payload = {
        "path": os.path.abspath(container), "codec": toc.get("codec"),
        "level": toc.get("level"), "block_size": toc.get("block_size"),
        "version": toc.get("version"), "created": toc.get("created"),
        "stored_size": os.path.getsize(container),
        "manifest_sha256": toc.get("manifest_sha256"),
        "entries": rows,
    }
    if as_json:
        _emit(payload, True)
        return
    head = Table(title=os.path.basename(container), header_style="bold cyan", show_header=False)
    head.add_column("field")
    head.add_column("value", overflow="fold")
    for key in ("codec", "level", "block_size", "version", "created", "stored_size"):
        value = payload[key]
        head.add_row(key, human_size(value) if key == "stored_size" else str(value))
    head.add_row("manifest", (payload["manifest_sha256"] or "-")[:16] + "…")
    console.print(head)

    table = Table(title="blocks", header_style="bold cyan")
    for column in ("entry", "size", "blocks", "first offset", "last offset"):
        table.add_column(column, overflow="fold")
    for row in rows:
        table.add_row(row["path"], human_size(row["size"]), str(row["blocks"]),
                      str(row["first_offset"]), str(row["last_offset"]))
    console.print(table)


# --------------------------------------------------------------------------- doctor / bench
@app.command()
@handle
def doctor(
    quick: bool = typer.Option(False, "--quick", help="Skip the slower codec probes."),
    as_json: bool = typer.Option(False, "--json"),
):
    """Check this installation, including a live round-trip."""
    report = doctor_mod.run(quick=quick)
    if as_json:
        _emit(report, True)
        raise typer.Exit(0 if report["ok"] else 1)

    table = Table(title=f"kosh doctor — {report['platform']}", header_style="bold cyan")
    table.add_column("check")
    table.add_column("", width=3)
    table.add_column("detail", overflow="fold")
    for check in report["checks"]:
        mark = "[green]✓[/]" if check["ok"] else ("[red]✗[/]" if check["fatal"] else "[yellow]–[/]")
        table.add_row(check["name"], mark, check["detail"])
    console.print(table)
    if not report["ok"]:
        console.print("[red]fatal problems found[/]")
        raise typer.Exit(1)
    console.print("[green]ready[/]")


@app.command()
@handle
def bench(
    paths: Optional[list[str]] = typer.Argument(None, help="Files to measure (default: a generated corpus)."),
    images: bool = typer.Option(False, "--images", help="Include a generated PNG."),
    markdown_out: Optional[str] = typer.Option(None, "--markdown", "-m",
                                               help="Write a markdown table to this file."),
    as_json: bool = typer.Option(False, "--json"),
):
    """Measure every codec against gzip -9, xz -9 and zip -9."""
    report = bench_mod.run(list(paths) if paths else None, include_images=images)
    if as_json:
        _emit(report, True)
        return

    for item in report["results"]:
        table = Table(
            title=f"{item['file']} — {human_size(item['raw'])} (seed {report['seed']})",
            header_style="bold cyan",
        )
        table.add_column("codec")
        table.add_column("stored", justify="right")
        table.add_column("ratio", justify="right")
        table.add_column("speed", justify="right")
        table.add_column("", width=6, no_wrap=True)
        for row in item["rows"]:
            table.add_row(
                row["codec"], human_size(row["stored"]), f"{row['ratio'] * 100:.1f}%",
                f"{row['speed_mbps']} MB/s",
                "[green]best[/]" if row["codec"] == item["best"] else "",
            )
        console.print(table)

    winners = ", ".join(f"{i['file']} → {i['best']}" for i in report["results"] if i["best"])
    console.print(f"[dim]best per file:[/] {winners}")
    if markdown_out:
        with open(markdown_out, "w", encoding="utf-8") as fh:
            fh.write(bench_mod.markdown(report) + "\n")
        console.print(f"[green]wrote[/] {markdown_out}")


# --------------------------------------------------------------------------- ui
@app.command()
@handle
def ui(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(8765, "--port", "-p", help="Port."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open a browser window."),
):
    """Launch the local web interface."""
    try:
        from .ui.server import serve
    except ImportError as exc:  # pragma: no cover
        raise KoshError(f"the web UI needs extra packages: {exc}") from exc
    serve(host=host, port=port, open_browser=open_browser)


@app.command()
def version():
    """Print the version."""
    console.print(f"kosh {__version__}")


def main() -> None:  # pragma: no cover
    _utf8_output()
    try:
        app()
    except KoshError as exc:
        err_console.print(f"[bold red]error[/] {exc}")
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
