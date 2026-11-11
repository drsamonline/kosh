"""Local web interface for Kosh.

A single-page front end over the same engine the CLI uses: upload or point at
files, pick an operation, watch it run, download the result.  No cloud, no
telemetry — the server binds to localhost and keeps every byte on disk.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import threading
import time
import uuid
import webbrowser
import zipfile
from dataclasses import dataclass, field
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..core import archive as archive_mod
from ..core import optimize as optimize_mod
from ..core import pack as pack_mod
from ..core import splitjoin
from ..core import verify as verify_mod
from ..core.util import KoshError, human_size, parse_size

STATIC = os.path.join(os.path.dirname(__file__), "static")
WORKROOT = os.path.join(os.environ.get("KOSH_HOME", os.path.expanduser("~/.kosh")), "sessions")

app = FastAPI(title="Kosh", version=__version__, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@dataclass
class Job:
    id: str
    op: str
    status: str = "queued"          # queued | running | done | error
    progress: float = 0.0
    message: str = ""
    result: dict = field(default_factory=dict)
    files: list[dict] = field(default_factory=list)
    error: str = ""
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None

    def snapshot(self) -> dict:
        return {
            "id": self.id, "op": self.op, "status": self.status,
            "progress": round(self.progress, 3), "message": self.message,
            "result": self.result, "files": self.files, "error": self.error,
            "seconds": round((self.finished or time.time()) - self.started, 2),
        }


JOBS: dict[str, Job] = {}
LOCK = threading.Lock()
SESSION_TTL = 6 * 3600


def _session_dir(token: str) -> str:
    if not token or "/" in token or ".." in token:
        raise HTTPException(400, "bad session")
    path = os.path.join(WORKROOT, token)
    if not os.path.isdir(path):
        raise HTTPException(404, "session expired; upload again")
    return path


def _clean_old_sessions() -> None:
    cutoff = time.time() - SESSION_TTL
    if not os.path.isdir(WORKROOT):
        return
    for name in os.listdir(WORKROOT):
        path = os.path.join(WORKROOT, name)
        try:
            if os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


# --------------------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    with open(os.path.join(STATIC, "index.html"), "r", encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


# --------------------------------------------------------------------------- upload
@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...),
                 paths: str = Form("[]")) -> dict:
    _clean_old_sessions()
    token = uuid.uuid4().hex[:12]
    root = os.path.join(WORKROOT, token, "in")
    os.makedirs(root, exist_ok=True)
    try:
        names = json.loads(paths) if paths else []
    except json.JSONDecodeError:
        names = []

    stored = []
    for index, item in enumerate(files):
        rel = names[index] if index < len(names) else item.filename
        rel = os.path.normpath(rel or item.filename).replace("\\", "/")
        if rel.startswith("../") or rel.startswith("/"):
            rel = os.path.basename(rel)
        target = os.path.join(root, rel)
        os.makedirs(os.path.dirname(target) or root, exist_ok=True)
        with open(target, "wb") as out:
            while True:
                chunk = await item.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        stored.append({"name": rel, "size": os.path.getsize(target)})

    return {"token": token, "files": stored, "root": root}


# --------------------------------------------------------------------------- run
def _progress(job: Job):
    def report(done: int, total: int, message: str) -> None:
        if total:
            job.progress = min(1.0, done / total)
        job.message = message
    return report


def _zip_dir(src: str, dest_zip: str) -> None:
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(src):
            for name in files:
                full = os.path.join(root, name)
                zf.write(full, os.path.relpath(full, src))


def _collect_outputs(job: Job, out_dir: str, single: Optional[str] = None) -> None:
    """Expose whatever the operation produced as downloadable files."""
    files = []
    if single and os.path.isfile(single):
        files.append({"name": os.path.basename(single), "size": os.path.getsize(single),
                      "path": single})
    elif os.path.isdir(out_dir):
        entries = [os.path.join(r, f) for r, _d, fs in os.walk(out_dir) for f in fs]
        if len(entries) == 1:
            files.append({"name": os.path.basename(entries[0]),
                          "size": os.path.getsize(entries[0]), "path": entries[0]})
        elif entries:
            bundle = os.path.join(os.path.dirname(out_dir), "kosh-result.zip")
            _zip_dir(out_dir, bundle)
            files.append({"name": "kosh-result.zip", "size": os.path.getsize(bundle),
                          "path": bundle})
    for item in files:
        item["url"] = f"/api/download/{job.id}/{uuid.uuid4().hex[:6]}"
    job.files = files
    with LOCK:
        _DOWNLOAD_MAP[job.id] = {item["url"].rsplit("/", 1)[-1]: item["path"] for item in files}


_DOWNLOAD_MAP: dict[str, dict[str, str]] = {}


def _run_job(job: Job, session: str, op: str, params: dict) -> None:
    in_root = os.path.join(session, "in")
    out_root = os.path.join(session, "out", job.id)
    os.makedirs(out_root, exist_ok=True)
    progress = _progress(job)
    job.status = "running"

    try:
        if op == "optimize":
            targets = []
            for root, _dirs, files in os.walk(in_root):
                for name in sorted(files):
                    targets.append(os.path.join(root, name))
            if not targets:
                raise KoshError("no files uploaded")
            # Optimise copies, so the user can compare before downloading.
            mirrored = os.path.join(out_root, "optimised")
            for path in targets:
                rel = os.path.relpath(path, in_root)
                dest = os.path.join(mirrored, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(path, dest)
            copies = [os.path.join(mirrored, os.path.relpath(p, in_root)) for p in targets]
            results = optimize_mod.optimize_many(
                copies, level=int(params.get("level", 2)),
                strip=bool(params.get("strip", False)),
                jobs=max(1, min(4, os.cpu_count() or 1)),
                progress=progress,
            )
            before = sum(r.before for r in results)
            after = sum(r.after for r in results)
            job.result = {
                "files": [r.as_dict() for r in results],
                "before": before, "after": after, "saved": before - after,
                "percent": round((before - after) * 100 / before, 2) if before else 0,
            }
            _collect_outputs(job, mirrored)

        elif op == "pack":
            files = [f for f in params.get("files") or []]
            inputs = [os.path.join(in_root, f) for f in files] or [in_root]
            for path in inputs:
                if not os.path.exists(path):
                    raise KoshError(f"missing upload: {path}")
            out = os.path.join(out_root, params.get("name") or "archive.kosh")
            level = params.get("level")
            level = int(level) if level not in (None, "", "auto") else None
            result = pack_mod.pack(inputs, out, codec=params.get("codec", "zstd"),
                                   level=level,
                                   block_size=parse_size(params.get("block", "8M")),
                                   progress=progress)
            job.result = {"raw_size": result.raw_size, "stored_size": result.stored_size,
                          "ratio": round(result.ratio, 4), "entries": result.entries,
                          "codec": result.codec}
            _collect_outputs(job, out_root, single=out)

        elif op == "unpack":
            archive = _single_input(in_root)
            dest = os.path.join(out_root, "extracted")
            result = pack_mod.unpack(archive, dest, progress=progress)
            job.result = {"entries": result.entries, "bytes": result.bytes}
            _collect_outputs(job, dest)

        elif op == "split":
            source = _single_input(in_root)
            parts_dir = os.path.join(out_root, "parts")
            mode = params.get("mode", "size")
            result = splitjoin.split(
                source, parts_dir,
                part_size=params.get("size") if mode == "size" else None,
                parts=int(params["parts"]) if mode == "parts" and params.get("parts") else None,
                progress=progress,
            )
            job.result = {"parts": len(result.parts), "part_size": result.part_size,
                          "total": result.total, "sha256": result.sha256}
            _collect_outputs(job, parts_dir)

        elif op == "join":
            result = splitjoin.join(in_root, os.path.join(out_root, "joined"), progress=progress)
            job.result = {"output": os.path.basename(result.output), "total": result.total,
                          "verified": result.verified}
            _collect_outputs(job, out_root, single=result.output)

        elif op == "archive":
            inputs = [in_root]
            out = os.path.join(out_root, params.get("name") or "archive.zip")
            result = archive_mod.create(out, inputs, level=int(params.get("level", 9)),
                                        progress=progress)
            job.result = {"format": result.fmt, "entries": result.entries,
                          "raw_size": result.raw_size, "stored_size": result.stored_size}
            _collect_outputs(job, out_root, single=out)

        elif op == "verify":
            target = _single_input(in_root)
            result = verify_mod.verify(target, deep=not bool(params.get("quick")))
            job.result = {
                "kind": result["kind"], "entries": result["entries"],
                "bytes": result["bytes"], "ok": result["ok"],
                "manifest_verified": result.get("manifest_verified"),
                "problems": result["problems"],
            }
            if not result["ok"]:
                raise KoshError("integrity check failed: " + "; ".join(result["problems"]))
            _collect_outputs(job, out_root)

        elif op == "inspect":
            target = _single_input(in_root)
            try:
                info = archive_mod.list_archive(target)
                job.result = {
                    "kind": f"{info['format']} archive", "entries": len(info["entries"]),
                    "stored_size": info["stored_size"], "raw_size": info["raw_size"],
                    "listing": info["entries"][:200],
                }
            except KoshError:
                head = open(target, "rb").read(1 << 16)
                kind = optimize_mod.sniff(head)
                job.result = {"kind": kind, "stored_size": os.path.getsize(target)}
            _collect_outputs(job, out_root)
        else:
            raise KoshError(f"unknown operation {op!r}")

        job.status = "done"
        job.progress = 1.0
        job.message = "finished"
    except KoshError as exc:
        job.status = "error"
        job.error = str(exc)
    except Exception as exc:  # pragma: no cover - defensive
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
    finally:
        job.finished = time.time()


def _single_input(root: str) -> str:
    entries = [os.path.join(r, f) for r, _d, fs in os.walk(root) for f in fs]
    if not entries:
        raise KoshError("no files uploaded")
    if len(entries) > 1:
        # A split set is many files but one logical input; prefer a manifest.
        manifests = [e for e in entries if e.endswith(splitjoin.MANIFEST_SUFFIX)]
        if manifests:
            return manifests[0]
        archives = [e for e in entries if e.lower().endswith((".kosh", ".zip", ".tar", ".gz",
                                                              ".tgz", ".zst", ".xz"))]
        if len(archives) == 1:
            return archives[0]
        raise KoshError("upload a single file for this operation")
    return entries[0]


@app.post("/api/run")
async def run(payload: dict) -> dict:
    token = payload.get("token", "")
    session = _session_dir(token)
    op = payload.get("op", "")
    params = payload.get("params") or {}
    job = Job(id=uuid.uuid4().hex[:10], op=op)
    with LOCK:
        JOBS[job.id] = job
    thread = threading.Thread(target=_run_job, args=(job, session, op, params), daemon=True)
    thread.start()
    return {"id": job.id}


@app.get("/api/job/{job_id}")
def job_status(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return job.snapshot()


@app.get("/api/download/{job_id}/{key}")
def download(job_id: str, key: str) -> FileResponse:
    path = _DOWNLOAD_MAP.get(job_id, {}).get(key)
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "file is no longer available")
    return FileResponse(path, filename=os.path.basename(path))


# --------------------------------------------------------------------------- serve
def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    import uvicorn

    url = f"http://{host}:{port}/"
    print(f"Kosh {__version__} — {url}")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
