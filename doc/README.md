# Compose Update Manager 運用マニュアル

Docker Composeで稼働中の1サービスを対象に、公開Docker Hubイメージの更新、またはベースイメージを更新したローカルビルドを行います。更新時に対象サービスのデータをバックアップし、新しいコンテナがヘルスチェックを通過できなければ自動で復元します。

- [導入と初期設定](#導入と初期設定)
- [Docker Hubイメージの設定例](#docker-hubイメージの設定例)
- [ローカルビルドの設定例](#ローカルビルドの設定例)
- [設定項目](#設定項目)
- [更新方法](#更新方法)
- [ログの確認](#ログの確認)
- [イメージの世代管理](#イメージの世代管理)
- [バックアップと復元](#バックアップと復元)
- [トラブルシューティング](#トラブルシューティング)

## 導入と初期設定

Linux、Python 3.11以上、Docker Engine、Docker Composeプラグインが必要です。標準インストーラーと定期実行にはsystemdを使います。以下はリポジトリのルートから実行します。

```bash
python3 --version
docker version
docker compose version
bash install.sh --dry-run
sudo bash install.sh
sudoedit /etc/svc-update/config.toml
sudo svc-update status
```

インストーラーはプログラムを `/opt/svc-update/svc_update.py` に、CLIを `/usr/local/bin/svc-update` に配置します。既存の設定ファイルを引き継ぎ、既存のプログラムは日時付きでバックアップします。通常のインストールでは既存のタイマーも停止・無効化されます。

以下の設定例では、既存プロジェクトと区別できるディレクトリ名、Composeプロジェクト名、ポートを使います。既存のサービスを登録するときは実際の値に読み替えてください。systemdから実行する場合、サービスユニットの `ProtectHome=true` により、Composeファイルやビルドコンテキストは `/srv` や `/opt` などホームディレクトリの外に置きます。Dockerを操作する権限はroot相当なので、設定やビルドファイルの書き込み権限を管理者と信頼できる運用者に限定します。

### Composeプロジェクトの環境ファイル

対象のディレクトリに `.env` と `versions.env` を用意します。`.env` は空でも構いません。`versions.env` には追跡するバージョンを `KEY=値` の形式で書きます。変数名は大文字の英字で始め、残りは大文字の英数字とアンダースコアを使います。値は引用符で囲まず、同じファイル内でキーを重複させないでください。

アップデーターはComposeに `.env`、`versions.env` の順に渡し、両ファイルで指定した変数名を親プロセスの環境から除外します。更新時に書き換えるのは `versions.env` の指定した変数です。

## Docker Hubイメージの設定例

`/srv/containers/update-demo-nginx` に空の `.env`、次の2ファイルを用意します。

`versions.env`

```dotenv
NGINX_VERSION=1.27.4
```

`compose.yaml`

```yaml
name: update-demo-nginx
services:
  web:
    image: nginx:${NGINX_VERSION:?Set in versions.env}
    ports:
      - "127.0.0.1:18080:80"
```

`/etc/svc-update/config.toml` に登録します。既存の `[global]` がある場合はそのセクションを使い、サービスのセクションを追記します。

```toml
[global]
backup_root = "/var/backups/svc-update"

[services.registry-demo]
enabled = true
auto_update = false
directory = "/srv/containers/update-demo-nginx"
compose_service = "web"
version_variable = "NGINX_VERSION"
image = "nginx"
policy = "patch"
keep_previous_images = 1
health_url = "http://127.0.0.1:18080/"
health_timeout_seconds = 90
```

初回はComposeでサービスを起動して、HTTP応答と更新候補を確認します。ここでの `registry-demo` はTOMLの登録名で、`web` はComposeのサービス名です。

```bash
cd /srv/containers/update-demo-nginx
sudo docker compose --env-file .env --env-file versions.env -f compose.yaml config --images
sudo docker compose --env-file .env --env-file versions.env -f compose.yaml up -d web
curl -fsS http://127.0.0.1:18080/
sudo svc-update check registry-demo
```

## ローカルビルドの設定例

`/srv/containers/update-demo-app` に空の `.env`、次の3ファイルを用意します。TOMLの `image` は更新を追跡するベースのリポジトリを指します。Composeの `image` が、ビルド結果を起動するための名前とタグです。

`versions.env`

```dotenv
APP_BASE_VERSION=1.34.0
```

`docker-compose.yml`

```yaml
name: update-demo-app
services:
  app:
    image: update-demo-app:latest
    build:
      context: .
      args:
        BASE_IMAGE: busybox:${APP_BASE_VERSION:?Set in versions.env}
    ports:
      - "127.0.0.1:18081:8080"
```

`Dockerfile`

```dockerfile
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG BASE_IMAGE
RUN mkdir -p /www && echo "$BASE_IMAGE" > /www/index.html
CMD ["httpd", "-f", "-p", "8080", "-h", "/www"]
```

`/etc/svc-update/config.toml` の `[global]` に加えて、以下のサービスを登録します。

```toml
[services.local-demo]
enabled = true
auto_update = false
directory = "/srv/containers/update-demo-app"
compose_file = "docker-compose.yml"
compose_service = "app"
version_variable = "APP_BASE_VERSION"
image = "busybox"
policy = "patch"
keep_previous_images = 2
health_url = "http://127.0.0.1:18081/"

[services.local-demo.local_build]
base_argument = "BASE_IMAGE"
```

```bash
cd /srv/containers/update-demo-app
sudo docker compose --env-file .env --env-file versions.env -f docker-compose.yml config
sudo docker compose --env-file .env --env-file versions.env -f docker-compose.yml build --pull app
sudo docker compose --env-file .env --env-file versions.env -f docker-compose.yml up -d --no-build --pull never app
curl -fsS http://127.0.0.1:18081/
sudo svc-update check local-demo
```

Dockerfileの `FROM` で `BASE_IMAGE` を使うため、更新後もHTTPの応答本文でビルドに使ったベースを確認できます。Composeの固定タグ `update-demo-app:latest` は更新後も同じです。ビルド結果をバージョン付きで並べたい場合は、初回起動前にComposeの `image` を `update-demo-app:release-${APP_BASE_VERSION}` に変更します。

適用時は、現在のコンテナを稼働させたまま `docker compose build --pull` で対象だけを一時タグにビルドします。ビルドとバックアップが済んだら、Composeが指定するタグで新しいコンテナを起動します。固定タグは対象専用にし、`build.tags` による追加タグや、`additional_contexts` で別のComposeサービスを参照する構成は避けます。

## 設定項目

設定ファイルの既定パスは `/etc/svc-update/config.toml` です。別ファイルを使うときは、サブコマンドの前に `--config` を指定します。[設定ファイルのひな形](../config.example.toml)

```bash
sudo svc-update --config /path/to/config.toml status
```

### 共通設定

| 項目 | 内容 |
| --- | --- |
| `global.backup_root` | 必須。バックアップ、イメージ履歴、更新状態を置くディレクトリ。標準構成では `/var/backups/svc-update`。 |
| `global.allowed_bind_roots` | 書き込み可能なバインドマウントを許可する親ディレクトリの配列。既存の絶対パスを指定します。 |
| `email.enabled` | メール通知の有効化。既定は `false`。有効にする場合は `host`、`from`、`to` を指定します。 |
| `email.port` / `email.starttls` | SMTPポートは既定で `25`、STARTTLSは既定で `false`。SMTP認証を使う場合は実行環境に `SVC_UPDATE_SMTP_USER` と `SVC_UPDATE_SMTP_PASSWORD` を渡します。 |

### サービスごとの設定

| 項目 | 内容 |
| --- | --- |
| `enabled` | 対象サービスを有効化。既定は `false`。 |
| `auto_update` | `run` と定期実行の対象にする。既定は `false`。 |
| `directory` | Composeプロジェクトのディレクトリ。設定例では絶対パスを使用。 |
| `compose_service` | Composeファイルの `services` 直下のキー。 |
| `compose_file` / `compose_files` | 1ファイルまたは順序付きの複数ファイル。プロジェクト内の相対パスを指定します。 |
| `image` | 公開Docker Hubのリポジトリ名。ローカルビルドではベースのリポジトリ名。 |
| `version_variable` | `versions.env` 内で追跡する変数名。 |
| `policy` | 更新候補の選択方法。既定は `patch`。 |
| `allow_breaking_auto_update` | `major` または `latest` と `auto_update = true` を併用する場合に `true` が必要。 |
| `keep_previous_images` | 保持する旧世代の数。省略すると全世代を保持。 |
| `archive_image` | 名前付きボリュームのアーカイブ・復元に使うイメージ。既定は `busybox:1.36.1`。 |
| `health_url` | 実更新・復元の確認に使うHTTP URL。ホスト側から `127.0.0.1` または `localhost` でアクセスでき、HTTP 200を返すURLを指定します。Docker側にヘルスチェックがある場合は、その結果も `healthy` になる必要があります。 |
| `health_timeout_seconds` | ヘルスチェックの待機時間。既定は `150` 秒。 |
| `local_build.base_argument` | Dockerfileへベースイメージの参照を渡すComposeのビルド引数名。この設定を追加するとローカルビルドモードで動作します。 |

Composeファイルを明示しない場合は、`compose.yaml`、`compose.yml`、`docker-compose.yaml`、`docker-compose.yml` の順で最初に見つかった1つを選びます。オーバーライドファイルも使う運用なら `compose_files = ["compose.yaml", "compose.override.yaml"]` のように順番を指定します。

### 更新ポリシー

追跡するタグの書式は `X.Y.Z` または `X.Y.Z-suffix` です。接尾辞は完全一致するものを選びます。`1.27.4-alpine` からは、`1.27.5-alpine` が候補になり、`1.27.5` や `1.27.5-alpine3.20` は対象外です。ローカル出力の固定タグには `:latest` を使えます。追跡するベースやレジストリ側のタグは数値形式にしてください。

| ポリシー | 現在より新しい候補の選び方 |
| --- | --- |
| `patch` | 同じメジャー・マイナーの中で最も近いパッチ。 |
| `minor` | 同じメジャーで、次に近いマイナーの最小パッチ。 |
| `major` | 次に近いメジャーの最小バージョン。 |
| `any` | メジャーやマイナーを問わず、現在より上で最小のバージョン。 |
| `latest` | 現在より上で最大のバージョン。 |

## 更新方法

### 手動更新

```bash
sudo svc-update status registry-demo
sudo svc-update check registry-demo
sudo svc-update upgrade registry-demo
sudo svc-update upgrade registry-demo --apply
```

`status` で表示する `configured` は設定されたバージョンです。実際に起動しているイメージはComposeの `ps` や `docker inspect` で確認します。`check` と `upgrade` のドライランでは更新候補を確認できます。コンテナ起動状況、マウント、ビルド結果まで含む検証は `--apply` の処理で行います。

実更新では、新しいイメージを取得またはビルドし、対象サービスを停止してバックアップを作成します。その後 `versions.env` を更新し、`--no-deps --no-build --pull never` で対象だけを再作成・起動します。ヘルスチェックが成功すると、その更新が確定します。

### systemdによる定期実行

手動更新と復元の動作を確認してから、定期更新するサービスに `auto_update = true` を設定します。`enabled = true` と `auto_update = true` の両方を設定したサービスが `run` の対象です。

```bash
sudo svc-update run
sudo systemctl enable --now svc-update.timer
systemctl list-timers --all svc-update.timer
```

`run` は候補確認のドライランです。systemdサービスは `run --apply` で起動します。タイマーの既定値は毎日02:00に最大10分のランダム遅延を加えた時刻です。サーバ停止中に逃した回は、起動時にさかのぼって実行しません。

定期実行を止めるには `sudo systemctl disable --now svc-update.timer` を使います。タイマーを停止しても、すでに実行中の更新処理は続きます。

## ログの確認

```bash
journalctl -u svc-update.service -n 50 --no-pager
journalctl -u svc-update.service --grep='^SUCCESS ' --no-pager
journalctl -u svc-update.service -f
```

更新前後のバージョンは `SUCCESS` 行に並びます。以下はローカルビルドの出力例です。

```text
SUCCESS local-demo 1.34.0 -> 1.34.1
Backup: /var/backups/svc-update/local-demo/20261121T030833Z
Local image: update-demo-app:latest
Base image: busybox:1.34.1
```

ローカルビルドでは矢印の両側がベースイメージのタグです。`Local image` が実際に起動したローカルイメージ名です。

| ログ | 意味 |
| --- | --- |
| `no eligible ... tag` | 現在のポリシーと接尾辞に合う新しいタグがありません。 |
| `DRY RUN` | 更新候補を表示した段階です。 |
| `SUCCESS` | 新イメージで起動し、ヘルスチェックが成功しました。 |
| `ROLLED BACK` | 更新は失敗し、旧イメージとデータの復元を確認しました。コマンドは失敗の終了コードを返します。 |
| `CRITICAL RESTORE FAILED` / `CRITICAL EARLY FAILURE` | 自動復元または旧コンテナの再起動で失敗しました。中断ファイルと実稼働状態を確認します。 |
| `MANUAL RESTORE VERIFIED` | 手動復元後の起動とヘルスチェックが成功しました。 |

手動更新は端末に出力します。手動更新の結果もjournaldへ送る場合は、対象と候補を確認し、実更新を行うタイミングで `systemd-cat` 経由で実行します。

```bash
sudo systemd-cat -t svc-update-manual /usr/local/bin/svc-update upgrade registry-demo --apply
journalctl -t svc-update-manual --no-pager
```

## イメージの世代管理

`keep_previous_images` は稼働中のイメージを除いた世代数です。レジストリイメージとローカルビルドのどちらにも、登録したサービスごとに設定できます。

| 設定 | 動作 |
| --- | --- |
| 未指定 | 過去のイメージをすべて保持します。 |
| `0` | 更新確定後、追跡済みの旧世代がすべて掃除対象になります。 |
| `1` | 更新直前のイメージを保持します。 |
| `2` 以上 | 更新の新しい順に指定数を保持します。 |

例えば `1.34.0 → 1.34.1 → 1.35.0` と更新して `keep_previous_images = 1` を指定した場合、`1.35.0` と `1.34.1` が残り、`1.34.0` が掃除対象になります。固定タグのローカルビルドでは、置き換えるイメージを `svc-update-history:<ID>` として残します。バージョン付きのローカルタグとDocker Hubイメージでは、旧バージョンのタグをそのまま使います。

対象になるのは成功した更新で記録されたイメージです。コンテナが参照中のタグや、別の設定済みサービスが保持するタグは保護されます。使用中で削除が保留になったタグは、次回以降の成功した更新で再確認します。バックアップのデータファイルとBuildKitキャッシュは別管理です。Dockerイメージの共有レイヤーは、タグ削除後も参照があれば残ります。

## バックアップと復元

### 保存内容

```text
/var/backups/svc-update/
└── registry-demo/
    ├── 20261121T030833Z/
    │   ├── manifest.json
    │   ├── versions.env
    │   ├── volume-00.tar   # 対象に名前付きボリュームがある場合
    │   └── bind-00.tar     # 対象に保存対象のバインドマウントがある場合
    ├── image-history.json
    └── inflight.json       # 更新中または中断時
```

バックアップIDはUTCで `YYYYMMDDThhmmssZ` 形式です。マニフェストには旧バージョン、イメージID、対象のマウントとチェックサムが記録されます。復元にはこのバックアップに加えて、記録と一致する旧イメージが必要です。

保存するのは対象サービス専用の、シンプルなローカル名前付きボリュームと書き込み可能なバインドマウントです。外部ボリューム、ドライバーオプション付きボリューム、匿名ボリューム、およびほかのComposeサービスが共有する対象データは検査で拒否されます。読み取り専用のバインドマウントは復元対象外です。

書き込み可能なバインドマウントを使う場合は、例えば `allowed_bind_roots = ["/srv/containers/myapp"]` を `[global]` に追加し、マウント元をその配下の `/srv/containers/myapp/data` にします。シンボリックリンク、特殊ファイル、バックアップ先との重複、マウントポイント自体を指す書き込み可能なバインドは使えません。別のホストプロセスもデータに書き込む構成では、整合性を保つためにバックアップ・復元中の書き込みを調整します。

### 手動復元

復元すると対象を停止し、バックアップ取得後のデータの変更を上書きします。復元したい時点と、必要に応じて退避する現在のデータを先に確認してください。

```bash
sudo ls -1 /var/backups/svc-update/registry-demo/
backup_id=20261121T030833Z
sudo python3 -m json.tool "/var/backups/svc-update/registry-demo/${backup_id}/manifest.json"
sudo svc-update restore registry-demo --backup-id "$backup_id"
sudo svc-update restore registry-demo --backup-id "$backup_id" --apply
```

`backup_id` は実際の一覧から選びます。ドライランでは選択したパスを確認できます。実復元ではチェックサムとイメージIDを検証し、データと `versions.env` を復元したあと、対象を起動してヘルスチェックを行います。固定タグのローカルビルドでは、必要に応じて保持イメージをComposeの固定タグに戻します。

旧タグが削除されている場合、元のイメージIDと一致するイメージを確保してから復元します。複数のマウントを復元する場合は順次適用されます。データのバックアップ形式は `format: 3`、`scope: target` です。旧 `format: 2` の復元には、対応する旧版の手順を使います。

## トラブルシューティング

| 状況 | 確認すること |
| --- | --- |
| 更新候補が出ない | `versions.env` の値、`policy`、タグの接尾辞を確認します。`major` で該当する上位メジャーがない場合は正常終了です。 |
| `expected one running container` | 対象のComposeサービス名とプロジェクトを確認し、対象が1コンテナで稼働していることを確認します。 |
| `Target image does not match` | Composeを同じファイルと `--env-file` 指定で展開し、TOMLの `image` と追跡中のタグが一致するか確認します。 |
| `Running container does not match the configured image` | 稼働中のコンテナのイメージIDと、Composeで指定したタグの現在のイメージIDを比較します。手動の再タグ付けや変更途中の設定を確認します。 |
| ローカルビルドの引数で失敗 | Composeの `build.args` が `busybox:1.34.0` などの完全な参照に展開され、Dockerfileの `FROM` がその引数を参照することを確認します。 |
| `Healthcheck timed out` | 指定URLをホストから確認し、コンテナのログとDockerのヘルスチェックを確認します。`ROLLED BACK` があれば旧版への復元結果も確認できます。 |
| `Old image is missing or changed` | `manifest.json` の `old_image_id` と保持イメージのIDを比較します。別のイメージに同じタグを付けても復元条件は満たしません。 |
| `Another updater process is running` | 同じバックアップ先を使う実行が進行中です。systemdサービスの状態を確認し、実行の終了を待ちます。 |
| `Interrupted transaction` | 下記の手順で中断箇所と復元に使うバックアップを確認します。 |

中断時は `sudo svc-update status registry-demo`、systemdのログ、`backup_root/registry-demo/inflight.json` を確認します。`stage` と、記録されていれば `backup` の値を見て、稼働中のイメージ、`versions.env`、データの状態を照合します。復元すべきバックアップがある場合は、上の手順で手動復元します。手動復元に成功すると中断ファイルが消えます。

バックアップ作成前に中断した場合は、旧コンテナの復旧と状態の照合を先に行います。更新が適用されておらず、旧構成で正常に稼働していることを確認できたら、調査記録として `inflight.json` を別の場所に退避し、候補確認を再開します。

サーバの定期ログと、BusyBoxを使った更新・世代管理・復元の実機検証結果はプロジェクトのREADMEで確認できます。[サーバ検証結果](../README.md#server-validation)
