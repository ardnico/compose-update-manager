# Compose Update Manager — v0.3 preview

A conservative updater for **public Docker Hub** images managed with Docker Compose. Requires Python 3.11+, Docker Engine, Docker Compose v2 and systemd on Linux. Python standard library only.

**Important:** The automated Docker upgrade and rollback paths have not been tested against a live daemon. Do not enable unattended updates until deliberate failure and recovery tests pass on a disposable Compose stack.

## New in v0.3

- Recognizes `compose.yaml`, `compose.yml`, `docker-compose.yaml` and `docker-compose.yml`; one file is auto-selected in that priority order. `compose_file` or ordered `compose_files` in the TOML configuration can select custom filenames and additional overlays.
- Only the **target service** is stopped and restarted. It uses `docker compose stop SERVICE` and `docker compose up -d --no-deps --no-build --pull never SERVICE`.
- Policies: `patch`, `minor`, `major`, `any`, `latest`. See below.
- Strict numeric versions with optional exact suffix, e.g. `1.27.0`, `1.27.0-alpine`, `1.27.0-alpine3.20`. Variant suffixes are never silently changed.
- `install.sh` installs CLI, program and systemd units while preserving `/etc/svc-update/config.toml`; timer is disabled unless explicitly requested.
- The backup format is now **3**. Only target-service persisted mounts are saved and restored. Shared target data that another service mounts is rejected.

## Policy semantics

For current `1.27.0-alpine`:

| Policy | Selection |
|--|--|
| `patch` | Numerically smallest higher patch in same major/minor; e.g. `1.27.1-alpine` |
| `minor` | Lowest patch in the next available higher minor in the same major, e.g. `1.28.0-alpine` |
| `major` | Lowest version in the next available major, e.g. `2.0.0-alpine` |
| `any` | Numerically smallest version higher than current |
| `latest` | Highest numerically sorted available version |

The suffix must be **identical**. `-alpine` and `-alpine3.20` are different variants. This tool does not implement SemVer pre-release ordering; `-rc` is treated as an exact variant, not a pre-release rank. No support for `v1.2.3`, `latest` tag aliases or build metadata.

For `auto_update = true`, `major` and `latest` policies require `allow_breaking_auto_update = true`. Such updates can be incompatible. Prefer patch for unattended services.

## Installer

Run from the cloned repository:

```bash
bash install.sh --dry-run
sudo bash install.sh
sudoedit /etc/svc-update/config.toml
sudo svc-update status
sudo svc-update check nginx
```

The installer preserves existing `config.toml`, creates a timestamped backup of any previous `svc_update.py`, installs `/usr/local/bin/svc-update` and the systemd units, and **stops/disables the existing timer by default**. To enable only **after** validating manual update and rollback:

```bash
sudo systemctl enable --now svc-update.timer
```

`sudo bash install.sh --enable-timer` is supported but should not be used before validating recovery. The service is a one-shot static service and does **not** need `systemctl enable svc-update.service`.

## Configure a target

Example `/etc/svc-update/config.toml` for a Compose project in `/opt/docker`:

```toml
[global]
backup_root = "/var/backups/svc-update"
# Required only for target-service RW bind mounts:
# allowed_bind_roots = ["/project/myapp"]

[email]
enabled = false

[services.nginx]
enabled = true
auto_update = false
policy = "patch"
image = "nginx"
compose_service = "nginx"  # Compose services: key, NOT container_name
version_variable = "NGINX_VERSION"
directory = "/opt/docker"
# compose_file = "docker-compose.yml" # optional; autodetects standard names
# compose_files = ["compose.yaml", "compose.override.yaml"]
archive_image = "busybox:1.36.1"
health_url = "http://127.0.0.1:8080/"
health_timeout_seconds = 90
```

In the target's Compose file:

```yaml
services:
  nginx:
    image: nginx:${NGINX_VERSION:?Set in versions.env}
```

Keep `/opt/docker/.env` (can be empty), and put `NGINX_VERSION=1.27.0-alpine` in `/opt/docker/versions.env`. Both environment files are passed explicitly to Docker Compose. Inherited variables with the same names are removed to avoid unwanted overrides.

```bash
sudo svc-update status
sudo svc-update check nginx
sudo svc-update upgrade nginx          # version-selection dry run, not a full preflight
sudo svc-update upgrade nginx --apply  # real update; rollback may overwrite target data
sudo svc-update run                    # auto_update=true services, dry run
sudo svc-update run --apply            # real nightly command
```

## Backups, no-deps and compatibility

The updater stops **only** `compose_service`, backs up **only the target's** named local volumes and writable bind mounts, and restarts it with `--no-deps`. RO binds are permitted and left untouched. RW bind mounts must be inside configured `allowed_bind_roots`; symlinks, special files, remote/external volumes and shared writeable storage are not supported. If another Compose service mounts a target volume or overlaps the target's RW bind, the updater rejects it. It cannot detect independent host processes writing to data, so those must be stopped separately. A restored backup will lose all writes made after the backup snapshot.

**v0.2 backups are not restorable through v0.3** because v0.2 backed up the whole Compose stack and v0.3 only backs up one service. v0.3 uses a `format: 3, scope: target` manifest and deliberately refuses format-2 archives. Retain the old binary and old archives for recovery in an isolated environment. Confirm an end-to-end format-3 recovery before allowing automatic execution.

On interrupted transactions, `inflight.json` remains and subsequent updates stop for manual inspection. Restoration checks backup hashes and prior Docker image identity, but multi-mount restore is **not atomic**. This tool does not perform DB migration compatibility checks and is not suitable for all production databases. Health probes check the target service and a configured localhost HTTP endpoint. Email notifications are optional.

## Tests

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh bin/svc-update
bash install.sh --dry-run
```

The test suite uses Docker mocks. No Docker daemon-backed upgrade, restore, SMTP delivery, or unattended timer has been exercised by the implementer. Test those on a disposable VM.

## Security

Docker access is root-equivalent. Keep all scripts, Compose definitions, versions files and updater configuration write-protected against untrusted users. Backups may contain confidential data. Maintain independent off-host backups. Registry tags can be mutable; production deployments should additionally pin or validate image digests.

See [LICENSE](LICENSE).
