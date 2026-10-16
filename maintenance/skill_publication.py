"""Offline, repeatable release preparation for self-built tool cards.

Card writes enqueue durable events. Reconciliation also detects direct edits and
source changes. This module never creates approval, pushes, or calls a network API.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import yaml
from maintenance import tool_sync as sync, public_release as release
from validation.module_config import read_config
from validation.validate import load_note, normalized, validate
from weekly.workflow import lock, outside_repo, save_json, private_write


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def configuration(path):
    c = json.loads(Path(path).read_text())
    if c.get('schema_version') != 1:
        raise ValueError('invalid publication configuration')
    for key in ('vault', 'system_data', 'module_config', 'card_index', 'source_index', 'registry'):
        c[key] = Path(c[key]).expanduser().resolve()
        if not c[key].exists():
            raise ValueError('missing configured publication path: ' + key)
    outside_repo(c['system_data'])
    if c['system_data'].is_relative_to(c['vault']):
        raise ValueError('publication outputs must stay outside Vault')
    for key in ('card_index', 'source_index'):
        if not c[key].is_relative_to(c['system_data']):
            raise ValueError('publication index must stay in system data')
    return c


def cards(c):
    _, modules = read_config(c['module_config'])
    root = c['vault'] / modules['skills']
    for path in sorted(root.rglob('*.md')):
        if not path.resolve().is_relative_to(c['vault']):
            raise ValueError('card symlink escapes Vault')
        meta, body = load_note(path)
        if meta.get('type') == 'tool':
            yield path, meta, body


def binding(meta, data):
    ids = ([meta['tool_registry_id']] if meta.get('tool_registry_id')
           else meta.get('tool_registry_ids', []))
    tools = [t for t in data['tools'] if t['id'] in ids]
    if not tools and meta.get('skill_name'):
        # Only the one registered primary entry owns a shared Skill.
        tools = [t for t in data['tools'] if t.get('skill_name') == meta['skill_name'] and t.get('skill_entries')]
    if len(tools) > 1:
        tools = [t for t in tools if t.get('skill_entries')]
    if len(tools) != 1:
        raise sync.Blocked('card_tool_ownership_missing_or_ambiguous')
    return tools[0]['id'], meta.get('public_distribution_key') or None


def is_internal(meta, path=None, c=None):
    if meta.get('subtype') == 'external':
        return False
    known = meta.get('subtype') == 'internal' or bool(meta.get('tool_registry_id') or meta.get('tool_registry_ids'))
    if path is not None and c is not None:
        for rel in c.get('internal_card_folders', []):
            folder = (c['vault'] / rel).resolve()
            if not folder.is_relative_to(c['vault']):
                raise ValueError('internal card folder escapes Vault')
            known = known or path.resolve().is_relative_to(folder)
    return known


def ensure_takeaway(body):
    if re.search(r'^## 拿去用\s*$', body, re.M):
        return body
    block = '## 拿去用\n\n公开版尚未发布，发行准备结果将进入待确认清单。\n\n'
    marker = re.search(r'^## 怎么用\s*$', body, re.M)
    return body[:marker.start()] + block + body[marker.start():] if marker else body.rstrip() + '\n\n' + block


def enqueue(writes, system_data, event_id, card_ids):
    """Add an outbox event to the same transaction as the cards and indexes."""
    if card_ids:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', event_id):
            raise ValueError('unsafe publication event ID')
        folder = outside_repo(system_data) / 'publications/events'
        path = folder / (event_id + '.json')
        if path.exists():
            raise ValueError('publication event already exists')
        writes[path] = json.dumps({'schema': 1, 'card_ids': sorted(set(card_ids)), 'state': 'pending'}, indent=2) + '\n'


def review_markdown(review):
    d = review['descriptor']
    lines = ['# 待确认公开发行', '', '目标：`' + d['target'] + '`（public）',
             '版本：`' + d['version'] + '`；许可：`' + d['license'] + '`',
             '批准摘要：`' + review['approval_digest'] + '`', '',
             '## 源码文件', '', '| 文件 | 字节 | SHA256 |', '|---|---:|---|']
    lines += ['| ' + x['path'] + ' | ' + str(x['size']) + ' | `' + x['sha256'] + '` |' for x in d['files']]
    lines += ['', '## 发行产物', '', '| 文件 | 字节 | SHA256 |', '|---|---:|---|']
    lines += ['| ' + x['path'] + ' | ' + str(x['size']) + ' | `' + x['sha256'] + '` |' for x in d['assets']]
    lines += ['', '本清单不构成批准。确认后才创建／更新上述公开仓库与固定版本 Release。',
              'REVIEW.json、候选目录位置、私人总账和个人资料均不上传。', '']
    return '\n'.join(lines)


def reconcile(config_file, discover_skills=True):
    c = configuration(config_file)
    discovery = None
    if discover_skills and c.get('auto_intake_skills'):
        from maintenance.skill_lifecycle import reconcile as intake
        discovery = intake(config_file)
    home = c['system_data'] / 'publications'
    with lock(home), sync.ledger_lock(c['registry']):
        data = sync.load_ledger(c['registry'])
        state_path = home / 'STATE.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {'schema': 1, 'cards': {}}
        changed, blocked, prepared, active = [], [], {}, set()
        for path, meta, body in cards(c):
            if not is_internal(meta, path, c):
                continue
            ident = meta['id']; active.add(ident)
            prior = state['cards'].get(ident, {})
            row = {'card_sha256': sha(path), 'path': str(path.relative_to(c['vault']))}
            try:
                tool, variant = binding(meta, data)
                if not re.search(r'^## 拿去用\s*$', body, re.M):
                    raise sync.Blocked('card_takeaway_section_missing')
                key = tool + (':' + variant if variant else '')
                files, assets, descriptor, _, _ = release.payload(sync, data, tool, variant)
                fingerprint = sync.digest(release.canonical(descriptor))
                dist, _, _ = release.config(sync, data, tool, variant)
                row.update(tool=tool, distribution=variant, approval_digest=fingerprint)
                if dist.get('status') == 'public_release_verified' and dist.get('last_approved_digest') == fingerprint:
                    row.update(status='public_release_verified', public_release_url=dist['public_release_url'])
                else:
                    if dist.get('status') == 'public_release_verified' and ('v' + dist.get('version', '')) == dist.get('public_release_url', '').split('/')[-1]:
                        raise sync.Blocked('public_source_changed_requires_new_version')
                    candidate = dist.get('candidate')
                    if candidate and Path(candidate).is_dir():
                        try:
                            reviewed = release.inspect(sync, data, candidate)
                            if reviewed['approval_digest'] != fingerprint:
                                candidate = None
                        except (sync.Blocked, OSError, ValueError):
                            candidate = None
                    else:
                        candidate = None
                    if key in prepared:
                        candidate = prepared[key]
                    if not candidate:
                        dest = home / 'candidates' / (dist['repository'] + '-' + fingerprint[:16])
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        # Old/tampered candidates are preserved; never overwrite an approval snapshot.
                        if dest.exists():
                            dest = dest.with_name(dest.name + '-' + uuid.uuid4().hex[:8])
                        result = release.prepare(sync, data, c['registry'], tool, dest, variant)
                        candidate = result['candidate']; prepared[key] = candidate
                    reviewed = release.inspect(sync, data, candidate)
                    row.update(status='awaiting_human_file_approval', candidate=str(candidate))
                    report = home / 'reviews' / (fingerprint + '.md')
                    if not report.exists():
                        private_write(report, review_markdown(reviewed), True)
                    row['review_file'] = str(report)
            except (sync.Blocked, OSError, ValueError, KeyError, TypeError) as exc:
                row.update(status='blocked', reason=getattr(exc, 'reason', type(exc).__name__))
                # Locations only. No matched values are included in a scan finding.
                if isinstance(exc, sync.Blocked) and exc.details:
                    row['findings'] = exc.details
            if row != prior:
                changed.append(ident)
                if row['status'] == 'blocked':
                    blocked.append({'card': ident, 'reason': row['reason']})
            state['cards'][ident] = row
        for ident in set(state['cards']) - active:
            state['cards'].pop(ident)
        # Durable outbox entries are acknowledged only after a recorded disposition.
        for event in (home / 'events').glob('*.json'):
            item = json.loads(event.read_text())
            if item.get('state') == 'pending':
                item.update(state='reconciled', dispositions={i: state['cards'].get(i, {'status': 'external_or_missing'})['status'] for i in item['card_ids']})
                save_json(event, item)
        if changed or not state_path.exists():
            save_json(state_path, state)
        report = {'state': 'attention_required' if blocked else ('updated' if changed else 'unchanged'),
                  'changed_cards': changed, 'blocked': blocked,
                  'prepared_candidates': sorted(set(prepared.values())),
                  'pending_preparation': [{'card': ident, 'path': row['path'], 'reason': row['reason']}
                      for ident, row in state['cards'].items()
                      if row.get('reason') == 'public_distribution_unregistered'],
                  'network_used': False,
                  'publication_performed': False, 'approval_created': False, 'skill_intake': discovery}
        if discovery and discovery['state'] == 'attention_required':
            report['state'] = 'attention_required'
        elif discovery and discovery['state'] == 'updated' and report['state'] == 'unchanged':
            report['state'] = 'updated'
        return report


def backfill_release(config_file, registry, tool_id=None, distribution=None):
    """Back up, update only verified public links, validate, and roll back on failure."""
    c = configuration(config_file)
    if c['registry'] != Path(registry).resolve():
        raise ValueError('backfill registry mismatch')
    home = c['system_data'] / 'publications'
    with lock(home), lock(c['system_data'] / 'vault-writer'):
        data = sync.load_ledger(registry)
        index = json.loads(c['card_index'].read_text())
        writes = {}
        for path, meta, body in cards(c):
            if not is_internal(meta, path, c):
                continue
            try:
                tool, variant = binding(meta, data)
            except sync.Blocked:
                continue
            if tool_id and (tool != tool_id or variant != distribution):
                continue
            dist, _, _ = release.config(sync, data, tool, variant)
            if dist.get('status') != 'public_release_verified':
                continue
            # Current source must still match the approved release, not just a status flag.
            desc = release.payload(sync, data, tool, variant)[2]
            if sync.digest(release.canonical(desc)) != dist.get('last_approved_digest'):
                continue
            block = ('## 拿去用\n\n- 公开源码：[' + dist['repository'] + '](' + dist['public_repo_url'] + ')。\n'
                     '- 固定版本下载：[' + dist['version'] + '](' + dist['public_release_url'] + ')。\n'
                     '- 支持范围与安装：见公开版 README；对 AI 说“' + meta.get('invocation_phrase', '按 README 检查环境并使用此 Skill') + '”。\n\n')
            body = ensure_takeaway(body)
            body = re.sub(r'^## 拿去用\s*\n.*?(?=^## |\Z)', lambda m: block, body, flags=re.M | re.S)
            meta.update(public_repo_url=dist['public_repo_url'], public_release_url=dist['public_release_url'], public_distribution_status='public_release_verified')
            text = '---\n' + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + '---\n\n' + body.strip() + '\n'
            if path.read_text() != text:
                writes[path] = text
                index['cards'][meta['id']] = {'path': str(path.relative_to(c['vault'])), 'metadata': normalized(meta)}
        if not writes:
            return {'status': 'unchanged', 'cards': 0}
        writes[c['card_index']] = json.dumps(index, ensure_ascii=False, indent=2) + '\n'
        before = {p: p.read_bytes() for p in writes}
        backup = home / 'backups' / uuid.uuid4().hex
        shutil.copytree(c['vault'], backup / 'vault-before')
        shutil.copy2(c['card_index'], backup / 'card-index-before.json')
        try:
            for p, text in writes.items():
                if p.read_bytes() != before[p]:
                    raise ValueError('card changed during backfill')
                private_write(p, text)
            check = validate(c['vault'], c['module_config'], c['card_index'], c['source_index'])
            if not check['pass']:
                raise ValueError('backfill validation failed')
        except Exception:
            for p, raw in before.items():
                if p.read_bytes() not in (raw, writes[p].encode()):
                    raise ValueError('backfill rollback conflict; preserve backup')
            for p, raw in before.items():
                private_write(p, raw.decode())
            raise
        return {'status': 'passed', 'cards': len(writes) - 1, 'backup': str(backup)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config/publication.local.json')
    parser.add_argument('command', choices=['run', 'backfill'])
    args = parser.parse_args(argv)
    try:
        if args.command == 'run':
            result = reconcile(args.config)
        else:
            c = configuration(args.config)
            result = backfill_release(args.config, c['registry'])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get('state') == 'attention_required' else 0
    except (ValueError, OSError, KeyError, sync.Blocked) as exc:
        print(json.dumps({'state': 'blocked', 'reason': getattr(exc, 'reason', type(exc).__name__)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
