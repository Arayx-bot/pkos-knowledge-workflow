#!/usr/bin/env python3
"""Local tool ledger, bounded source audit, synchronization and pre-push gate.

Stdlib only. Findings contain locations, never matched values. No background work.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import importlib.util
import types

START = '<!-- tool-sync-index:start -->'
END = '<!-- tool-sync-index:end -->'
MAX_FILE = 20 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024
PATTERNS = {
    'private-key': r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----',
    'provider-token': r'\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b',
    'jwt': r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b',
    'email': r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',
    'cn-phone': r'(?<![\w])(?:\+?86[ -]?)?1[3-9]\d{9}(?![\w])',
    'password-url': r'\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@',
    'bearer': r'\bBearer[ \t]+[A-Za-z0-9._~+/-]{16,}',
    'credential-assignment': r'''(?i)\b(?:api[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token|password|private[_-]?key)\b["']?[ \t]*[:=][ \t]*["'][^\s"']{8,}["']''',
    'npm-auth': r'(?im)^\s*(?://[^\s]+/:)?_(?:authToken|auth)\s*=\s*[^\s${]+',
}
ASSETS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.pdf', '.mp3', '.wav', '.mp4', '.mov', '.zip', '.docx', '.xlsx'}


class Blocked(Exception):
    def __init__(self, reason, details=None):
        self.reason, self.details = reason, details or []
        super().__init__(reason)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(args, cwd=None, binary=False):
    env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_LAZY_FETCH': '1',
           'GIT_TERMINAL_PROMPT': '0', 'PYTHONDONTWRITEBYTECODE': '1'}
    p = subprocess.run(args, cwd=cwd, env=env, capture_output=True, timeout=120)
    if p.returncode:
        # Command output can contain credentials or private paths. Do not echo it.
        raise Blocked('command_failed', [{'command': args[0], 'exit_code': p.returncode}])
    return p.stdout if binary else p.stdout.decode('utf-8', errors='strict').strip()


def git(root, *args, binary=False):
    return run(['git', '-C', str(root), *args], binary=binary)


def load_ledger(path):
    text = Path(path).read_text()
    if text.count(START) != 1 or text.count(END) != 1:
        raise Blocked('ledger_index_missing_or_duplicated')
    block = text.split(START)[1].split(END)[0].strip()
    data = json.loads(block.removeprefix('```json').removesuffix('```').strip())
    if data.get('schema') != 1:
        raise Blocked('unsupported_ledger_schema')
    return data


def save_ledger(path, data):
    p = Path(path)
    text = p.read_text()
    before, remainder = text.split(START)
    _, after = remainder.split(END)
    # Human-readable entries and the embedded index belong to the same ledger.
    for tool in data['tools']:
        repo = data['repositories'].get(tool.get('repository'))
        if repo and repo.get('last_pushed_sha'):
            pattern = r'(### ' + re.escape(tool['id']) + r' · .*?)(?=\n### TOOL-|\n## |\Z)'
            match = re.search(pattern, before, re.S)
            if match:
                block = re.sub(r'(main `)[0-9a-f]{40}(`)', r'\g<1>' + repo['last_pushed_sha'] + r'\2', match[1])
                before = before[:match.start()] + block + before[match.end():]
    updated = before + START + '\n```json\n' + json.dumps(data, ensure_ascii=False, indent=2) + '\n```\n' + END + after
    with tempfile.NamedTemporaryFile('w', dir=p.parent, delete=False) as f:
        f.write(updated)
        tmp = Path(f.name)
    tmp.chmod(p.stat().st_mode & 0o777)
    tmp.replace(p)


def repo_for(data, name):
    if name not in data['repositories']:
        raise Blocked('unregistered_repository')
    return data['repositories'][name]


def paths_from_status(root):
    # Rename detection disabled: deletion and addition remain explicit manifest items.
    raw = git(root, '-c', 'status.renames=false', 'status', '--porcelain=v1', '-z', '--untracked-files=all', binary=True)
    return [{'status': s[:2].decode(), 'path': os.fsdecode(s[3:])} for s in raw.split(b'\0') if s]


def safe_rel(rel):
    p = PurePosixPath(rel)
    if p.is_absolute() or '..' in p.parts or not rel or '\x00' in rel or '\n' in rel or '\r' in rel:
        raise Blocked('unsafe_relative_path')
    return p


def forbidden(rel):
    p = safe_rel(rel)
    name = p.name.lower()
    example = any(x in name for x in ['.example', '.sample', '.template'])
    return (name.startswith('.env') and not example) or name in {'.admin-token', 'auth.json', 'web-push.json'} or (
        p.suffix.lower() in {'.key', '.pem', '.p12', '.pfx', '.db', '.sqlite', '.sqlite3'}) or (
        any(x in p.parts for x in ['node_modules', '.venv', '.ssh', '.wrangler'])) or rel == '.openai/hosting.json'


def scan_bytes(rel, raw, mode='100644', approvals=()):
    safe_rel(rel)
    sha = digest(raw)
    findings = []
    if forbidden(rel):
        findings.append({'path': rel, 'line': 0, 'kind': 'private-or-runtime-path'})
    if mode not in {'100644', '100755'}:
        findings.append({'path': rel, 'line': 0, 'kind': 'unreviewed-path-type'})
    if len(raw) > MAX_FILE:
        findings.append({'path': rel, 'line': 0, 'kind': 'oversize-file'})
    allowed = {(a['path'], a['sha256'], a['line'], a['kind']) for a in approvals}
    binary = b'\0' in raw
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        text, binary = '', True
    if binary or PurePosixPath(rel).suffix.lower() in ASSETS:
        findings.append({'path': rel, 'line': 0, 'kind': 'unreviewed-asset'})
    if not binary:
        for kind, pattern in PATTERNS.items():
            for match in re.finditer(pattern, text):
                findings.append({'path': rel, 'line': text.count('\n', 0, match.start()) + 1, 'kind': kind})
    return [{**f, 'sha256': sha} for f in findings if (rel, sha, f['line'], f['kind']) not in allowed]


def validate_identity(root, repo):
    if Path(git(root, 'rev-parse', '--show-toplevel')).resolve() != Path(root).resolve():
        raise Blocked('repository_root_mismatch')
    if git(root, 'remote', 'get-url', '--all', 'origin').splitlines() != [repo['ssh_url']] or git(root, 'remote', 'get-url', '--push', '--all', 'origin').splitlines() != [repo['ssh_url']]:
        raise Blocked('remote_changed')


def remote_metadata(data, name):
    repo = repo_for(data, name)
    account = run(['gh', 'api', 'user', '--jq', '.login'])
    if account != data['account']:
        raise Blocked('github_account_mismatch')
    meta = json.loads(run(['gh', 'api', f'repos/{account}/{name}']))
    if not meta.get('private') or meta.get('default_branch') != 'main':
        raise Blocked('remote_not_private_main')
    query = subprocess.run(['gh', 'api', f'repos/{account}/{name}/git/matching-refs/heads/main'],
                           capture_output=True, timeout=115)
    if query.returncode:
        try:
            error = json.loads(query.stdout)
        except (ValueError, UnicodeDecodeError):
            error = {}
        # GitHub returns 409 instead of an empty ref list for an empty repository.
        # Require the exact API response AND an independent empty SSH ref list.
        if (meta.get('size') != 0 or b'HTTP 409' not in query.stderr or
                error.get('message') != 'Git Repository is empty.'):
            raise Blocked('private_remote_refs_query_failed')
        if run(['git', 'ls-remote', repo['ssh_url']]):
            raise Blocked('private_empty_repository_refs_mismatch')
        return '0' * 40
    refs = json.loads(query.stdout)
    matches = [r for r in refs if r.get('ref') == 'refs/heads/main']
    if len(matches) == 1:
        return matches[0]['object']['sha']
    if not matches and meta.get('size') == 0:
        return '0' * 40  # A verified private, genuinely empty new repository.
    raise Blocked('remote_main_missing')


def scan_tree(root, commit, approvals, cache=None):
    cache = cache if cache is not None else {}
    findings, total = [], 0
    for entry in git(root, 'ls-tree', '-rz', '--full-tree', commit, binary=True).split(b'\0'):
        if not entry:
            continue
        meta, path = entry.split(b'\t', 1)
        mode, typ, oid = meta.decode().split()
        rel = os.fsdecode(path)
        if typ != 'blob':
            findings.append({'path': rel, 'line': 0, 'kind': 'unreviewed-submodule'})
            continue
        key = (rel, oid, mode)
        size = int(git(root, 'cat-file', '-s', oid))
        total += size
        if total > MAX_TOTAL:
            raise Blocked('scan_size_limit')
        if size > MAX_FILE:
            findings.append({'path': rel, 'line': 0, 'kind': 'oversize-file'})
        elif key not in cache:
            cache[key] = scan_bytes(rel, git(root, 'cat-file', 'blob', oid, binary=True), mode, approvals)
        findings.extend(cache.get(key, []))
    return findings


def scan_commits(root, local, remote, approvals):
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', local + ''):
        raise Blocked('invalid_commit')
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', remote):
        raise Blocked('invalid_remote_commit')
    zero = not remote.strip('0')
    if not zero:
        if subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', remote, local], capture_output=True).returncode:
            raise Blocked('non_fast_forward_or_missing_remote_object')
    commits = git(root, 'rev-list', local if zero else remote + '..' + local).splitlines()
    if len(commits) > 200:
        raise Blocked('history_review_limit')
    findings, cache = [], {}
    for commit in commits:
        for value in git(root, 'show', '-s', '--format=%ae%n%ce', commit).splitlines():
            if not re.fullmatch(r'(?:[0-9]+\+)?[A-Za-z0-9_-]+@users\.noreply\.github\.com', value):
                findings.append({'commit': commit, 'kind': 'unapproved-author-metadata'})
        # Commit messages are part of the pushed data too.
        message = git(root, 'show', '-s', '--format=%B', commit).encode()
        findings.extend({**f, 'commit': commit} for f in scan_bytes('(commit-message)', message))
        findings.extend({**f, 'commit': commit} for f in scan_tree(root, commit, approvals, cache))
    return findings


def inspect_paths(root, paths, approvals):
    entries, findings = [], []
    for rel in sorted(set(paths)):
        safe_rel(rel)
        p = Path(root) / rel
        # Do not dereference links into private/outside directories.
        if p.is_symlink() or any(x.is_symlink() for x in p.parents if x != Path(root) and Path(root) in x.parents):
            raise Blocked('unreviewed_symlink', [{'path': rel}])
        if not p.exists():
            if not git(root, 'ls-files', '--', rel):
                raise Blocked('missing_untracked_file', [{'path': rel}])
            entries.append({'path': rel, 'deleted': True})
            continue
        if not p.is_file() or p.stat().st_size > MAX_FILE:
            raise Blocked('unreviewed_file_or_size', [{'path': rel}])
        raw = p.read_bytes()
        findings.extend(scan_bytes(rel, raw, approvals=approvals))
        entries.append({'path': rel, 'sha256': digest(raw), 'bytes': len(raw)})
    if findings:
        raise Blocked('scan_findings', findings)
    return entries


def hook_content(name, registry):
    script = str(Path(__file__).resolve())
    return '#!/bin/sh\n# PKOS managed pre-push v1\nexec python3 ' + shlex.quote(script) + ' --registry ' + shlex.quote(str(Path(registry).resolve())) + ' pre-push --repo ' + shlex.quote(name) + ' --remote-name "$1" --remote-url "$2"\n'


def hook_path(root):
    configured = subprocess.run(['git', '-C', str(root), 'config', '--get', 'core.hooksPath'], capture_output=True).returncode == 0
    if configured:
        raise Blocked('existing_custom_hooks_path')
    p = Path(git(root, 'rev-parse', '--git-path', 'hooks/pre-push'))
    return p if p.is_absolute() else Path(root) / p


def install_hooks(data, registry):
    plans, conflicts = [], []
    for name, repo in data['repositories'].items():
        try:
            p = hook_path(repo['root'])
            expected = hook_content(name, registry)
            if p.exists() or p.is_symlink():
                if not p.is_symlink() and p.read_text() == expected:
                    continue
                raise Blocked('existing_hook_not_overwritten')
            plans.append((p, expected))
        except Blocked as e:
            conflicts.append({'repository': name, 'reason': e.reason})
    if conflicts:
        raise Blocked('hook_conflicts_no_hooks_installed', conflicts)
    for p, text in plans:
        with p.open('x') as f:
            f.write(text)
        p.chmod(0o755)
    return {'installed': len(plans), 'existing_hooks_overwritten': False}


def pre_push(data, name, remote_name, remote_url, lines):
    repo = repo_for(data, name)
    root = repo['root']
    validate_identity(root, repo)
    if remote_name != 'origin' or remote_url != repo['ssh_url']:
        raise Blocked('unexpected_push_target')
    inventory_issues = skill_inventory(data)
    if inventory_issues:
        raise Blocked('skill_inventory_requires_review', inventory_issues)
    records = [line.split() for line in lines.splitlines() if line.strip()]
    if not records:
        return {'status': 'nothing_to_push'}
    if len(records) != 1 or len(records[0]) != 4:
        raise Blocked('only_one_main_ref_allowed')
    local_ref, local, remote_ref, remote = records[0]
    if local_ref != 'refs/heads/main' or remote_ref != 'refs/heads/main' or not local.strip('0'):
        raise Blocked('branch_tag_or_deletion_not_allowed')
    findings = scan_commits(root, local, remote, repo.get('approvals', []))
    if findings:
        raise Blocked('scan_findings', findings)
    actual = remote_metadata(data, name)
    if actual != remote:
        raise Blocked('remote_changed_during_push')
    return {'status': 'push_scan_passed', 'repository': name, 'commit': local}


def skill_inventory(data):
    registered = {str(Path(p).resolve()) for t in data['tools'] for p in t.get('skill_entries', [])}
    external = {str(Path(p).resolve()) for p in data.get('external_skill_entries', [])}
    found, issues = set(), []
    for directory in data['skill_roots']:
        base = Path(directory)
        if not base.exists():
            continue
        for child in base.iterdir():
            if child.name.startswith('.'):
                continue
            if child.is_symlink() and not child.exists():
                issues.append({'kind': 'broken_skill_link', 'path': str(child)})
            if (child / 'SKILL.md').is_file():
                found.add(str((child / 'SKILL.md').resolve()))
    # Git's candidate set respects private-data ignores, unlike a whole-disk walk.
    for repo in data['repositories'].values():
        for rel in git(repo['root'], 'ls-files', '--cached', '--others', '--exclude-standard', '-z', binary=True).split(b'\0'):
            if rel and os.fsdecode(rel).endswith('SKILL.md'):
                p = Path(repo['root']) / os.fsdecode(rel)
                if p.is_file():
                    found.add(str(p.resolve()))
    for p in sorted(found - registered - external):
        issues.append({'kind': 'unregistered_skill_ownership_unknown', 'path': p})
    return issues


def check(data, registry, remote=False):
    rows, issues = [], []
    ids = [t['id'] for t in data['tools']]
    if len(ids) != len(set(ids)):
        issues.append({'kind': 'duplicate_tool_id'})
    headers = re.findall(r'^### (TOOL-\d{3}) · ', Path(registry).read_text(), re.M)
    if set(headers) != set(ids):
        issues.append({'kind': 'ledger_human_index_ids_differ'})
    for tool in data['tools']:
        for p in tool.get('primary_paths', []):
            if not Path(p).exists():
                issues.append({'kind': 'missing_primary', 'tool': tool['id'], 'path': p})
        for link in tool.get('links', []):
            p = Path(link['path'])
            if not p.is_symlink() or p.resolve() != Path(link['target']).resolve() or not p.exists():
                issues.append({'kind': 'incorrect_source_link', 'tool': tool['id'], 'path': str(p)})
    for name, repo in data['repositories'].items():
        row = {'repository': name, 'root': repo['root'], 'remote_checked': remote}
        try:
            root = repo['root']
            validate_identity(root, repo)
            row['branch'] = git(root, 'branch', '--show-current')
            row['head'] = git(root, 'rev-parse', 'HEAD')
            row['changes'] = paths_from_status(root)
            row['recorded_sha_matches'] = row['head'] == repo.get('last_pushed_sha')
            upstream = subprocess.run(['git', '-C', root, 'rev-parse', '--verify', 'refs/remotes/origin/main'], capture_output=True, text=True)
            row['cached_remote_sha'] = upstream.stdout.strip() if upstream.returncode == 0 else None
            p = hook_path(root)
            row['hook_valid'] = p.is_file() and not p.is_symlink() and os.access(p, os.X_OK) and p.read_text() == hook_content(name, registry)
            for key, valid in [('branch', row['branch'] == 'main'), ('worktree', not row['changes']), ('ledger_sha', row['recorded_sha_matches']), ('pre_push_hook', row['hook_valid']), ('cached_remote', row['cached_remote_sha'] == row['head'])]:
                if not valid:
                    issues.append({'repository': name, 'kind': key})
            if remote:
                row['remote_sha'] = remote_metadata(data, name)
                row['private'] = True
                if row['remote_sha'] != row['head']:
                    issues.append({'repository': name, 'kind': 'remote_sha_mismatch'})
        except (Blocked, OSError) as e:
            issues.append({'repository': name, 'kind': getattr(e, 'reason', 'local_file_error')})
        rows.append(row)
    try:
        issues.extend(skill_inventory(data))
    except Blocked as e:
        issues.append({'kind': e.reason})
    for item in data.get('global_instruction_files', []):
        p = Path(item['path'])
        if not p.is_file() or digest(p.read_bytes()) != item['sha256']:
            issues.append({'kind': 'global_instruction_changed_or_missing', 'path': str(p)})
        if (p.parent / 'AGENTS.override.md').exists():
            issues.append({'kind': 'global_instruction_override_present', 'path': str(p.parent / 'AGENTS.override.md')})
    return {'checked_at': dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(),
            'read_only': True, 'scope': {'skill_roots': data['skill_roots'], 'repositories': list(data['repositories']), 'whole_disk_coverage': False},
            'repositories': rows, 'tool_count': len(data['tools']), 'issues': issues, 'ok': not issues,
            'remote_note': 'GitHub API read only; no fetch' if remote else 'Cached refs only; live remote NOT verified'}


@contextlib.contextmanager
def ledger_lock(registry):
    import fcntl
    p = Path(str(registry) + '.lock')
    with p.open('a') as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked('another_sync_is_running')
        yield


def sync(data, registry, name, paths, message):
    repo = repo_for(data, name)
    root = repo['root']
    validate_identity(root, repo)
    if git(root, 'branch', '--show-current') != 'main':
        raise Blocked('not_on_main')
    if git(root, 'diff', '--cached', '--name-only'):
        raise Blocked('existing_staged_work')
    hook = hook_path(root)
    if not hook.is_file() or hook.is_symlink() or not os.access(hook, os.X_OK) or hook.read_text() != hook_content(name, registry):
        raise Blocked('required_hook_missing_or_changed')
    inventory_issues = skill_inventory(data)
    if inventory_issues:
        raise Blocked('skill_inventory_requires_review', inventory_issues)
    changed = {x['path'] for x in paths_from_status(root)}
    if not paths or len(set(paths)) != len(paths) or not set(paths) <= changed:
        raise Blocked('manifest_not_exact_changed_paths')
    approvals = repo.get('approvals', [])
    before = inspect_paths(root, paths, approvals)
    # Never stage/commit before scanning both current source and outgoing history.
    remote = remote_metadata(data, name)
    head = git(root, 'rev-parse', 'HEAD')
    findings = scan_commits(root, head, remote, approvals)
    if findings:
        raise Blocked('scan_findings', findings)
    if scan_bytes('(commit-message)', message.encode()):
        raise Blocked('commit_message_findings')
    if inspect_paths(root, paths, approvals) != before:
        raise Blocked('files_changed_since_scan')
    git(root, 'add', '--', *paths)
    if set(git(root, 'diff', '--cached', '--no-renames', '--name-only', '-z', binary=True).decode().split('\0')) - {''} != set(paths):
        raise Blocked('staged_manifest_mismatch_index_preserved')
    for entry in before:
        if not entry.get('deleted') and digest(git(root, 'show', ':' + entry['path'], binary=True)) != entry['sha256']:
            raise Blocked('staged_hash_mismatch_index_preserved')
    # Email metadata must be the verified account's noreply identity.
    for key in ['GIT_AUTHOR_IDENT', 'GIT_COMMITTER_IDENT']:
        identity = git(root, 'var', key)
        if not re.search(r'<(?:[0-9]+\+)?' + re.escape(data['account']) + r'@users\.noreply\.github\.com>', identity):
            raise Blocked('author_identity_requires_configuration_index_preserved')
    git(root, 'commit', '-m', message)
    sha = git(root, 'rev-parse', 'HEAD')
    # The installed hook independently rescans actual objects Git sends.
    git(root, 'push', 'origin', 'refs/heads/main:refs/heads/main')
    if remote_metadata(data, name) != sha:
        raise Blocked('post_push_remote_mismatch')
    repo['last_pushed_sha'] = sha
    repo['verification'] = {'status': 'remote_clone_pending', 'sha': sha}
    save_ledger(registry, data)
    return {'status': 'pushed', 'repository': name, 'sha': sha, 'manifest': before, 'verification': 'remote_clone_pending; run verify next'}


def verify(data, registry, name):
    repo = repo_for(data, name)
    sha = remote_metadata(data, name)
    if sha != repo['last_pushed_sha']:
        raise Blocked('verify_remote_differs_from_ledger')
    commands = repo.get('verify_commands')
    if not commands:
        raise Blocked('verification_commands_not_registered')
    steps, error, directory = [], None, None
    try:
        with tempfile.TemporaryDirectory(prefix='pkos-tool-sync-verify-') as directory:
            target = Path(directory) / 'repo'
            run(['git', 'clone', '--quiet', '--branch', 'main', '--single-branch', repo['ssh_url'], str(target)])
            if git(target, 'rev-parse', 'HEAD') != sha:
                raise Blocked('clone_sha_mismatch')
            # No credentials/real config from the working source are inherited/copied.
            env = {k: v for k, v in os.environ.items() if k in {'PATH', 'HOME', 'TMPDIR', 'LANG', 'SSH_AUTH_SOCK'}}
            env.update(NEXT_TELEMETRY_DISABLED='1', WRANGLER_SEND_METRICS='false', DO_NOT_TRACK='1', PYTHONDONTWRITEBYTECODE='1')
            for item in commands:
                cwd = target / item.get('cwd', '.')
                if not cwd.resolve().is_relative_to(target.resolve()):
                    raise Blocked('unsafe_verification_directory')
                try:
                    p = subprocess.run(item['argv'], cwd=cwd, env=env, capture_output=True, timeout=600)
                    code = p.returncode
                except subprocess.TimeoutExpired:
                    code = 124
                # Log only command/exit status; raw tool output may echo private data.
                steps.append({'argv': item['argv'], 'cwd': item.get('cwd', '.'), 'exit_code': code})
                if code:
                    break
    except (Blocked, OSError) as e:
        error = getattr(e, 'reason', 'verification_command_unavailable')
    result = {'status': 'passed' if not error and steps and all(x['exit_code'] == 0 for x in steps) else 'failed', 'sha': sha,
              'steps': steps, 'error': error, 'temporary_deleted': directory is None or not Path(directory).exists(), 'scope': 'registered offline commands only; not full business validation'}
    repo['verification'] = result
    save_ledger(registry, data)
    return result


def default_registry():
    if os.environ.get('PKOS_TOOL_REGISTRY'):
        return os.environ['PKOS_TOOL_REGISTRY']
    config = Path(__file__).resolve().parents[1] / 'config/tool-sync.local.json'
    if config.exists():
        return json.loads(config.read_text())['registry']
    return str(Path.home() / 'Documents/PKOS-System-Data/tools/TOOL_REGISTRY.md')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', default=default_registry())
    sub = parser.add_subparsers(dest='command', required=True)
    c = sub.add_parser('check'); c.add_argument('--remote', action='store_true')
    sub.add_parser('install-hooks')
    for name in ['scan', 'sync', 'verify', 'pre-push']:
        c = sub.add_parser(name); c.add_argument('--repo', required=True)
        if name in {'scan', 'sync'}:
            c.add_argument('--paths', nargs='+', required=True)
        if name == 'sync': c.add_argument('--message', required=True)
        if name == 'pre-push':
            c.add_argument('--remote-name', required=True); c.add_argument('--remote-url', required=True)
    c = sub.add_parser('share')
    modes = c.add_subparsers(dest='share_command', required=True)
    q = modes.add_parser('prepare'); q.add_argument('--tool', required=True); q.add_argument('--output', required=True); q.add_argument('--distribution')
    q = modes.add_parser('inspect'); q.add_argument('--candidate', required=True)
    q = modes.add_parser('publish')
    for flag in ['candidate', 'approval-file', 'approved-digest']: q.add_argument('--' + flag, required=True)
    c = sub.add_parser('share-pre-push')
    for flag in ['candidate', 'approval-file', 'approved-digest', 'root', 'remote-name', 'remote-url']: c.add_argument('--' + flag, required=True)
    args = parser.parse_args(argv)
    try:
        data = load_ledger(args.registry)
        if args.command in {'share', 'share-pre-push'}:
            spec = importlib.util.spec_from_file_location('pkos_public_release', Path(__file__).with_name('public_release.py'))
            release = importlib.util.module_from_spec(spec); spec.loader.exec_module(release)
            api = types.SimpleNamespace(**globals())
            if args.command == 'share-pre-push':
                result = release.gate(api, data, args.candidate, args.approval_file, args.approved_digest, args.root, args.remote_name, args.remote_url, sys.stdin.read())
            elif args.share_command == 'inspect':
                result = {'status': 'public_candidate_verified', **release.inspect(api, data, args.candidate)}
            else:
                with ledger_lock(args.registry):
                    data = load_ledger(args.registry)
                    result = release.prepare(api, data, args.registry, args.tool, args.output, args.distribution) if args.share_command == 'prepare' else release.publish(api, data, args.registry, args.candidate, args.approval_file, args.approved_digest)
        elif args.command == 'check':
            result = check(data, args.registry, args.remote)
        elif args.command == 'install-hooks':
            result = install_hooks(data, args.registry)
        elif args.command == 'pre-push':
            result = pre_push(data, args.repo, args.remote_name, args.remote_url, sys.stdin.read())
        elif args.command == 'scan':
            repo = repo_for(data, args.repo)
            result = {'status': 'scan_passed', 'manifest': inspect_paths(repo['root'], args.paths, repo.get('approvals', []))}
        else:
            with ledger_lock(args.registry):
                data = load_ledger(args.registry)
                result = sync(data, args.registry, args.repo, args.paths, args.message) if args.command == 'sync' else verify(data, args.registry, args.repo)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get('ok') is False or result.get('status') == 'failed' else 0
    except (Blocked, OSError, ValueError, subprocess.TimeoutExpired) as e:
        print(json.dumps({'status': 'blocked', 'reason': getattr(e, 'reason', 'local_configuration_or_command_error'), 'details': getattr(e, 'details', [])}, ensure_ascii=False, indent=2))
        return 3


if __name__ == '__main__':
    sys.exit(main())
