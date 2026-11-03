#!/usr/bin/env python3
"""Conservative Docker Compose application patch updater (Python 3.11+)."""
import argparse
import datetime as dt
import stat
import uuid
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import smtplib
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

VERSION = re.compile(r'^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$')
VARIABLE = re.compile(r'^[A-Z][A-Z0-9_]*$')
SAFE_NAME = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*$')

class UpdateError(RuntimeError):
    pass

def now():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')

def atom_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp.' + str(os.getpid()))
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

def cmd(args, cwd=None, output=True, timeout=240, clean_keys=()):
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                          env={k: v for k, v in os.environ.items() if k not in {'COMPOSE_FILE', 'COMPOSE_PROJECT_NAME', 'COMPOSE_ENV_FILES', *clean_keys}})
    if proc.returncode:
        raise UpdateError(f'Command failed ({proc.returncode}): {" ".join(args[:6])}\n{proc.stderr[-1800:]}')
    return proc.stdout.strip() if output else ''

def parse_env(path):
    pairs = {}
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise UpdateError(f'Invalid env line {path}:{number}')
        key, value = line.split('=', 1)
        if not VARIABLE.fullmatch(key.strip()) or not value.strip() or value.strip()[0] in "\"'":
            raise UpdateError(f'Unsupported env syntax at {path}:{number}; use KEY=plainvalue')
        if key in pairs:
            raise UpdateError(f'Duplicate key {key} in {path}')
        pairs[key] = value.strip()
    return pairs

def replace_env(path, key, value):
    lines = path.read_text(encoding='utf-8').splitlines()
    count = 0
    for index, line in enumerate(lines):
        if re.match(r'^\s*' + re.escape(key) + r'\s*=', line):
            lines[index] = f'{key}={value}'
            count += 1
    if count != 1:
        raise UpdateError(f'Expected one {key} entry in {path}; found {count}')
    atom_write(path, '\n'.join(lines) + '\n')

def load_config(path):
    with path.open('rb') as fh:
        conf = tomllib.load(fh)
    root = Path(conf['global']['backup_root']).resolve()
    root.mkdir(parents=True, exist_ok=True)
    conf['_backup_root'] = root
    allowlist = conf['global'].get('allowed_bind_roots', [])
    if not isinstance(allowlist, list) or any(not isinstance(v, str) or not Path(v).is_absolute() or not Path(v).is_dir() for v in allowlist):
        raise UpdateError('global.allowed_bind_roots must list existing absolute directories')
    for v in allowlist:
        if Path(v).resolve() == Path('/') or any(p.is_symlink() for p in (Path(v), *Path(v).parents)):
            raise UpdateError(f'Unsafe allowed_bind_roots entry: {v}')
    services = conf.get('services', {})
    if not services:
        raise UpdateError('No [services.*] configured')
    for name, svc in services.items():
        if not SAFE_NAME.fullmatch(name):
            raise UpdateError('Invalid service name')
        svc['_name'] = name
        svc['_dir'] = Path(svc['directory']).resolve()
        if not svc['_dir'].is_dir() and svc.get("enabled", False):
            raise UpdateError(f'No directory: {svc["_dir"]}')
        for part in ('compose.yaml', '.env', 'versions.env'):
            if svc.get("enabled", False) and not (svc['_dir'] / part).is_file():
                raise UpdateError(f'Missing {part} for {name}')
        if svc.get('policy', 'patch') != 'patch':
            raise UpdateError('MVP only supports patch policy')
        if not VARIABLE.fullmatch(svc['version_variable']):
            raise UpdateError('Invalid version variable')
        if not re.fullmatch(r'[a-z0-9][a-z0-9._/-]*', svc['image']):
            raise UpdateError('Unsupported image repository')
        if not SAFE_NAME.fullmatch(svc['compose_service']):
            raise UpdateError('Invalid compose_service')
    return conf

def compose(svc, *args, version_file=None, timeout=240):
    directory = svc['_dir']
    version_file = version_file or directory / 'versions.env'
    keys = set(parse_env(directory / ".env")) | set(parse_env(version_file))
    return cmd(['docker', 'compose', '--project-directory', str(directory), '-f', str(directory / 'compose.yaml'),
                '--env-file', str(directory / '.env'), '--env-file', str(version_file), *args], cwd=directory, timeout=timeout, clean_keys=keys)

