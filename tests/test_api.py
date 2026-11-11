"""The web API must drive the same engine as the CLI."""

from __future__ import annotations

import io
import os
import time

import pytest
from fastapi.testclient import TestClient

from kosh.ui.server import app

client = TestClient(app)


def wait(job_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = client.get(f"/api/job/{job_id}").json()
        if snapshot["status"] in ("done", "error"):
            return snapshot
        time.sleep(0.15)
    raise AssertionError("job did not finish")


def upload(files: dict[str, bytes]) -> dict:
    payload = [{"files": (name, io.BytesIO(data))} for name, data in files.items()]
    # TestClient wants one list under the same key
    body = []
    for name, data in files.items():
        body.append(("files", (name, data, "application/octet-stream")))
    body.append(("paths", ("", __import__("json").dumps(list(files)), "text/plain")))
    response = client.post("/api/upload", files=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_health_and_index():
    assert client.get("/api/health").json()["ok"] is True
    page = client.get("/")
    assert page.status_code == 200 and "Kosh" in page.text


def test_split_then_join_via_api(tmp_path):
    data = os.urandom(400_000)
    session = upload({"payload.bin": data})
    job = client.post("/api/run", json={
        "token": session["token"], "op": "split",
        "params": {"mode": "size", "size": "100K"},
    }).json()
    done = wait(job["id"])
    assert done["status"] == "done", done
    assert done["result"]["parts"] == 4
    assert done["files"], "no downloadable output"


def test_pack_via_api(tmp_path):
    session = upload({"notes.txt": b"hello kosh\n" * 2000})
    job = client.post("/api/run", json={
        "token": session["token"], "op": "pack",
        "params": {"codec": "zstd", "level": "9", "name": "out.kosh"},
    }).json()
    done = wait(job["id"])
    assert done["status"] == "done", done
    assert done["result"]["stored_size"] < done["result"]["raw_size"]
    assert done["files"][0]["name"] == "out.kosh"


def test_optimize_via_api_returns_savings():
    from PIL import Image
    import numpy as np
    rng = np.random.default_rng(5)
    arr = np.zeros((120, 160, 3), dtype=np.uint8)
    arr[:, :, 0] = np.arange(160)[None, :]
    arr[:, :, 1] = np.arange(120)[:, None]
    arr[:, :, 2] = rng.integers(0, 40, size=(120, 160), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG", compress_level=1)

    session = upload({"picture.png": buf.getvalue()})
    job = client.post("/api/run", json={
        "token": session["token"], "op": "optimize", "params": {"level": "1"},
    }).json()
    done = wait(job["id"], timeout=120)
    assert done["status"] == "done", done
    assert done["result"]["percent"] > 0
    assert done["files"]


def test_bad_session_is_rejected():
    response = client.post("/api/run", json={"token": "nope", "op": "pack", "params": {}})
    assert response.status_code == 404
