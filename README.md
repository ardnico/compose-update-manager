# Compose Update Manager — conservative MVP

Python 3.11+ / Docker Engine / Docker Compose v2 / Ubuntu Server. Standard library only.

## Supported scope

* Docker Hub public repositories with **exact `X.Y.Z` tags** (e.g. `nginx:1.28.0`).
* Next available stable **patch** version only, within the same major and minor.
* One target Compose service per stack. Other service versions remain pinned.
* Named, local Docker volumes only; stopped-stack archive backup and restore.
* All Compose services must already be running. All get restarted during upgrade.
* Container state checks and `http://127.0.0.1:<port>/` returning HTTP 200.
* Explicit apply, config-driven nightly targets, lock, transaction journal, notification.

**Not supported:** bind mounts (including LocalSettings.php), anonymous/external volumes, external databases, remote volumes, multiple image tags changed at once, images with distro suffixes, private registries, mutable release tags, application-specific migration hooks, HA deployments or guaranteed zero data loss. The program refuses unsupported mounts. **Do not enable MediaWiki production updates until the application-specific DB migration, extensions compatibility and backup/restoration paths have been validated.** Only a lab deployment is suitable at present.

Rollback restores a *pre-update* state and loses writes made after backup. Stop client writes for the full operation (maintenance window/firewall/application read-only). Old image must remain locally available and identical; no image pruning during upgrade. A tag is not an immutable identity: record/verify image IDs as this tool does, and adopt digest pinning for a production version.

## Install (example)

```bash
sudo install -d -m 0700 /etc/svc-update /var/backups/svc-update
sudo install -d -m 0755 /opt/svc-update /srv/containers/nginx
sudo install -m 0755 svc_update.py /opt/svc-update/svc_update.py
sudo install -m 0600 config.example.toml /etc/svc-update/config.toml
sudo cp examples/nginx/compose.yaml /srv/containers/nginx/
sudo cp examples/nginx/.env examples/nginx/versions.env /srv/containers/nginx/
sudo chmod 0600 /srv/containers/nginx/.env /srv/containers/nginx/versions.env
# Disable/remove the mediawiki entry in config.toml until its directory exists.
cd /srv/containers/nginx
sudo docker compose --env-file .env --env-file versions.env config
sudo docker compose --env-file .env --env-file versions.env up -d
```

> `config.example.toml` includes a disabled MediaWiki example with a non-existent directory. The parser ignores missing files for disabled services, so the example can remain in place.

## Commands

```bash
sudo python3 /opt/svc-update/svc_update.py --config /etc/svc-update/config.toml status
sudo python3 /opt/svc-update/svc_update.py --config /etc/svc-update/config.toml check nginx
sudo python3 /opt/svc-update/svc_update.py --config /etc/svc-update/config.toml upgrade nginx         # no changes
sudo python3 /opt/svc-update/svc_update.py --config /etc/svc-update/config.toml upgrade nginx --apply # DOWNTIME and destructive restore possible
sudo python3 /opt/svc-update/svc_update.py --config /etc/svc-update/config.toml run                   # enabled, auto_update services only; no changes
sudo python3 /opt/svc-update/svc_update.py --config /etc/svc-update/config.toml run --apply           # night job
# Manual restore after investigating an interrupted transaction:
sudo python3 /opt/svc-update/svc_update.py --config /etc/svc-update/config.toml restore nginx --backup-id 20261103T150000Z --apply
```

`check` accesses Docker Hub. Automatic upgrade is *disabled* by default (`auto_update = false`). To allow nightly updates set both `enabled = true` and `auto_update = true` after running full restoration drills. Version values are in `versions.env`; Compose references `${NGINX_VERSION}`. `.env` is kept for non-version configuration. The CLI always supplies both files explicitly.

## systemd timer

```bash
sudo install -m 0644 systemd/svc-update.service /etc/systemd/system/
sudo install -m 0644 systemd/svc-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now svc-update.timer
systemctl list-timers svc-update.timer
journalctl -u svc-update.service --no-pager -n 100
```

Email is off by default. Set `[email] enabled=true`, use your internal SMTP relay; credentials are read from `SVC_UPDATE_SMTP_USER` and `SVC_UPDATE_SMTP_PASSWORD` if provided via a root-only systemd `EnvironmentFile` (do not check secrets into Git). Mail transport failure is logged, without retroactively rolling back a healthy update.

## Recovery and safety

* On crash or forced termination, `inflight.json` is **not cleared**; future updates fail closed. Inspect its stage and the current containers and data before doing anything. Manual recovery may be required.
* When app deployment fails after an intact backup, manager recreates old containers, restores **all named local volumes**, and tests the service endpoint. Failure to recover leaves the journal for manual investigation.
* Any bind-mounted configuration, missing health endpoint, non-running service, or unsupported Docker volume causes an abort.
* `docker compose down` removes containers/network (NOT volumes); any app state stored in the writable container layer will be lost. Persist **all application data** in named volumes.
* Use an externally stored, periodically verified off-host backup as a second line of defense. This tool backs up to one local directory and does not implement off-host copies, backup retention, backup encryption, or alert escalation.
* Docker access grants root-equivalent privileges; keep config and binary root-owned, limit edit permissions, restrict the network path to localhost.
* HTTPS proxies, trusted local service checks, maintenance mode, extension compatibility, schema migration hooks, and immutable digest pinning are required for serious production services but outside the MVP.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

These are **unit tests only**. No Docker daemon is available in the development environment; `upgrade --apply`, downtime behavior, restore on failure and timer behavior must be integration tested on a disposable VM before real use.

## License

Internal example / adapt to local operational policy.
