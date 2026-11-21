#!/usr/bin/env python3
"""Conservative Docker Compose image updater (Python 3.11+)."""
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
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from email.message import EmailMessage
from typing import NamedTuple

VERSION = re.compile(r'^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([a-z0-9][a-z0-9._-]*))?$')
POLICIES = {'patch', 'minor', 'major', 'any', 'latest'}
COMPOSE_NAMES = ('compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml')
VARIABLE = re.compile(r'^[A-Z][A-Z0-9_]*$')
SAFE_NAME = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*$')
LOCAL_IMAGE = re.compile(r'(?:[a-z0-9.-]+:\d+/)?[a-z0-9][a-z0-9._/-]*(?::[\w][A-Za-z0-9_.-]{0,127})?', re.ASCII)

class UpdateError(RuntimeError):
    pass


class ImageVersion(NamedTuple):
    version: str
    ref: str
    id: str


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
        details = proc.stderr.strip() or proc.stdout.strip()
        if len(details) > 1800:
            details = f'{details[:1200]}\n...\n{details[-600:]}'
        raise UpdateError(f'Command failed ({proc.returncode}): {" ".join(args[:6])}\n{details}')
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

def parse_version(tag):
    """(major, minor, patch), exact flavor. Qualifiers are immutable flavors, not SemVer pre-releases."""
    match = VERSION.fullmatch(tag)
    if not match:
        raise UpdateError(f'Unsupported image tag {tag!r}; expected X.Y.Z or X.Y.Z-<flavor>')
    return tuple(map(int, match.group(1, 2, 3))), match.group(4) or ''


def select_compose_files(directory, svc):
    """Resolve explicit -f files or one standard Compose filename (never merge implicitly)."""
    selected = svc.get('compose_files')
    if selected is None:
        selected = [svc['compose_file']] if 'compose_file' in svc else [n for n in COMPOSE_NAMES if (directory / n).is_file()][:1]
    if not isinstance(selected, list) or not selected or any(not isinstance(item, str) for item in selected):
        raise UpdateError('Set compose_file or a nonempty compose_files list')
    if len(set(selected)) != len(selected):
        raise UpdateError('Duplicate Compose file configured')
    paths = []
    for relative in selected:
        rel = Path(relative)
        if rel.is_absolute() or '..' in rel.parts or not relative.endswith(('.yml', '.yaml')):
            raise UpdateError(f'Unsafe Compose path: {relative}')
        file = directory / rel
        if not file.is_file() or not file.resolve().is_relative_to(directory):
            raise UpdateError(f'Compose file not found under {directory}: {relative}')
        paths.append(file)
    return paths


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
        keep = svc.get('keep_previous_images')
        if keep is not None and (type(keep) is not int or keep < 0):
            raise UpdateError(f'{name}: keep_previous_images must be a nonnegative integer')
        if not svc.get('enabled', False):
            continue
        if not svc['_dir'].is_dir():
            raise UpdateError(f'No directory: {svc["_dir"]}')
        for part in ('.env', 'versions.env'):
            if not (svc['_dir'] / part).is_file():
                raise UpdateError(f'Missing {part} for {name}')
        svc['_compose_files'] = select_compose_files(svc['_dir'], svc)
        policy = svc.get('policy', 'patch')
        if policy not in POLICIES:
            raise UpdateError(f'Invalid policy {policy!r}; expected one of {sorted(POLICIES)}')
        if policy in {'major', 'latest'} and svc.get('auto_update', False) and not svc.get('allow_breaking_auto_update', False):
            raise UpdateError(f'{name}: {policy} auto_update requires allow_breaking_auto_update = true')
        if not VARIABLE.fullmatch(svc['version_variable']):
            raise UpdateError('Invalid version variable')
        if not re.fullmatch(r'[a-z0-9][a-z0-9._/-]*', svc['image']):
            raise UpdateError('Unsupported image repository')
        if not SAFE_NAME.fullmatch(svc['compose_service']):
            raise UpdateError('Invalid compose_service')
        if 'local_build' in svc:
            build = svc['local_build']
            if not isinstance(build, dict) or not VARIABLE.fullmatch(str(build.get('base_argument', ''))):
                raise UpdateError(f'{name}: set local_build.base_argument to a Dockerfile ARG name')
    return conf