def compose_config(svc, version_file=None):
    return json.loads(compose(svc, 'config', '--format', 'json', version_file=version_file))

def current_version(svc):
    v = parse_env(svc['_dir'] / 'versions.env').get(svc['version_variable'], '')
    if not VERSION.fullmatch(v):
        raise UpdateError(f'{svc["_name"]}: version must be an exact X.Y.Z tag, got {v!r}')
    return v

def hub_tags(image, max_pages=100):
    """List tags using Docker Registry HTTP API V2 (public Docker Hub images).

    Reject incomplete pagination rather than silently missing a patch version.
    """
    fields = image.split('/')
    if len(fields) == 1:
        ns, repo = 'library', fields[0]
    elif len(fields) == 2:
        ns, repo = fields
    else:
        raise UpdateError('Only Docker Hub namespace/repository images are supported')
    if not SAFE_NAME.fullmatch(ns) or not SAFE_NAME.fullmatch(repo):
        raise UpdateError('Invalid Docker Hub repository name')

    repository = f'{ns}/{repo}'
    auth_query = urllib.parse.urlencode({
        'service': 'registry.docker.io',
        'scope': f'repository:{repository}:pull',
    })
    auth_url = 'https://auth.docker.io/token?' + auth_query
    headers = {'User-Agent': 'compose-update-manager/0.1.1', 'Accept': 'application/json'}
    try:
        req = urllib.request.Request(auth_url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as res:
            token = json.load(res).get('token')
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        raise UpdateError(f'Docker Registry token request failed: {e}') from e
    if not token:
        raise UpdateError('Docker Registry token response did not include token')

    url = f'https://registry-1.docker.io/v2/{repository}/tags/list?n=1000'
    names = []
    visited = set()
    for _ in range(max_pages):
        if url in visited:
            raise UpdateError('Registry pagination loop detected')
        visited.add(url)
        req = urllib.request.Request(url, headers={
            **headers, 'Authorization': f'Bearer {token}',
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                obj = json.load(res)
                link = res.headers.get('Link', '')
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            raise UpdateError(f'Docker Registry tag listing failed: {e}') from e
        names.extend(obj.get('tags') or [])
        # Distribution API pagination uses the RFC 5988 Link header.
        match = re.search(r'<([^>]+)>\s*;\s*rel="?next"?', link, re.I)
        if not match:
            return names
        next_url = urllib.parse.urljoin(url, match.group(1))
        parsed = urllib.parse.urlparse(next_url)
        if (parsed.scheme, parsed.hostname, parsed.port) != ('https', 'registry-1.docker.io', None):
            raise UpdateError('Unexpected registry pagination URL')
        if parsed.path != f'/v2/{repository}/tags/list':
            raise UpdateError('Unexpected registry pagination path')
        url = next_url
    raise UpdateError('Registry tag listing is incomplete (page limit)')

def next_patch(current, tags):
    base = tuple(map(int, current.split('.')))
    options = sorted((tuple(map(int, t.split('.'))) for t in tags if VERSION.fullmatch(t)
                      and tuple(map(int, t.split('.')))[:2] == base[:2] and tuple(map(int, t.split('.'))) > base))
    return '.'.join(map(str, options[0])) if options else None

def discover(svc):
    return next_patch(current_version(svc), hub_tags(svc['image']))

def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as file:
        for block in iter(lambda: file.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def under(path, root):
    """Whether path is root or a descendant of root (resolved paths)."""
    return path == root or root in path.parents


def ensure_plain_tree(source):
    """Refuse symlinks/special files; bind data must be a normal tree."""
    if source.is_symlink():
        raise UpdateError(f'Symlink bind source not supported: {source}')
    mode = source.lstat().st_mode
    if stat.S_ISREG(mode):
        return 'file'
    if not stat.S_ISDIR(mode):
        raise UpdateError(f'Special bind source not supported: {source}')
    for dirpath, dirs, files in os.walk(source, followlinks=False):
        for name in dirs + files:
            entry = Path(dirpath) / name
            st = entry.lstat().st_mode
            if not (stat.S_ISREG(st) or stat.S_ISDIR(st)):
                raise UpdateError(f'Symlink/special file in bind source: {entry}')
    return 'directory'


def check_bind_source(source, backup_root, allowed_roots, writable):
    """Validated canonical host path, guarded against destructive restore."""
    if not source or not Path(source).is_absolute():
        raise UpdateError(f'Bind source must be an absolute existing path: {source!r}')
    raw = Path(source)
    if not raw.exists():
        raise UpdateError(f'Bind source does not exist: {source}')
    if any(p.is_symlink() for p in (raw, *raw.parents)):
        raise UpdateError(f'Symlink bind path component unsupported: {source}')
    resolved = raw.resolve(strict=True)
    if resolved == Path('/') or under(backup_root, resolved) or under(resolved, backup_root):
        raise UpdateError(f'Unsafe bind/backup overlap: {resolved}')
    if writable:
        if not allowed_roots:
            raise UpdateError('RW bind mount requires global.allowed_bind_roots allowlist')
        if not any(under(resolved, root) and resolved != root for root in allowed_roots):
            raise UpdateError(f'RW bind source is outside allowed_bind_roots: {resolved}')
        if os.path.ismount(resolved):
            raise UpdateError(f'RW bind source is a mount point: {resolved}')
    return resolved


def inspect_stack(svc, config, backup_root=None, allowed_roots=()):
    target = svc['compose_service']
    services = config['services']
    if target not in services:
        raise UpdateError(f'Target {target} missing in compose config')
    if services[target].get('build'):
        raise UpdateError('Target service with build: is not supported')
    if services[target].get('image') != f'{svc["image"]}:{current_version(svc)}':
        raise UpdateError('Target image does not match image:version in config')
    volumes = config.get('volumes', {})
    for key, value in volumes.items():
        if value.get('external'):
            raise UpdateError(f'External volume {key} is unsupported')
    named = set()
    binds = {}
    backup_root = backup_root or Path('/var/backups/svc-update').resolve()
    allowed_roots = [Path(root).resolve(strict=True) for root in allowed_roots]
    for service_name, service in services.items():
        # Non-target services may have build:; --no-build prevents building them.
        for mount in service.get('volumes', []):
            if not isinstance(mount, dict):
                raise UpdateError(f'Unsupported volume specification on {service_name}')
            source = mount.get('source')
            kind = mount.get('type')
            if kind == 'volume':
                if not source or source not in volumes:
                    raise UpdateError(f'Undeclared/anonymous volume on {service_name}')
                named.add(source)
            elif kind == 'bind':
                writable = not mount.get('read_only', False)
                resolved = check_bind_source(source, backup_root, allowed_roots, writable)
                key = str(resolved)
                existing = binds.get(key)
                if existing:
                    existing['writable'] = existing['writable'] or writable
                else:
                    binds[key] = {'source': key, 'writable': writable}
            else:
                raise UpdateError(f'Unsupported mount type {kind!r} on {service_name}')
    # Never write over overlapping directories during a rollback.
    sources = [Path(x) for x in binds]
    for i, one in enumerate(sources):
        for two in sources[i + 1:]:
            if under(one, two) or under(two, one):
                raise UpdateError(f'Nested bind mounts unsupported: {one} / {two}')
    # Re-check consolidated access mode (one bind may be shared RO/RW).
    for row in binds.values():
        if row['writable']:
            check_bind_source(row['source'], backup_root, allowed_roots, True)
        row['kind'] = ensure_plain_tree(Path(row['source']))
    names = sorted(volumes[key].get('name', key) for key in named)
    for name in names:
        if not SAFE_NAME.fullmatch(name):
            raise UpdateError(f'Invalid volume name: {name}')
        data = json.loads(cmd(['docker', 'volume', 'inspect', name]))[0]
        if data.get('Driver') != 'local' or data.get('Options'):
            raise UpdateError(f'Only simple local volumes are supported: {name}')
    if not names and not binds:
        raise UpdateError('Stack has no persisted mounts to back up')
    for service_name in services:
        cid = compose(svc, 'ps', '-q', service_name)
        if not cid:
            raise UpdateError(f'{service_name}: not running, refuse unattended update')
        state = json.loads(cmd(['docker', 'inspect', cid]))[0]['State']
        if not state['Running']:
            raise UpdateError(f'{service_name}: not running')
    return {'volumes': names, 'binds': sorted(binds.values(), key=lambda r: r['source'])}

def verify_candidate(svc, candidate, config):
    if not VERSION.fullmatch(candidate):
        raise UpdateError('Candidate must be a stable X.Y.Z version')
    old = tuple(map(int, current_version(svc).split('.')))
    new = tuple(map(int, candidate.split('.')))
    if old[:2] != new[:2] or new <= old:
        raise UpdateError('Candidate must be newer within same major/minor series')
    file = svc['_dir'] / '.candidate.env'
    orig = (svc['_dir'] / 'versions.env').read_text(encoding='utf-8')
    atom_write(file, re.sub(r'^' + re.escape(svc['version_variable']) + r'=.*$',
                            svc['version_variable'] + '=' + candidate, orig, flags=re.M))
    try:
        after = compose_config(svc, file)
        if set(after['services']) != set(config['services']) or after.get('volumes') != config.get('volumes'):
            raise UpdateError('Candidate changes stack topology')
        target = svc['compose_service']
        for name in config['services']:
            prior = config['services'][name].copy()
            future = after['services'][name].copy()
            if name == target:
                if future.get('image') != f'{svc["image"]}:{candidate}':
                    raise UpdateError('Candidate image interpolation wrong')
                prior.pop('image', None)
                future.pop('image', None)
            if prior != future:
                raise UpdateError(f'Candidate changes non-image settings on {name}')
    finally:
        file.unlink(missing_ok=True)

def archive_bind(source, destination):
    """Archive a host file/directory under payload/, including metadata."""
    ensure_plain_tree(source)
    with tarfile.open(destination, 'w') as tar:
        tar.add(source, arcname='payload', recursive=True)
    # Files can change mid-archive; all stack containers must be stopped first.


def verify_bind_archive(archive):
    with tarfile.open(archive, 'r') as tar:
        members = tar.getmembers()
        if not members or members[0].name != 'payload':
            raise UpdateError(f'Malformed bind archive: {archive}')
        for member in members:
            member_path = Path(member.name)
            if (member_path.is_absolute() or '..' in member_path.parts
                    or member_path.parts[0] != 'payload'
                    or not (member.isfile() or member.isdir())):
                raise UpdateError(f'Unsafe bind archive member: {member.name}')


def backup(svc, root, mounts, old, old_image_id):
    path = root / svc['_name'] / now()
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    shutil.copy2(svc['_dir'] / 'versions.env', path / 'versions.env')
    records = []
    for index, volume in enumerate(mounts['volumes']):
        archive = f'volume-{index:02d}.tar'
        cmd(['docker', 'run', '--rm', '--network', 'none', '--mount', f'type=volume,source={volume},target=/source,readonly',
             '--mount', f'type=bind,source={path},target=/backup', svc.get('archive_image', 'busybox:1.36.1'),
             'sh', '-c', f'cd /source && tar -cpf /backup/{archive} .'], timeout=3600)
        with tarfile.open(path / archive) as t:
            if t.getmembers() is None:
                raise UpdateError('Unreadable archive')
        records.append({'volume': volume, 'archive': archive, 'sha256': sha256(path / archive)})
    bind_records = []
    for index, mount in enumerate(mounts['binds']):
        if not mount['writable']:
            continue  # Read-only bind paths are NOT modified by the updater.
        source = Path(mount['source'])
        kind = ensure_plain_tree(source)
        if kind != mount['kind']:
            raise UpdateError(f'Bind source type changed: {source}')
        archive = f'bind-{index:02d}.tar'
        archive_bind(source, path / archive)
        verify_bind_archive(path / archive)
        bind_records.append({'source': str(source), 'kind': kind,
                             'archive': archive, 'sha256': sha256(path / archive)})
    manifest = {'format': 2, 'service': svc['_name'], 'created_at': now(), 'old_version': old,
                'old_image_id': old_image_id, 'volumes': records, 'binds': bind_records,
                'versions_sha256': sha256(path / 'versions.env')}
    atom_write(path / 'manifest.json', json.dumps(manifest, indent=2) + '\n')
    return path


def replace_bind_from_archive(archive, source, expected_kind):
    """Extract to sibling staging and replace original with rollback-on-error."""
    actual_kind = ensure_plain_tree(source)
    if actual_kind != expected_kind or os.path.ismount(source):
        raise UpdateError(f'Bind source changed type or mount status: {source}')
    token = uuid.uuid4().hex
    staging = source.parent / f'.svc-update-stage-{token}'
    previous = source.parent / f'.svc-update-previous-{token}'
    try:
        staging.mkdir(mode=0o700)
        with tarfile.open(archive, 'r') as tar:
            tar.extractall(path=staging, filter='data')
        payload = staging / 'payload'
        if ensure_plain_tree(payload) != expected_kind:
            raise UpdateError(f'Bind archive source type mismatch: {source}')
        os.rename(source, previous)
        try:
            os.rename(payload, source)
        except Exception:
            os.rename(previous, source)
            raise
        # Removing the old data is only safe after the new path is committed.
        if expected_kind == 'directory':
            shutil.rmtree(previous)
        else:
            previous.unlink()
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def restore(svc, path, with_versions=True, allowed_roots=(), backup_root=None):
    path = path.resolve()
    manifest_path = path / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest['service'] != svc['_name']:
        raise UpdateError('Wrong backup service')
    if sha256(path / 'versions.env') != manifest['versions_sha256']:
        raise UpdateError('Backup versions checksum mismatch')
    for row in manifest['volumes']:
        if sha256(path / row['archive']) != row['sha256']:
            raise UpdateError('Backup archive checksum mismatch')
        with tarfile.open(path / row['archive']) as t:
            for member in t.getmembers():
                if member.name.startswith('/') or '..' in Path(member.name).parts:
                    raise UpdateError('Unsafe archive path')
    # Validate ALL bind archives and destinations before changing versions or stopping containers.
    for row in manifest.get('binds', []):
        archive = path / row['archive']
        if sha256(archive) != row['sha256']:
            raise UpdateError('Backup bind checksum mismatch')
        verify_bind_archive(archive)
        check_bind_source(row['source'], backup_root or path.parent.parent, allowed_roots, True)
        if ensure_plain_tree(Path(row['source'])) != row['kind']:
            raise UpdateError(f'Bind source type changed: {row["source"]}')
    if with_versions:
        atom_write(svc['_dir'] / 'versions.env', (path / 'versions.env').read_text(encoding='utf-8'))
    # Ensure existing containers release bind mounts and volumes before restoring.
    compose(svc, 'down', '--remove-orphans', timeout=300)
    for row in manifest['volumes']:
        cmd(['docker', 'run', '--rm', '--network', 'none', '--mount', f'type=volume,source={row["volume"]},target=/target',
             '--mount', f'type=bind,source={path},target=/backup,readonly', svc.get('archive_image', 'busybox:1.36.1'),
             'sh', '-c', f'cd /target && rm -rf -- ./* ./.[!.]* ./..?* && tar -xpf /backup/{row["archive"]}'], timeout=3600)
    for row in manifest.get('binds', []):
        replace_bind_from_archive(path / row['archive'], Path(row['source']), row['kind'])
    current_id = cmd(['docker', 'image', 'inspect', '-f', '{{.Id}}', f'{svc["image"]}:{manifest["old_version"]}'])
    if current_id != manifest['old_image_id']:
        raise UpdateError('Old image identity changed; manual recovery required')
    compose(svc, 'up', '-d', '--no-build', '--pull', 'never', timeout=600)
    health(svc)

def health(svc):
    deadline = time.monotonic() + int(svc.get('health_timeout_seconds', 150))
    last = 'not yet checked'
    while time.monotonic() < deadline:
        try:
            config = compose_config(svc)
            for service in config['services']:
                cid = compose(svc, 'ps', '-q', service)
                if not cid:
                    raise UpdateError(f'No container: {service}')
                state = json.loads(cmd(['docker', 'inspect', cid]))[0]['State']
                if not state['Running'] or state.get('Health', {}).get('Status') == 'unhealthy':
                    raise UpdateError(f'{service} not healthy')
                if state.get('Health') and state['Health']['Status'] != 'healthy':
                    raise UpdateError(f'{service} healthcheck pending')
            url = svc.get('health_url')
            if not url:
                raise UpdateError('No health_url configured')
            parsed = urllib.parse.urlparse(url)
            if parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.scheme != 'http':
                raise UpdateError('Only localhost http health_url is allowed')
            with urllib.request.urlopen(url, timeout=5) as response:
                result_url = urllib.parse.urlparse(response.geturl())
                if result_url.hostname not in ("127.0.0.1", "localhost") or result_url.scheme != "http":
                    raise UpdateError("Healthcheck redirected off localhost")
                if response.status != 200:
                    raise UpdateError(f'HTTP health status {response.status}')
            return
        except Exception as exc:
            last = str(exc)
            time.sleep(5)
    raise UpdateError(f'Healthcheck timed out: {last}')

def notify(conf, title, message):
    email = conf.get('email', {})
    if not email.get('enabled', False):
        print('[notification disabled]', title, message)
        return
    msg = EmailMessage()
    msg['Subject'] = '[svc-update] ' + title
    msg['From'] = email['from']
    msg['To'] = ', '.join(email['to'])
    msg.set_content(message)
    with smtplib.SMTP(email['host'], int(email.get('port', 25)), timeout=15) as smtp:
        if email.get('starttls', False):
            smtp.starttls()
        user = os.environ.get('SVC_UPDATE_SMTP_USER')
        passwd = os.environ.get('SVC_UPDATE_SMTP_PASSWORD')
        if user and passwd:
            smtp.login(user, passwd)
        smtp.send_message(msg)

def emit(conf, title, message):
    print(title + '\n' + message)
    try:
        notify(conf, title, message)
    except Exception as exc:
        print('WARNING: email delivery failed:', exc, file=sys.stderr)

def make_journal(root, svc, stage, **kwargs):
    state = root / svc['_name'] / 'inflight.json'
    atom_write(state, json.dumps({'stage': stage, 'time': now(), **kwargs}, indent=2) + '\n')
    return state

def do_upgrade(conf, svc, candidate, apply=False):
    old = current_version(svc)
    if not candidate:
        print(f'{svc["_name"]}: {old}: no newer patch tag')
        return
    if not apply:
        print(f'{svc["_name"]}: DRY RUN {old} -> {candidate}. --apply required for changes')
        return
    root = conf['_backup_root']
    journal = root / svc['_name'] / 'inflight.json'
    if journal.exists():
        raise UpdateError(f'Existing interrupted transaction: {journal}. Manual review required')
    config = compose_config(svc)
    mounts = inspect_stack(svc, config, root, conf['global'].get('allowed_bind_roots', []))
    verify_candidate(svc, candidate, config)
    # All network/pull work happens before any outage.
    cmd(['docker', 'pull', f'{svc["image"]}:{candidate}'], timeout=1200)
    cmd(['docker', 'pull', svc.get('archive_image', 'busybox:1.36.1')], timeout=1200)
    previous_id = cmd(['docker', 'image', 'inspect', '-f', '{{.Id}}', f'{svc["image"]}:{old}'])
    backup_dir = None
    changed = False
    try:
        make_journal(root, svc, 'stopping', old=old, new=candidate)
        compose(svc, 'stop', timeout=360)
        make_journal(root, svc, 'backing_up', old=old, new=candidate)
        backup_dir = backup(svc, root, mounts, old, previous_id)
        make_journal(root, svc, 'deploying', old=old, new=candidate, backup=str(backup_dir))
        replace_env(svc['_dir'] / 'versions.env', svc['version_variable'], candidate)
        changed = True
        compose(svc, 'up', '-d', '--no-build', '--pull', 'never', timeout=600)
        health(svc)
        make_journal(root, svc, 'committed', old=old, new=candidate, backup=str(backup_dir))
        emit(conf, f'SUCCESS {svc["_name"]} {old} -> {candidate}', f'Backup: {backup_dir}')
        journal.unlink(missing_ok=True)
    except Exception as exc:
        failure = str(exc)
        if backup_dir and changed:
            try:
                make_journal(root, svc, 'rolling_back', old=old, new=candidate, backup=str(backup_dir), failure=failure)
                restore(svc, backup_dir, allowed_roots=conf['global'].get('allowed_bind_roots', []), backup_root=root)
                emit(conf, f'ROLLED BACK {svc["_name"]}', f'Update failed: {failure}\nRestore verified. Backup: {backup_dir}')
                journal.unlink(missing_ok=True)
            except Exception as restore_exc:
                emit(conf, f'CRITICAL RESTORE FAILED {svc["_name"]}', f'Update failed: {failure}\nRestore failed: {restore_exc}\nManual intervention required. Journal: {journal}')
                raise UpdateError('CRITICAL: automatic rollback failed; manual intervention required') from restore_exc
        else:
            try:
                if journal.exists():
                    compose(svc, 'start', timeout=300)
                    health(svc)
                    journal.unlink(missing_ok=True)
            except Exception as recovery_exc:
                emit(conf, f'CRITICAL EARLY FAILURE {svc["_name"]}', f'{failure}\nStart failed: {recovery_exc}\nJournal: {journal}')
                raise
            emit(conf, f'ABORTED {svc["_name"]}', f'{failure}\nNo new version deployed.')
        raise UpdateError(f'Upgrade failed: {failure}') from exc

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', default='/etc/svc-update/config.toml')
    sub = p.add_subparsers(dest='action', required=True)
    for action in ('status', 'check', 'upgrade'):
        s = sub.add_parser(action)
        s.add_argument('service', nargs='?')
        if action == 'upgrade':
            s.add_argument('--apply', action='store_true')
    s = sub.add_parser('run', help='Check all auto_update=true services')
    s.add_argument('--apply', action='store_true')
    s = sub.add_parser('restore', help='Manual restore from a verified backup')
    s.add_argument('service')
    s.add_argument('--backup-id', required=True)
    s.add_argument('--apply', action='store_true')
    args = p.parse_args()
    conf = load_config(Path(args.config))
    lock = conf['_backup_root'] / '.svc-update.lock'
    with open(lock, 'a+') as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError('Another updater process is running')
        if args.action == 'restore':
            svc = conf['services'][args.service]
            if not re.fullmatch(r'\d{8}T\d{6}Z', args.backup_id):
                raise UpdateError('Invalid backup ID')
            path = conf['_backup_root'] / svc['_name'] / args.backup_id
            if not (path / 'manifest.json').exists():
                raise UpdateError('Backup not found')
            if not args.apply:
                print('DRY RUN: restore would overwrite current data:', path)
                return
            restore(svc, path, allowed_roots=conf['global'].get('allowed_bind_roots', []), backup_root=conf['_backup_root'])
            (conf['_backup_root'] / svc['_name'] / 'inflight.json').unlink(missing_ok=True)
            emit(conf, f'MANUAL RESTORE VERIFIED {svc["_name"]}', f'Backup: {path}')
            return
        services = conf['services']
        if getattr(args, 'service', None):
            if args.service not in services:
                raise UpdateError(f'Unknown service: {args.service}')
            services = {args.service: services[args.service]}
        if args.action == 'run':
            services = {k: v for k, v in services.items() if v.get('enabled', False) and v.get('auto_update', False)}
        failed = False
        for name, svc in services.items():
            if not svc.get('enabled', False):
                print(f'{name}: disabled')
                continue
            try:
                old = current_version(svc)
                if args.action == 'status':
                    state = conf['_backup_root'] / name / 'inflight.json'
                    print(f'{name}: configured={old}, auto_update={svc.get("auto_update", False)}, interrupted={state.exists()}')
                else:
                    if (conf['_backup_root'] / name / 'inflight.json').exists():
                        raise UpdateError('Interrupted transaction; inspect journal before proceeding')
                    candidate = discover(svc)
                    if args.action == 'check':
                        print(f'{name}: {old} -> {candidate or "no eligible patch"}')
                    else:
                        do_upgrade(conf, svc, candidate, args.apply)
            except Exception as exc:
                failed = True
                print(f'{name}: ERROR: {exc}', file=sys.stderr)
        if failed:
            sys.exit(1)

if __name__ == '__main__':
    try:
        main()
    except (UpdateError, OSError, KeyError, ValueError, urllib.error.URLError, subprocess.TimeoutExpired) as e:
        print('ERROR:', e, file=sys.stderr)
        sys.exit(1)
