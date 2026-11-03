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

## v0.1.1: Docker Hub 403 on tag discovery

`hub_tags()` now uses the public Docker Registry HTTP API V2 instead of the
Docker Hub management API. It obtains a read-only scoped bearer token from
`auth.docker.io`, then fetches `/v2/<namespace>/<repository>/tags/list`
from `registry-1.docker.io`, following `Link: ...; rel="next"` pagination.
It fails closed on incomplete lists, unexpected redirects/pagination hosts,
or token failures. This remains public Docker Hub only; private registries
need separate credential handling.

To update an existing installation:

```sh
sudo cp /opt/svc-update/svc_update.py /opt/svc-update/svc_update.py.bak
sudo install -m 755 svc_update.py /opt/svc-update/svc_update.py
sudo python3 /opt/svc-update/svc_update.py --config /etc/svc-update/config.toml check nginx
```

Before any automatic updates, verify the full upgrade and rollback on an
isolated test stack. Docker Hub API 403 may also be caused by local proxy
or network policies, so a registry token request failure should be diagnosed
with the network team rather than worked around by disabling TLS validation.

## v0.2: bind mount 対応 (2026-11)

**検証状態:** Python 単体テスト通過。実ホストでの Docker 更新・復元・メール通知は未検証。
事前に `sudo systemctl stop svc-update.timer` で自動更新を止め、独立した試験環境で復元試験を実施すること。

- **読み取り専用 bind (`read_only: true`)**: 受け入れる。更新ツールはバックアップ・復元しない（ホスト側の設定ファイルを変更しない）。設定を別途Git等で管理・退避すること。
- **読み書き可能 bind (`read_only` 省略または false)**: 関連スタックのコンテナを停止した後、ホスト上のファイル・ディレクトリを tar に保存。更新失敗時、旧イメージ・named volume と一緒に元の内容に復元する。**復元時にはバックアップ取得後に追加・変更されたデータが消える。**
- **named volume**: 従来通り tar バックアップ・復元する。
- **既存の他サービスの `build:`**: 許容する（更新対象サービスに `build:` がある場合は引き続き拒否）。再起動は `--no-build`。
- **安全上の拒否条件**: RW bind の許可ディレクトリ未設定、未存在のソース、パスの途中にシンボリックリンクがある、データ内にシンボリックリンクや特殊ファイルがある、入れ子のbind、RW bindがマウントポイント、バックアップ先との重複、未対応の外部ボリューム等。
- **重要**: ローカルファイルシステム上のbindだけを想定。外部DB、ネットワーク共有、ホストの独立プロセスがデータを書き込む構成では一貫したバックアップを保証しない。コンテナ以外のライターを止めること。**運用環境での更新成功や無損失復旧は未保証。**

### `config.toml` の変更例

家計簿アプリ (`/opt/docker`) の `app` が `/project/kakeibo-app/data` をRWでbindし、`nginx` が `/mnt/data/nginx/default.conf` をROでbindしている構成の場合：

```toml
[global]
backup_root = "/var/backups/svc-update"
# RW bindソースは必ずこれらの配下（ルートそのものは不可）にあること
allowed_bind_roots = ["/project/kakeibo-app"]

[services.nginx]
enabled = true
auto_update = false
policy = "patch"
image = "nginx"
compose_service = "nginx"  # container_name ではない
version_variable = "NGINX_VERSION"
directory = "/opt/docker"
archive_image = "busybox:1.36.1"
health_url = "http://127.0.0.1:8080/"
health_timeout_seconds = 90
```

※実際の `[email]` セクションと設定値は既存のものを保持すること。`allowed_bind_roots` は `[global]` セクション内へ配置する。`/project/kakeibo-app/data` を直接指定しても許容されない（許可ルート**直下の子孫**のみ許可する設計）。

**注意:** Nginx のパッチ更新であっても、本ツールはComposeプロジェクト全体を停止し、RW bindの家計簿データも取得・復元対象にする。このため家計簿にダウンタイムが生じる。起動前に独立したバックアップを取ること。

### 動作確認

```bash
sudo systemctl stop svc-update.timer
sudo install -m 0755 svc_update.py /opt/svc-update/svc_update.py
sudo python3 -m unittest discover -s tests -v
sudo python3 /opt/svc-update/svc_update.py --config /etc/svc-update/config.toml check nginx
sudo python3 /opt/svc-update/svc_update.py --config /etc/svc-update/config.toml upgrade nginx  # DRY RUN: バックアップ検証は行わない
# 破壊的変更を伴う可能性がある。別環境で復元試験後にのみ実行:
# sudo python3 /opt/svc-update/svc_update.py --config /etc/svc-update/config.toml upgrade nginx --apply
```

### ロールバック時の取り扱い

- 失敗時にはComposeスタック全体を停止してからRW bindを復元する。
- bindバックアップは `backup_root/<service>/<timestamp>/bind-XX.tar`、対応先は `manifest.json` に記録される。
- 復元処理は同一親ディレクトリのステージング先へ展開し、元のパスを退避してから差し替える。差し替えが失敗した場合は元のパスを戻す。ただし複数の復元対象を跨いだ完全な原子的ロールバックではない。
- 自動復元に失敗した場合は `inflight.json` が残り、以後の自動更新を停止する。人手でバックアップと整合性を確認する。
- RO bind は復元しない。外部で設定を変更した場合、旧バージョンで動作しない可能性がある。
- バックアップ先はアプリのデータ領域・Docker volume外の独立ストレージとし、十分な容量とアクセス制限を確保する。