def compose(svc, *args, version_file=None, extra_files=(), timeout=240):
    directory = svc['_dir']
    version_file = version_file or directory / 'versions.env'
    keys = set(parse_env(directory / '.env')) | set(parse_env(version_file))
    files = svc.get('_compose_files') or select_compose_files(directory, svc)
    flags = [arg for file in (*files, *extra_files) for arg in ('-f', str(file))]
    return cmd(['docker', 'compose', '--project-directory', str(directory), *flags,
                '--env-file', str(directory / '.env'), '--env-file', str(version_file), *args],
               cwd=directory, timeout=timeout, clean_keys=keys)


def compose_config(svc, version_file=None):
    return json.loads(compose(svc, 'config', '--format', 'json', version_file=version_file))


def current_version(svc):
    version = parse_env(svc['_dir'] / 'versions.env').get(svc['version_variable'], '')
    parse_version(version)
    return version

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
    headers = {'User-Agent': 'compose-update-manager/0.3', 'Accept': 'application/json'}
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

def next_version(current, tags, policy='patch'):
    """Select version strictly within the same flavor. Never silently switch image variants."""
    if policy not in POLICIES:
        raise UpdateError(f'Unsupported update policy: {policy}')
    base, flavor = parse_version(current)
    eligible = []
    for tag in tags:
        try:
            version, candidate_flavor = parse_version(tag)
        except (UpdateError, TypeError):
            continue
        if candidate_flavor != flavor or version <= base:
            continue
        if policy == 'patch' and version[:2] != base[:2]:
            continue
        if policy == 'minor' and (version[0] != base[0] or version[1] <= base[1]):
            continue
        if policy == 'major' and version[0] <= base[0]:
            continue
        eligible.append((version, tag))
    if not eligible:
        return None
    if policy == 'minor':
        closest_minor = min(row[0][1] for row in eligible)
        eligible = [row for row in eligible if row[0][1] == closest_minor]
    if policy == 'major':
        closest_major = min(row[0][0] for row in eligible)
        eligible = [row for row in eligible if row[0][0] == closest_major]
    selector = max if policy == 'latest' else min
    return selector(eligible, key=lambda row: row[0])[1]


def next_patch(current, tags):
    """Backward-compatible helper for existing users/tests."""
    return next_version(current, tags, 'patch')


def discover(svc):
    return next_version(current_version(svc), hub_tags(svc['image']), svc.get('policy', 'patch'))

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


def image_key(ref):
    repo, colon, tag = ref.rpartition(':')
    if not colon or '/' in tag:
        repo, tag = ref, 'latest'
    if repo.startswith(('docker.io/', 'index.docker.io/')):
        repo = repo.split('/', 1)[1]
    if '/' not in repo:
        repo = 'library/' + repo
    return f'{repo}:{tag}'


def target_image(svc, config, version):
    target = svc['compose_service']
    services = config['services']
    if target not in services:
        raise UpdateError(f'Target {target} missing in compose config')
    definition = services[target]
    ref = definition.get('image', '')
    build = definition.get('build')
    upstream = f'{svc["image"]}:{version}'
    if 'local_build' not in svc:
        if build:
            raise UpdateError('Set local_build to manage a target with build:')
        if ref != upstream:
            raise UpdateError('Target image does not match image:version in config')
        return ref

    if not LOCAL_IMAGE.fullmatch(ref) or not build:
        raise UpdateError('Local build requires Compose image: and build:')
    if ':' not in ref.rsplit('/', 1)[-1]:
        ref += ':latest'
    base_arg = svc['local_build']['base_argument']
    if build.get('args', {}).get(base_arg) != upstream:
        raise UpdateError(f'Compose build.args.{base_arg} must resolve to {upstream}')
    extra_contexts = build.get('additional_contexts', {})
    if build.get('tags') or any(context.startswith('service:') for context in extra_contexts.values()):
        raise UpdateError('Local build cannot tag extra images or build other Compose services')
    key = image_key(ref)
    repo = key.rsplit(':', 1)[0]
    if repo in {image_key(upstream).rsplit(':', 1)[0], 'library/svc-update-build', 'library/svc-update-history'}:
        raise UpdateError('Local output repository must differ from the base and updater repositories')
    if any(image_key(other['image']) == key for name, other in services.items()
           if name != target and other.get('image')):
        raise UpdateError('Local output image is shared with another Compose service')
    return ref


