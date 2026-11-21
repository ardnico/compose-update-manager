# Compose Update Manager — v0.3 preview

A conservative updater for Docker Compose services that use **public Docker Hub** images directly or build their own images from a public Docker Hub base. Requires Python 3.11+, Docker Engine, the Docker Compose plugin and systemd on Linux. Python standard library only.

For installation, configuration, log queries, and recovery procedures in Japanese, see the [operations manual](doc/README.md).

Validate each target's update and recovery on a disposable Compose stack before enabling unattended updates. See [Server validation](#server-validation) for the BusyBox test results.

## New in v0.3

- Recognizes `compose.yaml`, `compose.yml`, `docker-compose.yaml` and `docker-compose.yml`; one file is auto-selected in that priority order. `compose_file` or ordered `compose_files` in the TOML configuration can select custom filenames and additional overlays.
- Only the **target service** is stopped and restarted. It uses `docker compose stop SERVICE` and `docker compose up -d --no-deps --no-build --pull never SERVICE`.
- Policies: `patch`, `minor`, `major`, `any`, `latest`. See below.
- Strict numeric versions with optional exact suffix, e.g. `1.27.0`, `1.27.0-alpine`, `1.27.0-alpine3.20`. Variant suffixes are never silently changed.
- `keep_previous_images` controls how many prior image generations each service retains.
- `local_build` updates the Dockerfile base version and rebuilds the image named in Compose.
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

These policies apply to the upstream tag, including the base tag of a local build. Upstream versions must have the form `X.Y.Z` or `X.Y.Z-suffix`. Suffixes match exactly, so `-alpine` and `-alpine3.20` are separate variants. A suffix such as `-rc` is also treated as a variant.

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
keep_previous_images = 1
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

### Image retention

Set `keep_previous_images` per service. With `1`, after updates from `1.34.0` to `1.34.1` to `1.35.0`, the current `1.35.0` and previous `1.34.1` remain and the updater removes the old `1.34.0` tag. With `0`, all previous generations become eligible for cleanup after the new container passes its health check. Omitting the setting keeps all generations.

Cleanup uses the service's update history under `backup_root`. A successful update records the image it replaced. Pre-existing tags outside that history stay on the host. The updater checks the recorded image ID and preserves tags used by containers or retained by another configured service. Tags still in use are checked again after a later successful update. Docker can keep shared layers, and BuildKit cache has its own lifecycle. [Docker image removal](https://docs.docker.com/reference/cli/docker/image/rm/)

Data backups remain on disk. A restore requires the recorded image and verifies its identity before stopping the target or writing data. Keep enough generations for the rollback window you need.

### Local builds

With `local_build`, `image` in TOML is the Docker Hub **base repository** and `version_variable` contains its tag. The Compose service supplies the **local output image**. For example, use these settings with the usual `[global]` section and an existing Compose project.

```toml
[services.app]
enabled = true
auto_update = false
policy = "patch"
image = "busybox"
compose_service = "app"
directory = "/srv/containers/app"
compose_file = "docker-compose.yml"
version_variable = "APP_BASE_VERSION"
keep_previous_images = 2
health_url = "http://127.0.0.1:18081/"

[services.app.local_build]
base_argument = "BASE_IMAGE"
```

Set `APP_BASE_VERSION=1.34.0` in `versions.env`. Pass the full base reference through `build.args` and use that argument in the Dockerfile's `FROM`. [Dockerfile ARG and FROM](https://docs.docker.com/reference/dockerfile/#understand-how-arg-and-from-interact)

```yaml
services:
  app:
    image: my-app:latest
    build:
      context: .
      args:
        BASE_IMAGE: busybox:${APP_BASE_VERSION:?Set in versions.env}
```

```dockerfile
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
```

Run `svc-update check app` to see the next eligible base tag and `svc-update upgrade app --apply` to rebuild and deploy. The updater builds only the configured service with `docker compose build --pull` under a temporary tag before stopping the current container. Fixed local tags such as `my-app:latest` stay the same in Compose. The updater saves their previous images under `svc-update-history:` tags and can put the saved image back under the Compose tag during a restore. You can also use a versioned output such as `my-app:release-${APP_BASE_VERSION}`, in which case the previous version keeps its own tag. [Docker Compose build](https://docs.docker.com/reference/cli/docker/compose/build/)

The output repository must differ from the base repository. The target may update its output tag and the configured base argument. Other Compose settings stay independent of the tracked version. A managed build must use a single Compose service output.

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

The unit suite uses Docker mocks. The live tests below ran separately on disposable Compose projects.

## Server validation

At the start of validation on 2026-11-21 JST, `journalctl -u svc-update.service` showed 14 nightly runs from November 8 through November 21. Each logged `nginx: 1.27.5: no eligible major tag` and systemd recorded a successful exit. Those runs exercised candidate discovery and the no-update path.

On the same server, manual tests used Python 3.11.18, Docker Engine 29.9.2 and Docker Compose 5.7.0 on Linux amd64. Three disposable Compose projects ran BusyBox HTTP services on loopback ports. Each test used `keep_previous_images = 1` and updated from `1.34.0` to `1.34.1` to `1.35.0`.

| Scenario | Observed result |
|--|--|
| Docker Hub image | Pulled both updates, kept `busybox:1.34.1` and removed `busybox:1.34.0`. |
| Local build with a fixed `:latest` tag | Rebuilt from both newer BusyBox bases, preserved the Compose tag, kept the `1.34.1` build under its history tag and removed the `1.34.0` build tag. |
| Local build with a versioned `:release-${APP_BASE_VERSION}` tag | Rebuilt and deployed both rendered tags, kept `:release-1.34.1` and removed `:release-1.34.0`. |
| Manual restore | All three projects recovered the retained version and its named-volume data. Restoring a pruned version was refused before the running container was stopped. |
| Failed deployment | The fixed-tag `1.36.0` build returned HTTP 404 and wrote to the test volume. Automatic rollback restored the `1.35.0` image, `versions.env` and the pre-update volume contents. |
| Failed build | A Dockerfile step exited with status 1. The original container kept running with the same ID, image and volume data. |

The sibling container IDs stayed the same throughout the tests. `check` and the `run` dry run reported the pending update, and the dry run kept the current container image. The tests invoked the repository CLI against temporary projects. The server's installed service continues to run the existing program under `/opt/svc-update`.

## Security

Docker access is root-equivalent. Keep all scripts, Compose definitions, versions files and updater configuration write-protected against untrusted users. Backups may contain confidential data. Maintain independent off-host backups. Registry tags can be mutable; production deployments should additionally pin or validate image digests.

See [LICENSE](LICENSE).
