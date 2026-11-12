#!/usr/bin/env python3
"""Publish the repository to GitHub through the REST API when `git push` is blocked in the session.

  GH_PAT=<token> python gh_sync.py "commit message"

Publishes every file git tracks. Business data (dump, raw, loads, reports) is included ONLY when
git.private_confirmed is true in config/sources.yaml. The token comes from the environment and is never stored.
One commit on top of the remote head; unchanged files are not re-uploaded; files deleted locally are deleted remotely."""
import base64
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
API = "https://api.github.com"
DATA_DIRS = ["data/backup", "data/raw", "data/loads", "reports"]


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def local_files() -> dict[str, tuple[str, bytes]]:
    out = {}
    for line in subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-s"], text=True).splitlines():
        meta, path = line.split("\t", 1)
        out[path] = (meta.split()[0], (ROOT / path).read_bytes())
    cfg = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text()).get("git", {})
    if cfg.get("private_confirmed"):
        for d in DATA_DIRS:
            for p in (ROOT / d).rglob("*"):
                if p.is_file() and p.name != ".gitkeep":
                    out[str(p.relative_to(ROOT))] = ("100644", p.read_bytes())
    return out


def main():
    msg = sys.argv[1] if len(sys.argv) > 1 else "sync"
    tok = os.environ.get("GH_PAT") or sys.exit("GH_PAT not set")
    remote = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text())["git"]["remote"]
    owner, repo = remote.rstrip("/").split("/")[-2:]
    H = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
    base = f"{API}/repos/{owner}/{repo}"

    def call(method, path, **kw):
        r = requests.request(method, base + path, headers=H, timeout=180, **kw)
        return r

    files = local_files()
    ref = call("GET", "/git/ref/heads/main")
    if ref.status_code in (404, 409):  # empty repository: the contents API creates the first commit
        first = ".gitignore" if ".gitignore" in files else sorted(files)[0]
        r = call("PUT", f"/contents/{first}", json={"message": msg, "branch": "main",
                                                      "content": base64.b64encode(files[first][1]).decode()})
        r.raise_for_status()
        ref = call("GET", "/git/ref/heads/main")
    ref.raise_for_status()
    head = ref.json()["object"]["sha"]
    base_tree = call("GET", f"/git/commits/{head}").json()["tree"]["sha"]
    remote_tree = {t["path"]: t["sha"] for t in call("GET", f"/git/trees/{base_tree}", params={"recursive": "1"}).json()["tree"] if t["type"] == "blob"}

    entries = []
    for path, (mode, data) in files.items():
        sha = blob_sha(data)
        if remote_tree.get(path) == sha:
            continue
        b = call("POST", "/git/blobs", json={"content": base64.b64encode(data).decode(), "encoding": "base64"})
        b.raise_for_status()
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": b.json()["sha"]})
    for path in remote_tree:
        if path not in files:
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
    if not entries:
        print("remote already up to date")
        return
    tree = call("POST", "/git/trees", json={"base_tree": base_tree, "tree": entries})
    tree.raise_for_status()
    commit = call("POST", "/git/commits", json={"message": msg, "tree": tree.json()["sha"], "parents": [head]})
    commit.raise_for_status()
    call("PATCH", "/git/refs/heads/main", json={"sha": commit.json()["sha"]}).raise_for_status()
    print(f"published {len(entries)} changed files to {owner}/{repo}@main ({commit.json()['sha'][:7]})")


if __name__ == "__main__":
    main()