def inspect_stack(svc, config, backup_root=None, allowed_roots=()):
    """Inspect only target persistence. Refuse data shared with running sibling services."""
    target = svc['compose_service']
    services = config['services']
    target_image(svc, config, current_version(svc))
    volumes = config.get('volumes', {})
    named = set()
    binds = {}
    backup_root = backup_root or Path('/var/backups/svc-update').resolve()
    allowed_roots = [Path(root).resolve(strict=True) for root in allowed_roots]
    target_mounts = services[target].get('volumes', [])
    for mount in target_mounts:
        if not isinstance(mount, dict):
            raise UpdateError(f'Unsupported volume specification on {target}')
        source = mount.get('source')
        kind = mount.get('type')
        if kind == 'volume':
            if not source or source not in volumes:
                raise UpdateError(f'Undeclared/anonymous volume on {target}')
            if volumes[source].get('external'):
                raise UpdateError(f'External volume {source} is unsupported')
            named.add(source)
        elif kind == 'bind':
            writable = not mount.get('read_only', False)
            resolved = check_bind_source(source, backup_root, allowed_roots, writable)
            key = str(resolved)
            if key in binds:
                binds[key]['writable'] |= writable
            else:
                binds[key] = {'source': key, 'writable': writable}
        else:
            raise UpdateError(f'Unsupported mount type {kind!r} on {target}')

    sources = [Path(x) for x in binds]
    for i, one in enumerate(sources):
        for two in sources[i + 1:]:
            if under(one, two) or under(two, one):
                raise UpdateError(f'Nested bind mounts unsupported: {one} / {two}')
    for row in binds.values():
        if row['writable']:
            check_bind_source(row['source'], backup_root, allowed_roots, True)
        row['kind'] = ensure_plain_tree(Path(row['source']))

    # No other service may retain a mounted directory/volume that the target
    # will restore: that would modify live shared storage behind its back.
    for sibling, definition in services.items():
        if sibling == target:
            continue
        for mount in definition.get('volumes', []):
            if not isinstance(mount, dict):
                raise UpdateError(f'Unsupported volume specification on {sibling}')
            if mount.get('type') == 'volume' and mount.get('source') in named:
                raise UpdateError(f'Volume {mount.get("source")} is shared with {sibling}; --no-deps update unsafe')
            if mount.get('type') == 'bind':
                other = Path(mount.get('source') or '').resolve()
                if any(row['writable'] and (under(other, Path(row['source'])) or under(Path(row['source']), other))
                       for row in binds.values()):
                    raise UpdateError(f'RW bind path overlaps with {sibling}; --no-deps update unsafe')

    names = sorted(volumes[key].get('name', key) for key in named)
    for name in names:
        if not SAFE_NAME.fullmatch(name):
            raise UpdateError(f'Invalid volume name: {name}')
        data = json.loads(cmd(['docker', 'volume', 'inspect', name]))[0]
        if data.get('Driver') != 'local' or data.get('Options'):
            raise UpdateError(f'Only simple local volumes are supported: {name}')

    container = target_container(svc)
    if not container['State']['Running']:
        raise UpdateError(f'{target}: not running')
    return {'volumes': names, 'binds': sorted(binds.values(), key=lambda row: row['source']),
            'running_image_id': container.get('Image')}


def target_container(svc):
    target = svc['compose_service']
    ids = compose(svc, 'ps', '-q', target).splitlines()
    if len(ids) != 1:
        raise UpdateError(f'{target}: expected one running container')
    return json.loads(cmd(['docker', 'inspect', ids[0]]))[0]


def image_id(ref):
    return cmd(['docker', 'image', 'inspect', '-f', '{{.Id}}', ref])


def tagged_image_id(ref):
    # The reference lookup returns an empty result when the tag is absent.
    if cmd(['docker', 'image', 'ls', '--quiet', '--no-trunc', ref]):
        return image_id(ref)
    return None


@contextmanager
def candidate_env(svc, candidate):
    with tempfile.TemporaryDirectory(prefix='svc-update-') as temp:
        file = Path(temp) / 'versions.env'
        atom_write(file, (svc['_dir'] / 'versions.env').read_text(encoding='utf-8'))
        replace_env(file, svc['version_variable'], candidate)
        yield file


