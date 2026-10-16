"""Explicit public exports. No private history, inherited exceptions or source writes.

Preparation is offline. Publication requires a frozen manifest and a separately
recorded human approval. Keep this workflow separate from private synchronization.
"""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipfile


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def guard(api, condition, reason):
    if not condition:
        raise api.Blocked(reason)


def regular(api, root, rel):
    p = PurePosixPath(rel)
    guard(api, bool(rel) and not p.is_absolute() and '..' not in p.parts and '\\' not in rel and str(p) == rel, 'public_unsafe_path')
    guard(api, not any(x.lower() in {'.git', 'private', 'node_modules', '.venv', 'input', 'output', 'data'} for x in p.parts), 'public_private_or_runtime_path')
    f = root / rel
    guard(api, not root.is_symlink() and not f.is_symlink() and all(not (root / Path(*p.parts[:i])).is_symlink() for i in range(1, len(p.parts))), 'public_symlink')
    guard(api, f.is_file() and f.stat().st_size <= api.MAX_FILE, 'public_missing_or_oversize_file')
    return f.read_bytes()


def config(api, data, tool_id, distribution=None):
    tools = [t for t in data['tools'] if t['id'] == tool_id]
    guard(api, len(tools) == 1, 'public_distribution_unregistered')
    c = (tools[0].get('public_distributions', {}).get(distribution) if distribution
         else tools[0].get('public_distribution'))
    guard(api, isinstance(c, dict), 'public_distribution_unregistered')
    guard(api, re.fullmatch(r'[a-z0-9][a-z0-9-]{0,99}', c['repository']) and re.fullmatch(r'\d+\.\d+\.\d+', c['version']), 'public_invalid_name_or_version')
    source = Path(c['source_directory'])
    guard(api, not source.is_symlink(), 'public_source_must_be_canonical')
    source = source.resolve()
    repo = data['repositories'][tools[0]['repository']]
    guard(api, source.is_relative_to(Path(repo['root']).resolve()) and str(source) in tools[0]['primary_paths'], 'public_source_not_registered_primary')
    guard(api, c['license'] == 'MIT' and len(c['files']) == len(set(c['files'])) and {'SKILL.md', 'README.md', 'LICENSE'} <= set(c['files']), 'public_invalid_file_config')
    return c, source, repo


def public_scan(api, data, files):
    findings = []
    private_urls = [r['ssh_url'] for r in data['repositories'].values()]
    private_urls += ['https://github.com/' + data['account'] + '/' + n for n in data['repositories']]
    for rel, raw in files.items():
        findings.extend(api.scan_bytes(rel, raw))  # Deliberately NO private approvals.
        text = raw.decode('utf-8', errors='strict')
        if re.search(r'(?:/' + r'Users/[^/\s]+/|/' + r'home/[^/\s]+/|[A-Za-z]:\\Users\\[^\\\s]+\\)', text):
            findings.append({'path': rel, 'kind': 'personal_absolute_path'})
        if any(url in text for url in private_urls):
            findings.append({'path': rel, 'kind': 'private_repository_url'})
    if findings:
        raise api.Blocked('public_scan_findings', findings)


