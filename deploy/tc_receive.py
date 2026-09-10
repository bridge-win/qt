#!/usr/bin/python3
"""Root-installed, forced-command SSH receiver for the TC research workbench.

Install outside the checkout. Never execute a deployment-supplied script as root.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

APP = Path("/opt/qt")
STATE = Path("/var/lib/qt-deploy")
CODE_DIRS = ("src", "scripts", "config", "packages")
CODE_FILES = ("pyproject.toml", "README.md")
SERVICES = ("qt-workbench-api", "qt-workbench-worker")
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024


def command(value: str) -> tuple[str, int]:
    match = re.fullmatch(r"deploy ([0-9a-f]{40}) ([1-9][0-9]{0,19})", value)
    if not match:
        raise ValueError("Only deploy <commit-sha> <run-id> is permitted")
    return match[1], int(match[2])


def allowed_path(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return False
    if any(part.startswith(".") for part in path.parts):
        return False
    return (
        path.parts[0] in CODE_DIRS
        or name in (*CODE_FILES, "release.json", "web", "web/dist")
        or name.startswith("web/dist/")
    )


def unpack(archive: Path, destination: Path, sha: str) -> None:
    total = 0
    seen: set[str] = set()
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle:
            name = member.name.rstrip("/")
            if not allowed_path(name) or name in seen:
                raise ValueError("Unexpected or duplicate archive path")
            if not member.isfile() and not member.isdir():
                raise ValueError("Archive links and special files are forbidden")
            seen.add(name)
            total += member.size
            if total > MAX_EXPANDED or len(seen) > 20000:
                raise ValueError("Expanded release exceeds deployment limits")
            target = destination / name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                stream = bundle.extractfile(member)
                if stream is None:
                    raise ValueError("Missing archive file body")
                with target.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    if json.loads((destination / "release.json").read_text()) != {"sha": sha}:
        raise ValueError("Release manifest does not match requested commit")
    for name in (*CODE_DIRS, *CODE_FILES, "web/dist/index.html"):
        if not (destination / name).exists():
            raise ValueError("Incomplete release: " + name)


def run(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, **kwargs)


def check_dependencies(release: Path) -> None:
    # The interpreter and all imported package metadata run as the app user.
    checker = """
import importlib.metadata as m, sys, tomllib
from pathlib import Path
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
projects=[tomllib.loads(Path(path).read_text())['project'] for path in sys.argv[1:]]
requirements=[]
for p in projects:
 if '.'.join(map(str,sys.version_info[:3])) not in SpecifierSet(p.get('requires-python','')):
  raise SystemExit('Runtime Python maintenance required')
 if m.version(p['name']) != p['version']:
  raise SystemExit('Installed package metadata maintenance required: '+p['name'])
 requirements.extend(p.get('dependencies',[]))
 requirements.extend(p.get('optional-dependencies',{}).get('native-research',[]))
missing=[]
for text in requirements:
 r=Requirement(text)
 if r.marker and not r.marker.evaluate(): continue
 try: version=m.version(r.name)
 except m.PackageNotFoundError: missing.append(r.name); continue
 if version not in r.specifier: missing.append(str(r))