def verify_candidate(svc, candidate, config, file):
    old, flavor = parse_version(current_version(svc))
    new, new_flavor = parse_version(candidate)
    policy = svc.get('policy', 'patch')
    if new_flavor != flavor or new <= old:
        raise UpdateError('Candidate must be newer and preserve the image flavor')
    if policy == 'patch' and old[:2] != new[:2]:
        raise UpdateError('Patch update cannot change major/minor')
    if policy == 'minor' and (old[0] != new[0] or new[1] <= old[1]):
        raise UpdateError('Minor update must increment minor in the same major')
    if policy == 'major' and new[0] <= old[0]:
        raise UpdateError('Major update must increment major')
    after = compose_config(svc, file)
    prior_resources = {k: v for k, v in config.items() if k != 'services'}
    next_resources = {k: v for k, v in after.items() if k != 'services'}
    if prior_resources != next_resources or set(after['services']) != set(config['services']):
        raise UpdateError('Candidate changes stack topology')
    ref = target_image(svc, after, candidate)
    for name in config['services']:
        prior = config['services'][name].copy()
        future = after['services'][name].copy()
        if name == svc['compose_service']:
            prior.pop('image', None)
            future.pop('image', None)
            if 'local_build' in svc:
                base_arg = svc['local_build']['base_argument']
                for definition in (prior, future):
                    build = definition['build']
                    definition['build'] = {**build, 'args': {k: v for k, v in build['args'].items() if k != base_arg}}
        if prior != future:
            raise UpdateError(f'Candidate changes settings outside the managed image or base argument on {name}')
    return ref


def build_candidate(svc, version_file, staged_ref):
    # A separate output tag keeps the currently deployed tag available until backup completes.
    overlay = version_file.with_name('build.json')
    overlay.write_text(json.dumps({'services': {svc['compose_service']: {'image': staged_ref}}}), encoding='utf-8')
    compose(svc, 'build', '--pull', svc['compose_service'], version_file=version_file,
            extra_files=(overlay,), timeout=3600)
    return image_id(staged_ref)

def archive_bind(source, destination):
    """Archive a host file/directory under payload/, including metadata."""
    ensure_plain_tree(source)
    with tarfile.open(destination, 'w') as tar:
        tar.add(source, arcname='payload', recursive=True)
    # Only the target is stopped; shared RW storage is rejected by inspect_stack.


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