def zip_bytes(files, prefix='', skill_override=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for rel, raw in sorted(files.items()):
            if rel == 'SKILL.md' and skill_override is not None:
                raw = skill_override
            item = zipfile.ZipInfo(prefix + rel, date_time=(2026, 1, 1, 0, 0, 0))
            item.create_system = 3
            item.external_attr = 0o100644 << 16
            item.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(item, raw)
    return stream.getvalue()


def workbuddy_skill(api, c, files):
    meta = json.loads(files[c['workbuddy_metadata']])
    guard(api, set(meta) == {'description_zh', 'description_en', 'version', 'author'} and meta['version'] == c['version'] and all(isinstance(v, str) and v.strip() for v in meta.values()), 'public_workbuddy_metadata_invalid')
    text = files['SKILL.md'].decode()
    guard(api, text.startswith('---\n') and text.count('\n---\n') >= 1, 'public_skill_frontmatter_missing')
    header, body = text[4:].split('\n---\n', 1)
    guard(api, all(not re.search(r'^' + re.escape(k) + ':', header, re.M) for k in meta), 'public_duplicate_frontmatter')
    return ('---\n' + header + '\n' + '\n'.join(k + ': ' + json.dumps(v, ensure_ascii=False) for k, v in meta.items()) + '\n---\n' + body).encode()


def payload(api, data, tool_id, distribution=None):
    c, source, repo = config(api, data, tool_id, distribution)
    mapping = c.get('file_map', {})
    guard(api, isinstance(mapping, dict) and set(mapping) <= set(c['files']), 'public_invalid_file_map')
    # Destination and source both receive the normal path checks. No transforms or hidden copies.
    for rel in c['files']:
        p = PurePosixPath(rel)
        guard(api, bool(rel) and not p.is_absolute() and '..' not in p.parts and str(p) == rel and '\\' not in rel, 'public_unsafe_path')
        guard(api, not any(x.lower() in {'.git', 'private', 'node_modules', '.venv', 'input', 'output', 'data'} for x in p.parts), 'public_private_or_runtime_path')
    files = {rel: regular(api, source, mapping.get(rel, rel)) for rel in c['files']}
    public_scan(api, data, files)
    guard(api, files['LICENSE'].startswith(b'MIT License\n'), 'public_license_mismatch')
    wb = workbuddy_skill(api, c, files)
    public_scan(api, data, {'SKILL.md': wb})
    assets = {c['repository'] + '-' + c['version'] + '.zip': zip_bytes(files, c['repository'] + '/'),
              c['repository'] + '-' + c['version'] + '-workbuddy.zip': zip_bytes(files, skill_override=wb)}
    entries = lambda values: [{'path': k, 'size': len(v), 'sha256': api.digest(v)} for k, v in sorted(values.items())]
    descriptor = {'schema': 1, 'tool': tool_id, 'target': data['account'] + '/' + c['repository'], 'visibility': 'public',
                  'version': c['version'], 'license': c['license'], 'files': entries(files), 'assets': entries(assets)}
    if distribution:
        descriptor['distribution'] = distribution
    return files, assets, descriptor, source, repo


def prepare(api, data, registry, tool_id, output, distribution=None):
    files, assets, desc, source, repo = payload(api, data, tool_id, distribution)
    out = Path(output).absolute()
    guard(api, not out.exists() and not out.is_symlink(), 'public_output_exists')
    guard(api, out.parent.exists() and all(not p.is_symlink() for p in [out.parent, *out.parent.parents]), 'public_output_parent_unsafe')
    out = out.resolve()
    guard(api, all(not out.is_relative_to(Path(r['root']).resolve()) for r in data['repositories'].values()), 'public_output_inside_managed_source')
    out.mkdir()
    for folder, values in [('repository', files), ('assets', assets)]:
        for rel, raw in values.items():
            target = out / folder / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
    review = {'descriptor': desc, 'approval_digest': api.digest(canonical(desc)),
              'source_head_at_prepare': api.git(repo['root'], 'rev-parse', 'HEAD'),
              'source_has_uncommitted_changes': bool(api.paths_from_status(repo['root'])),
              'state': 'awaiting_human_file_approval', 'source_directory': str(source)}
    (out / 'REVIEW.json').write_text(json.dumps(review, ensure_ascii=False, indent=2) + '\n')
    c, _, _ = config(api, data, tool_id, distribution)
    c.update(status='awaiting_human_file_approval', candidate=str(out), approval_digest=review['approval_digest'])
    api.save_ledger(registry, data)
    return {'status': review['state'], 'candidate': str(out), 'approval_digest': review['approval_digest'], 'manifest': desc, 'network_used': False}


def inspect(api, data, candidate):
    out = Path(candidate)
    guard(api, out.is_dir() and not out.is_symlink() and all(not p.is_symlink() for p in out.parents), 'public_candidate_unsafe')
    review = json.loads((out / 'REVIEW.json').read_text())
    desc = review['descriptor']
    # Regenerate from current source: any approved-source change invalidates the candidate.
    files, assets, expected, _, _ = payload(api, data, desc['tool'], desc.get('distribution'))
    guard(api, desc == expected and review['approval_digest'] == api.digest(canonical(desc)), 'public_candidate_or_source_changed')
    actual = set()
    for p in out.rglob('*'):
        guard(api, not p.is_symlink(), 'public_candidate_symlink')
        if p.is_file():
            actual.add(p.relative_to(out).as_posix())
    names = {'REVIEW.json'} | {'repository/' + p for p in files} | {'assets/' + p for p in assets}
    guard(api, actual == names, 'public_candidate_extra_or_missing_file')
    for folder, values in [('repository', files), ('assets', assets)]:
        for rel, raw in values.items():
            guard(api, (out / folder / rel).read_bytes() == raw, 'public_candidate_content_changed')
    return review


def approved(api, data, candidate, approval_file, approved_digest):
    review = inspect(api, data, candidate)
    approval = json.loads(Path(approval_file).read_text())
    guard(api, approved_digest == review['approval_digest'] and approval.get('approval_digest') == approved_digest and
          approval.get('target') == review['descriptor']['target'] and approval.get('decision') == 'approved' and
          isinstance(approval.get('user_confirmation_reference'), str) and bool(approval['user_confirmation_reference'].strip()), 'public_human_approval_required')
    return review


def metadata(api, data, target):
    guard(api, api.run(['gh', 'api', 'user', '--jq', '.login']) == data['account'], 'public_account_mismatch')
    # Only a confirmed 404 is absence; network/auth errors must stop.
    p = subprocess.run(['gh', 'api', 'repos/' + target], capture_output=True, timeout=60)
    if p.returncode:
        guard(api, b'HTTP 404' in p.stderr, 'public_remote_query_failed')
        return None
    m = json.loads(p.stdout)
    guard(api, m.get('full_name') == target and m.get('private') is False and not m.get('archived') and not m.get('fork'), 'public_remote_visibility_or_identity_mismatch')
    return m


def remote_sha(api, target, meta):
    if meta.get('size') == 0:
        query = subprocess.run(['gh', 'api', 'repos/' + target + '/git/matching-refs/heads/main'], capture_output=True, timeout=115)
        if query.returncode == 0:
            refs = json.loads(query.stdout)
            if not refs:
                return '0' * 40
        else:
            error = json.loads(query.stdout) if query.stdout else {}
            # GitHub returns 409 for a new, genuinely empty repository.
            # Independently require SSH to report no refs; other failures stop.
            guard(api, b'HTTP 409' in query.stderr and error.get('message') == 'Git Repository is empty.', 'public_remote_refs_query_failed')
            guard(api, not api.run(['git', 'ls-remote', meta['ssh_url']]), 'public_empty_repository_refs_mismatch')
            return '0' * 40
    guard(api, meta.get('default_branch') == 'main', 'public_remote_default_branch_mismatch')
    return json.loads(api.run(['gh', 'api', 'repos/' + target + '/git/ref/heads/main']))['object']['sha']


def gate(api, data, candidate, approval_file, approved_digest, root, remote_name, remote_url, lines):
    review = approved(api, data, candidate, approval_file, approved_digest)
    desc = review['descriptor']; c, _, _ = config(api, data, desc['tool'], desc.get('distribution'))
    url = 'git' + '@' + 'github.com:' + desc['target'] + '.git'
    guard(api, remote_name == 'origin' and remote_url == url and api.git(root, 'remote', 'get-url', '--all', 'origin') == url and api.git(root, 'remote', 'get-url', '--push', '--all', 'origin') == url, 'public_remote_changed')
    rows = [x.split() for x in lines.splitlines() if x.strip()]
    guard(api, len(rows) == 1 and len(rows[0]) == 4 and rows[0][0] == rows[0][2] == 'refs/heads/main', 'public_only_main_allowed')
    _, local, _, remote = rows[0]
    meta = metadata(api, data, desc['target'])
    guard(api, meta is not None and remote_sha(api, desc['target'], meta) == remote and remote == c.get('last_public_sha', '0' * 40), 'public_remote_history_changed')
    findings = api.scan_commits(root, local, remote, [])
    if findings:
        raise api.Blocked('public_history_scan_findings', findings)
    files, _, _, _, _ = payload(api, data, desc['tool'], desc.get('distribution'))
    guard(api, set(api.git(root, 'ls-tree', '-r', '--name-only', local).splitlines()) == set(files), 'public_commit_file_set_changed')
    for rel, raw in files.items():
        guard(api, api.git(root, 'show', local + ':' + rel, binary=True) == raw, 'public_commit_content_changed')
    return {'status': 'public_pre_push_passed'}


def publish(api, data, registry, candidate, approval_file, approved_digest):
    review = approved(api, data, candidate, approval_file, approved_digest)
    desc = review['descriptor']; c, source, source_repo = config(api, data, desc['tool'], desc.get('distribution'))
    guard(api, not api.git(source_repo['root'], 'diff', '--cached', '--name-only'), 'public_source_staged_conflict')
    target = desc['target']; url = 'git' + '@' + 'github.com:' + target + '.git'; tag = 'v' + desc['version']
    meta = metadata(api, data, target)
    remote = remote_sha(api, target, meta) if meta else '0' * 40
    guard(api, remote == c.get('last_public_sha', '0' * 40) and (meta is not None or not c.get('last_public_sha')), 'public_remote_history_changed')
    # Reject existing releases instead of overwriting an asset or mutable tag.
    if meta:
        p = subprocess.run(['gh', 'api', 'repos/' + target + '/releases/tags/' + tag], capture_output=True, timeout=60)
        guard(api, p.returncode != 0 and b'HTTP 404' in p.stderr, 'public_release_exists_or_query_failed')
    account = json.loads(api.run(['gh', 'api', 'user']))
    guard(api, account['login'] == data['account'] and isinstance(account['id'], int), 'public_identity_unavailable')
    with tempfile.TemporaryDirectory(prefix='pkos-public-release-') as tmp:
        root = Path(tmp) / 'repository'
        if remote.strip('0'):
            api.run(['git', 'clone', '--quiet', '--branch', 'main', '--single-branch', url, str(root)])
            guard(api, api.git(root, 'rev-parse', 'HEAD') == remote, 'public_clone_remote_changed')
            # Previously published files only; extra external edits require review/import first.
            prior_files = c.get('last_public_files', [])
            guard(api, set(api.git(root, 'ls-files').splitlines()) == set(prior_files), 'public_external_file_set_changed')
            for rel in prior_files:
                (root / rel).unlink()
        else:
            root.mkdir(); api.git(root, 'init', '-b', 'main'); api.git(root, 'remote', 'add', 'origin', url)
        guard(api, subprocess.run(['git', '-C', str(root), 'config', '--get', 'core.hooksPath'], capture_output=True).returncode != 0, 'public_existing_hooks_configuration')
        hook = root / '.git/hooks/pre-push'
        guard(api, not hook.exists() and not hook.is_symlink(), 'public_existing_hook')
        for item in desc['files']:
            rel = item['path']; p = root / rel; p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes((Path(candidate) / 'repository' / rel).read_bytes())
        api.git(root, 'config', 'user.name', data['account'])
        api.git(root, 'config', 'user.email', str(account['id']) + '+' + data['account'] + '@users.noreply.github.com')
        # Stage only approved current files and previously recorded deletion paths.
        api.git(root, 'add', '--', *sorted({x['path'] for x in desc['files']} | set(c.get('last_public_files', []))))
        if api.git(root, 'diff', '--cached', '--name-only'):
            api.git(root, 'commit', '-m', 'Release ' + c['repository'] + ' ' + c['version'])
        sha = api.git(root, 'rev-parse', 'HEAD')
        findings = api.scan_commits(root, sha, remote, [])
        if findings:
            raise api.Blocked('public_history_scan_findings', findings)
        command = [sys.executable, str(Path(api.__file__).resolve()), '--registry', str(Path(registry).resolve()), 'share-pre-push',
                   '--candidate', str(Path(candidate).resolve()), '--approval-file', str(Path(approval_file).resolve()), '--approved-digest', approved_digest, '--root', str(root)]
        hook.write_text('#!/bin/sh\nexec ' + ' '.join(shlex.quote(x) for x in command) + ' --remote-name "$1" --remote-url "$2"\n'); hook.chmod(0o700)
        if meta is None:
            api.run(['gh', 'repo', 'create', target, '--public'])
        # New repo/visibility is rechecked inside the separate public hook.
        api.git(root, 'push', 'origin', 'refs/heads/main:refs/heads/main')
        meta = metadata(api, data, target)
        guard(api, meta is not None and remote_sha(api, target, meta) == sha, 'public_post_push_verification_failed')
        c.update(last_public_sha=sha, last_public_files=[x['path'] for x in desc['files']], status='public_pushed_release_pending', public_repo_url='https://github.com/' + target)
        api.save_ledger(registry, data)
        assets = [str(Path(candidate) / 'assets' / x['path']) for x in desc['assets']]
        api.run(['gh', 'release', 'create', tag, '--repo', target, '--target', sha, '--title', c['repository'] + ' ' + c['version'], '--notes', 'Approved portable Skill release; see README for setup and tested scope.', *assets])
        release = json.loads(api.run(['gh', 'api', 'repos/' + target + '/releases/tags/' + tag]))
        expected = {(x['path'], x['size'], 'sha256:' + x['sha256']) for x in desc['assets']}
        actual = {(x['name'], x['size'], x.get('digest')) for x in release['assets']}
        guard(api, actual == expected and api.run(['git', 'ls-remote', url, 'refs/tags/' + tag]).split()[0] == sha, 'public_release_verification_failed')
        c.update(status='public_release_verified', public_release_url=release['html_url'], last_approved_digest=approved_digest,
                 source_manifest=desc['files'], source_head_at_publication=api.git(source_repo['root'], 'rev-parse', 'HEAD'))
        api.save_ledger(registry, data)
    # Backfill is local and only follows verified publication; failure never conceals the release.
    backfill = {'status': 'not_configured'}
    local = Path(api.__file__).resolve().parents[1] / 'config/publication.local.json'
    if local.is_file():
        try:
            package_root = str(Path(api.__file__).resolve().parents[1])
            if package_root not in sys.path:
                sys.path.insert(0, package_root)
            from maintenance.skill_publication import backfill_release
            backfill = backfill_release(local, registry, desc['tool'], desc.get('distribution'))
        except Exception as exc:
            backfill = {'status': 'pending', 'error_type': type(exc).__name__}
            c['card_backfill_status'] = 'pending'
            api.save_ledger(registry, data)
    return {'card_backfill': backfill, 'status': c['status'], 'sha': sha, 'public_repo_url': c['public_repo_url'], 'release_url': c['public_release_url'], 'temporary_deleted': True,
            'next': 'Review card_backfill status; retry local backfill if pending. Never publish the private source repo.'}