if missing: raise SystemExit('Runtime dependency maintenance required: '+', '.join(missing))
print('Runtime dependencies satisfied')
"""
    run(
        "/usr/sbin/runuser",
        "-u",
        "qt",
        "--",
        str(APP / ".venv-native-research/bin/python"),
        "-c",
        checker,
        str(release / "pyproject.toml"),
        str(release / "packages/btc-backtest/pyproject.toml"),
        timeout=60,
    )


def pending_jobs() -> int:
    database = APP / "data/workbench/research.sqlite3"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        return connection.execute(
            "SELECT count(*) FROM research_jobs WHERE status IN ('queued','running','cancelling')"
        ).fetchone()[0]


def wait_for_idle() -> None:
    deadline = time.monotonic() + 300
    while pending_jobs():
        if time.monotonic() >= deadline:
            raise RuntimeError("Research queue did not drain within 5 minutes; release unchanged")
        time.sleep(5)


def request(path: str, authenticated: bool) -> tuple[int, bytes, str]:
    login = json.loads(Path("/root/qt-eatfear-access.json").read_text())
    headers = {}
    if authenticated:
        value = base64.b64encode((login["username"] + ":" + login["password"]).encode()).decode()
        headers["Authorization"] = "Basic " + value
    req = urllib.request.Request("https://qt.eatfear.com" + path, headers=headers)
    try:
        response = urllib.request.build_opener(NoRedirect()).open(req, timeout=15)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, response.read(), response.headers.get("Content-Type", "")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def healthcheck() -> None:
    run("/usr/bin/systemctl", "is-active", "--quiet", *SERVICES)
    for path in ("/", "/api/v3/capabilities"):
        if request(path, False)[0] != 401:
            raise RuntimeError("Anonymous access boundary failed")
    for path in ("/", "/strategies", "/api/v3/capabilities"):
        status, body, content_type = request(path, True)
        if status != 200:
            raise RuntimeError("Authenticated health check failed: " + path)
        if path == "/" and body != (APP / "web/dist/index.html").read_bytes():
            raise RuntimeError("Public frontend does not match installed build")
        if path.startswith("/api/") and "application/json" not in content_type:
            raise RuntimeError("API returned a non-JSON response")
    status, body, _ = request("/api/v3/runtime", True)
    runtime = json.loads(body)
    if status != 200 or runtime.get("live_enabled") or not runtime.get("worker", {}).get("online"):
        raise RuntimeError("Research worker health check failed")
    if request("/api/v3/deployment-missing", True)[0] != 404:
        raise RuntimeError("API miss did not return 404")


def wait_for_health() -> None:
    deadline = time.monotonic() + 90
    while True:
        try:
            healthcheck()
            return
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(3)


def copy_tree(source: Path, target: Path) -> None:
    # Copy from a root-controlled validated tree. Keep the Caddy bind-mount inode.
    run(
        "/usr/bin/rsync",
        "-a",
        "--delete",
        "--chmod=D755,F644",
        str(source) + "/",
        str(target) + "/",
    )


def switch_release(release: Path, backup: Path) -> None:
    for name in ("web", "web/dist"):
        if (APP / name).is_symlink():
            raise ValueError("Refusing symlink deployment target: " + name)
    for name in (*CODE_DIRS, *CODE_FILES):
        target = APP / name
        if target.is_symlink():
            raise ValueError("Refusing symlink deployment target: " + name)
        if target.is_dir():
            shutil.copytree(target, backup / name, symlinks=True)
        else:
            shutil.copy2(target, backup / name)
    shutil.copytree(APP / "web/dist", backup / "web-dist", symlinks=True)
    changed = False
    try:
        changed = True
        for name in CODE_DIRS:
            copy_tree(release / name, APP / name)
        for name in CODE_FILES:
            shutil.copyfile(release / name, APP / name)
        copy_tree(release / "web/dist", APP / "web/dist")
        run("/usr/bin/systemctl", "start", *SERVICES)
        wait_for_health()
    except BaseException:
        if changed:
            print("Deployment failed; restoring previous code and frontend", flush=True)
            run("/usr/bin/systemctl", "stop", *SERVICES)
            for name in CODE_DIRS:
                copy_tree(backup / name, APP / name)
            for name in CODE_FILES:
                shutil.copyfile(backup / name, APP / name)
            copy_tree(backup / "web-dist", APP / "web/dist")
            run("/usr/bin/systemctl", "start", *SERVICES)
            wait_for_health()
            print("Rollback verified; existing databases retained", flush=True)
        raise


def deploy(release: Path, sha: str, run_id: int) -> None:
    marker = STATE / "current.json"
    if marker.exists():
        current = json.loads(marker.read_text())
        if run_id < current["run_id"] or (run_id == current["run_id"] and sha != current["sha"]):
            raise RuntimeError("Refusing an older or mismatched deployment run")
    check_dependencies(release)
    # Stop submissions first, but leave the worker running until the queue drains.
    run("/usr/bin/systemctl", "stop", SERVICES[0])
    try:
        wait_for_idle()
        run("/usr/bin/systemctl", "stop", SERVICES[1])
        backup = STATE / "backups" / (str(run_id) + "-" + str(time.time_ns()))
        backup.mkdir(parents=True, mode=0o700)
        for database in (APP / "data/workbench").glob("*.sqlite3"):
            with (
                sqlite3.connect(database) as source,
                sqlite3.connect(backup / database.name) as dest,
            ):
                source.backup(dest)
        switch_release(release, backup)
        temporary = STATE / "current.tmp"
        temporary.write_text(
            json.dumps({"sha": sha, "run_id": run_id, "deployed_at": time.time()}) + "\n"
        )
        temporary.replace(marker)
        print("DEPLOYED " + sha + " https://qt.eatfear.com", flush=True)
        backups = sorted((STATE / "backups").iterdir(), key=lambda item: item.stat().st_mtime)
        for old in backups[:-5]:
            shutil.rmtree(old)
    finally:
        run("/usr/bin/systemctl", "start", *SERVICES)


def main() -> None:
    os.umask(0o022)
    sha, run_id = command(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    if os.geteuid() != 0:
        raise PermissionError("Receiver must run under its restricted deployment key")
    for event in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(event, interrupted)
    STATE.mkdir(mode=0o755, parents=True, exist_ok=True)
    with (STATE / "deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(prefix="incoming-", dir=STATE) as work:
            directory = Path(work)
            directory.chmod(0o755)
            archive = directory / "release.tar.gz"
            size = 0
            with archive.open("wb") as output:
                while chunk := sys.stdin.buffer.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_ARCHIVE:
                        raise ValueError("Upload exceeds deployment size limit")
                    output.write(chunk)
            release = directory / "release"
            release.mkdir(mode=0o755)
            unpack(archive, release, sha)
            deploy(release, sha, run_id)


def interrupted(signum: int, frame: object) -> None:
    raise InterruptedError("Deployment interrupted; restoring service availability")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("DEPLOYMENT FAILED: " + str(error), file=sys.stderr)
        sys.exit(1)