def backup(svc, root, mounts, old, old_image_id, *, local_image=None, old_image_ref=None):
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
    manifest = {'format': 3, 'scope': 'target', 'compose_service': svc['compose_service'],
                'service': svc['_name'], 'created_at': now(), 'old_version': old,
                'old_image_id': old_image_id, 'volumes': records, 'binds': bind_records,
                'versions_sha256': sha256(path / 'versions.env')}
    if local_image is not None:
        manifest.update(local_image=local_image, old_image_ref=old_image_ref)
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
    if (manifest.get('format') != 3 or manifest.get('scope') != 'target'
            or manifest.get('compose_service') != svc['compose_service']):
        raise UpdateError('Incompatible backup scope; v0.2 full-stack archives require old recovery procedure')
    if sha256(path / 'versions.env') != manifest['versions_sha256']:
        raise UpdateError('Backup versions checksum mismatch')
    expected_id = manifest['old_image_id']
    old_ref = manifest.get('old_image_ref', f'{svc["image"]}:{manifest["old_version"]}')
    local_image = manifest.get('local_image')
    if local_image is not None:
        versions = path / 'versions.env' if with_versions else None
        restore_ref = target_image(svc, compose_config(svc, versions), manifest['old_version'])
        if image_key(restore_ref) != image_key(local_image):
            raise UpdateError('Backup local image does not match the current Compose definition')
        if tagged_image_id(local_image) == expected_id:
            old_ref = local_image
    if tagged_image_id(old_ref) != expected_id:
        raise UpdateError(f'Old image is missing or changed ({old_ref}); manual recovery required')
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
    # Stop ONLY the target: dependencies remain running during an upgrade/restore.
    target = svc['compose_service']
    compose(svc, 'stop', target, timeout=300)
    if with_versions:
        atom_write(svc['_dir'] / 'versions.env', (path / 'versions.env').read_text(encoding='utf-8'))
    for row in manifest['volumes']:
        cmd(['docker', 'run', '--rm', '--network', 'none', '--mount', f'type=volume,source={row["volume"]},target=/target',
             '--mount', f'type=bind,source={path},target=/backup,readonly', svc.get('archive_image', 'busybox:1.36.1'),
             'sh', '-c', f'cd /target && rm -rf -- ./* ./.[!.]* ./..?* && tar -xpf /backup/{row["archive"]}'], timeout=3600)
    for row in manifest.get('binds', []):
        replace_bind_from_archive(path / row['archive'], Path(row['source']), row['kind'])
    if local_image is not None and image_key(old_ref) != image_key(local_image):
        cmd(['docker', 'image', 'tag', expected_id, local_image])
    compose(svc, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', target, timeout=600)
    health(svc, expected_id)

def health(svc, expected_image_id=None):
    deadline = time.monotonic() + int(svc.get('health_timeout_seconds', 150))
    last = 'not yet checked'
    while time.monotonic() < deadline:
        try:
            config = compose_config(svc)
            service = svc['compose_service']
            if service not in config['services']:
                raise UpdateError(f'Missing target service: {service}')
            container = target_container(svc)
            if expected_image_id is not None and container['Image'] != expected_image_id:
                raise UpdateError(f'{service} is running an unexpected image')
            state = container['State']
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


def image_history(path):
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    return []


def write_image_history(path, history):
    atom_write(path, json.dumps(history, indent=2) + '\n')


def record_previous_image(path, old_ref, old_id, current_ref):
    replaced = {image_key(old_ref), image_key(current_ref)}
    history = [row for row in image_history(path) if image_key(row['ref']) not in replaced]
    write_image_history(path, [{'ref': old_ref, 'id': old_id}, *history])


def containers_using_tag(ref, docker_image_id):
    ids = cmd(['docker', 'ps', '-aq', '--filter', f'ancestor={docker_image_id}']).splitlines()
    if not ids:
        return []
    containers = json.loads(cmd(['docker', 'container', 'inspect', *ids]))
    return [row for row in containers if image_key(row['Config']['Image']) == image_key(ref)]


def prune_images(conf, svc, path, current_ref):
    keep = svc.get('keep_previous_images')
    if keep is None:
        return
    history = image_history(path)
    protected = {image_key(current_ref)}
    for name, other in conf.get('services', {}).items():
        if name != svc['_name']:
            other_history = image_history(path.parent.parent / name / path.name)
            protected.update(image_key(row['ref']) for row in other_history[:other.get('keep_previous_images')])
    remaining = history[:keep]
    for row in history[keep:]:
        ref = row['ref']
        try:
            if image_key(ref) in protected:
                remaining.append(row)
                continue
            if tagged_image_id(ref) != row['id']:
                continue  # Removed or retagged since the update.
            if containers_using_tag(ref, row['id']):
                remaining.append(row)
                print(f'{svc["_name"]}: retained {ref} (used by a container)')
                continue
            cmd(['docker', 'image', 'rm', '--no-prune', ref])
            print(f'{svc["_name"]}: removed old image {ref}')
        except (UpdateError, OSError, subprocess.TimeoutExpired) as exc:
            remaining.append(row)
            print(f'WARNING: could not remove old image {ref}: {exc}', file=sys.stderr)
    if remaining != history:
        write_image_history(path, remaining)


def remove_temporary_image(ref, expected_id=None):
    try:
        found = tagged_image_id(ref)
        if found and (expected_id is None or found == expected_id):
            cmd(['docker', 'image', 'rm', '--no-prune', ref])
    except (UpdateError, OSError, subprocess.TimeoutExpired) as exc:
        print(f'WARNING: could not remove temporary image {ref}: {exc}', file=sys.stderr)


def do_upgrade(conf, svc, candidate, apply=False):
    old = current_version(svc)
    if not candidate:
        print(f'{svc["_name"]}: {old}: no eligible {svc.get("policy", "patch")} tag')
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
    old_ref = target_image(svc, config, old)
    previous_id = image_id(old_ref)
    if mounts.get('running_image_id') not in (None, previous_id):
        raise UpdateError(f'Running container does not match the configured image {old_ref}')
    local = 'local_build' in svc
    staged_ref = archive_ref = new_ref = new_id = None
    existing_new_id = None
    committed = False
    try:
        # Resolve and build/pull the candidate while the old container is running.
        with candidate_env(svc, candidate) as version_file:
            new_ref = verify_candidate(svc, candidate, config, version_file)
            if local:
                existing_new_id = tagged_image_id(new_ref)
                if image_key(old_ref) == image_key(new_ref):
                    running = target_container(svc)['Id']
                    if any(row['Id'] != running for row in containers_using_tag(old_ref, previous_id)):
                        raise UpdateError(f'Local image tag {old_ref} is shared with another container')
                staged_ref = f'svc-update-build:{uuid.uuid4().hex}'
                new_id = build_candidate(svc, version_file, staged_ref)
                if image_key(old_ref) == image_key(new_ref):
                    archive_ref = f'svc-update-history:{uuid.uuid4().hex}'
                    cmd(['docker', 'image', 'tag', previous_id, archive_ref])
                elif existing_new_id not in (None, new_id):
                    raise UpdateError(f'Candidate local image tag already exists with a different image: {new_ref}')
            else:
                cmd(['docker', 'pull', new_ref], timeout=1200)
                new_id = image_id(new_ref)
        if mounts['volumes']:
            cmd(['docker', 'pull', svc.get('archive_image', 'busybox:1.36.1')], timeout=1200)
        if image_id(old_ref) != previous_id:
            raise UpdateError(f'Configured image tag changed during preparation: {old_ref}')
        previous = ImageVersion(old, archive_ref or old_ref, previous_id)
        upcoming = ImageVersion(candidate, new_ref, new_id)
        backup_dir = deploy_update(conf, svc, mounts, previous, upcoming, old_ref, existing_new_id)
        committed = True
    finally:
        if staged_ref:
            remove_temporary_image(staged_ref, new_id)
        if archive_ref and not committed and not journal.exists():
            remove_temporary_image(archive_ref, previous_id)
    journal.unlink(missing_ok=True)
    message = f'Backup: {backup_dir}'
    if local:
        message += f'\nLocal image: {new_ref}\nBase image: {svc["image"]}:{candidate}'
    emit(conf, f'SUCCESS {svc["_name"]} {old} -> {candidate}', message)
    try:
        prune_images(conf, svc, journal.with_name('image-history.json'), new_ref)
    except (UpdateError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f'WARNING: image cleanup failed: {exc}', file=sys.stderr)


def deploy_update(conf, svc, mounts, previous, upcoming, old_ref, existing_new_id):
    root = conf['_backup_root']
    journal = root / svc['_name'] / 'inflight.json'
    local = 'local_build' in svc
    if local and tagged_image_id(upcoming.ref) != existing_new_id:
        raise UpdateError(f'Candidate local image tag changed during preparation: {upcoming.ref}')
    old, candidate = previous.version, upcoming.version
    backup_dir = None
    changed = False
    created_tag = False
    try:
        make_journal(root, svc, 'stopping', old=old, new=candidate)
        compose(svc, 'stop', svc['compose_service'], timeout=360)
        make_journal(root, svc, 'backing_up', old=old, new=candidate)
        backup_dir = backup(svc, root, mounts, old, previous.id,
                            local_image=old_ref if local else None, old_image_ref=previous.ref)
        make_journal(root, svc, 'deploying', old=old, new=candidate, backup=str(backup_dir))
        changed = True
        if local:
            cmd(['docker', 'image', 'tag', upcoming.id, upcoming.ref])
            created_tag = existing_new_id is None
        replace_env(svc['_dir'] / 'versions.env', svc['version_variable'], candidate)
        compose(svc, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', svc['compose_service'], timeout=600)
        health(svc, upcoming.id)
        make_journal(root, svc, 'committed', old=old, new=candidate, backup=str(backup_dir))
        record_previous_image(journal.with_name('image-history.json'), previous.ref, previous.id, upcoming.ref)
    except Exception as exc:
        failure = str(exc)
        if backup_dir and changed:
            try:
                make_journal(root, svc, 'rolling_back', old=old, new=candidate, backup=str(backup_dir), failure=failure)
                restore(svc, backup_dir, allowed_roots=conf['global'].get('allowed_bind_roots', []), backup_root=root)
                if created_tag:
                    remove_temporary_image(upcoming.ref, upcoming.id)
                emit(conf, f'ROLLED BACK {svc["_name"]}', f'Update failed: {failure}\nRestore verified. Backup: {backup_dir}')
                journal.unlink(missing_ok=True)
            except Exception as restore_exc:
                emit(conf, f'CRITICAL RESTORE FAILED {svc["_name"]}', f'Update failed: {failure}\nRestore failed: {restore_exc}\nManual intervention required. Journal: {journal}')
                raise UpdateError('CRITICAL: automatic rollback failed; manual intervention required') from restore_exc
        else:
            try:
                if journal.exists():
                    compose(svc, 'start', svc['compose_service'], timeout=300)
                    health(svc)
                    journal.unlink(missing_ok=True)
            except Exception as recovery_exc:
                emit(conf, f'CRITICAL EARLY FAILURE {svc["_name"]}', f'{failure}\nStart failed: {recovery_exc}\nJournal: {journal}')
                raise
            emit(conf, f'ABORTED {svc["_name"]}', f'{failure}\nNo new version deployed.')
        raise UpdateError(f'Upgrade failed: {failure}') from exc
    return backup_dir

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
                        print(f'{name}: {old} -> {candidate or "no eligible update"}')
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
