# Automatic deployment to TC

Pushing to `main` runs `.github/workflows/deploy-tc.yml` (GitHub Actions →
**Deploy TC**). A successful run updates https://qt.eatfear.com. The same
workflow can be rerun with **Run workflow**, selecting `main`.

## Release gate and deployment

1. Install locked frontend dependencies, run all frontend tests, and build the SPA.
2. Install Python 3.12 native research dependencies; run receiver lint/tests,
   Nautilus, workbench, provenance, cycle, and lab service tests.
3. Package the tested commit plus built frontend. Skip a superseded commit if
   `main` has advanced before upload; serialize production runs.
4. Stream the package over SSH with pinned server identity. The root-installed
   receiver accepts only `deploy <40-character SHA> <GitHub run ID>`, rejects
   links, traversal, unexpected paths, oversized archives, and stale runs.
5. Check the installed Python/package requirements, stop API submissions, and
   allow the worker up to five minutes to drain queued/running research jobs.
   A timeout restarts the API and leaves code unchanged.
6. Back up code, frontend, and consistent SQLite snapshots; update only `src`,
   `scripts`, `config`, `packages`, `pyproject.toml`, `README.md`, and `web/dist`.
   Restart API/worker and check the real HTTPS hostname, anonymous 401,
   authenticated pages/API, exact frontend HTML, worker availability, research
   mode, and API 404 behavior. On failure, restore code/frontend and recheck.

Normal deployment causes a brief API interruption; a busy queue can extend it
up to five minutes. Databases are never automatically rolled back. The latest
five code/database backup sets remain under `/var/lib/qt-deploy/backups`.
Successful revision/run metadata is stored in `/var/lib/qt-deploy/current.json`.

This workflow has its own TC research checks. The existing `CI` workflow remains
independent: its platform image and general lint had existing failures when
this pipeline was introduced. A green **Deploy TC** does not assert that those
unrelated platform checks pass.

## Credentials and server installation

GitHub → Settings → Environments → `tc-production` permits only branch `main`:

- Secret `TC_SSH_PRIVATE_KEY`: dedicated deployment key, not the operator key.
- Secret `TC_SSH_KNOWN_HOSTS`: server Ed25519 identity obtained over existing,
  authenticated SSH; deployment uses `StrictHostKeyChecking=yes`.
- Variable `TC_HOST`: `101.32.243.66`.

The matching server public key is restricted with `restrict` and the fixed
command `/usr/bin/timeout 1200 /usr/bin/python3 -I /usr/local/sbin/qt-deploy-receive.py`.
It cannot open a shell, forward ports, or replace the root-owned receiver.
Only a separately authenticated administrator installs receiver changes from
`deploy/tc_receive.py`. Main-branch authors can deploy application code, which
runs as the existing `qt` service user.

`/opt/qt` and deployed code are root-owned; the service's data directories remain
writable by `qt`. Login credentials, `.env.workbench`, runtime environments,
Caddy configuration, DNS, service units, and persistent data are outside the
upload allowlist. Infrastructure changes require a separate reviewed deployment.

Dependencies are validated against the existing native runtime, not upgraded
in place. A new Python requirement, package version, or unsatisfied dependency
fails before stopping services; maintain the server runtime and then rerun the
workflow. This avoids an unrollbackable partial native-library upgrade.

## Verify and recover

```bash
ssh tc 'cat /var/lib/qt-deploy/current.json'
ssh tc 'systemctl status qt-workbench-api qt-workbench-worker --no-pager'
```

To roll back an application change, revert its commit on `main` and push. This
creates a new checked deployment; old workflow runs cannot overwrite a newer
successful run. Failed runs report the failing check and rollback status in
GitHub Actions. If rollback itself fails, use the existing operator SSH access
and inspect the retained backup; never replace live SQLite state automatically.
