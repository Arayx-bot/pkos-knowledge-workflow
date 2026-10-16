"""Scoped incremental learning intake; the chat agent supplies semantic decisions."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid
import yaml

from validation.module_config import read_config, check_directories
from validation.validate import validate, load_note, normalized
from weekly.sources import text_blocks, timestamp
from weekly.workflow import lock, outside_repo, private_write, save_json, ZONE

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / 'config/automatic.local.json'
KINDS = {'thought': 'thinking', 'knowledge': 'knowledge', 'project': 'projects', 'tool': 'skills'}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def file_digest(path):
    return digest(path.read_bytes()) if path.exists() else None


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def now():
    return datetime.now(ZONE).isoformat()


def config_at(path):
    cfg = read_json(Path(path))
    if cfg.get('schema_version') != 1 or not isinstance(cfg.get('enabled'), bool):
        raise ValueError('invalid automatic configuration')
    authorization = cfg.get('authorization', {})
    if not authorization.get('reference') or not authorization.get('quote') or any(
            str(authorization[key]).startswith('REPLACE_') for key in ('reference', 'quote')):
        raise ValueError('explicit scoped owner authorization is required')
    for key in ('system_data', 'vault', 'module_config', 'card_index', 'source_index'):
        cfg[key] = Path(cfg[key]).expanduser().resolve()
        if not cfg[key].exists():
            raise ValueError('configured path is missing: ' + key)
    outside_repo(cfg['system_data'])
    if cfg['system_data'].is_relative_to(cfg['vault']):
        raise ValueError('system data must be outside Vault')
    for key in ('card_index', 'source_index'):
        if not cfg[key].is_relative_to(cfg['system_data']):
            raise ValueError('indexes must belong to configured system data')
    if not cfg.get('sessions'):
        raise ValueError('no explicitly authorized sessions')
    session_ids = set()
    for entry in cfg['sessions']:
        if not entry.get('id') or entry['id'] in session_ids:
            raise ValueError('invalid or duplicate authorized session')
        session_ids.add(entry['id'])
        entry['path'] = str(Path(entry['path']).expanduser().resolve())
    data, directories = read_config(cfg['module_config'])
    check_directories(cfg['vault'], data, directories)
    for kind, routing in cfg['routing'].items():
        if kind not in KINDS:
            raise ValueError('unsupported automatic card type')
        parent = within(cfg['vault'], routing['folder'])
        overview = within(cfg['vault'], routing['overview'])
        if not parent.is_dir() or not overview.is_file():
            raise ValueError('configured card folder or overview missing')
        if parent.relative_to(cfg['vault']).parts[0] != directories[KINDS[kind]]:
            raise ValueError('card folder does not match module')
        if overview.relative_to(cfg['vault']).parts[0] != directories[KINDS[kind]]:
            raise ValueError('overview does not match module')
    return cfg


def within(root, relative):
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise ValueError('unsafe relative path')
    path = root / rel
    if not path.resolve().is_relative_to(root):
        raise ValueError('path escapes configured root')
    if path.is_symlink():
        raise ValueError('write target cannot be a symlink')
    return path


def state_at(cfg):
    path = cfg['system_data'] / 'automatic/STATE.json'
    return read_json(path) if path.exists() else {
        'schema_version': 1, 'cursors': {}, 'pending': None, 'core_keys': {}, 'held': []}


def require_ready(cfg):
    if not cfg['enabled']:
        raise ValueError('automatic intake is paused')
    for journal in (cfg['system_data'] / 'automatic/transactions').glob('*/JOURNAL.json'):
        if read_json(journal)['state'] == 'writing':
            raise ValueError('unfinished transaction; recover before processing: ' + journal.parent.name)


def selected_messages(entry, cursor, limit=None):
    """Read one authorized file, never enumerate other chats or tool outputs."""
    path = Path(entry['path'])
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    session, result, end = None, [], cursor
    for line, chunk in enumerate(lines, 1):
        if not chunk.endswith(b'\n'):
            break  # Appending JSONL may end with a partial record.
        row = json.loads(chunk)
        payload = row.get('payload', {})
        if row.get('type') == 'session_meta':
            if session is not None:
                raise ValueError('multiple session headers')
            session = payload.get('id')
            if session != entry['id']:
                raise ValueError('session file does not match authorization')
            origin = payload.get('source')
            if origin == 'subagent' or isinstance(origin, dict) and 'subagent' in origin:
                raise ValueError('subagent is outside learning chat scope')
        end = line
        if line <= cursor or row.get('type') != 'response_item' or payload.get('type') != 'message':
            continue
        if session is None:
            raise ValueError('message before authorized session header')
        role = payload.get('role')
        if role not in ('user', 'assistant'):
            continue
        if role == 'assistant' and payload.get('phase') not in (None, 'final_answer'):
            continue
        text = text_blocks(payload.get('content'))
        if not text or text.startswith(('# AGENTS.md instructions', '<environment_context>',
                                       '<external_codex_apps_open_page>', '<permissions instructions>',
                                       '[PKOS_AUTO_INTAKE]')):
            continue
        when = timestamp(row.get('timestamp'))
        if not when:
            raise ValueError('selected message timestamp is unknown')
        identifier = payload.get('id') or f'line-{line}'
        key = digest((session + ':' + identifier).encode())[:24]
        result.append({'evidence_id': 'AUTO-' + key, 'session_id': session,
                       'message_id': payload.get('id'), 'line': line, 'role': role,
                       'text': text, 'timestamp': when.isoformat(), 'path': str(path),
                       'time_basis': 'session_message', 'source': 'codex'})
        if limit and len(result) >= limit:
            break
    if session is None or end < cursor:
        raise ValueError('authorized session is missing or truncated')
    return result, end


def checked_vault(cfg):
    report = validate(cfg['vault'], cfg['module_config'], cfg['card_index'], cfg['source_index'])
    if not report['pass']:
        raise ValueError('Vault validation failed: ' + '; '.join(report['errors'][:5]))
    return report


def vault_digest(cfg):
    return digest(json_text({str(p.relative_to(cfg['vault'])): file_digest(p)
                             for p in sorted(cfg['vault'].rglob('*.md'))}).encode())


def status(cfg):
    state = state_at(cfg)
    return {'enabled': cfg['enabled'], 'scope': [e['id'] for e in cfg['sessions']],
            'pending': state['pending'], 'held': len(state['held']),
            'cursors': state['cursors'], 'validation': checked_vault(cfg)}


def prepare(cfg):
    root = cfg['system_data'] / 'automatic'
    with lock(root):
        require_ready(cfg)
        state = state_at(cfg)
        if state['pending']:
            batch = within(root / 'batches', state['pending'])
            return {'state': 'awaiting_editor', 'batch_id': state['pending'], 'directory': str(batch)}
        checked_vault(cfg)
        messages, cursors = [], dict(state['cursors'])
        for entry in cfg['sessions']:
            rows, end = selected_messages(entry, cursors.get(entry['id'], 0), 100)
            messages.extend(rows)
            cursors[entry['id']] = end
        if not any(r['role'] == 'user' for r in messages):
            if cursors != state['cursors']:
                state['cursors'] = cursors
                save_json(root / 'STATE.json', state)
            return {'state': 'idle', 'messages': 0}
        context = []
        for entry in cfg['sessions']:
            previous, _ = selected_messages(entry, 0)
            context.extend([r for r in previous if r['line'] <= state['cursors'].get(entry['id'], 0)][-20:])
        # Unresolved material remains available when the owner later clarifies it.
        for held in state['held']:
            old = within(root / 'batches', held['batch_id']) / 'EVIDENCE.jsonl'
            context.extend(json.loads(line) for line in old.read_text().splitlines()
                           if json.loads(line)['evidence_id'] in held['evidence_ids'])
        context = list({r['evidence_id']: r for r in context}.values())
        batch_id = datetime.now(ZONE).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
        batch = root / 'batches' / batch_id
        batch.mkdir(parents=True, mode=0o700)
        manifest = {'batch_id': batch_id, 'created_at': now(), 'cursors': cursors,
                    'card_index_sha': file_digest(cfg['card_index']),
                    'source_index_sha': file_digest(cfg['source_index']),
                    'vault_sha': vault_digest(cfg),
                    'message_count': len(messages)}
        private_write(batch / 'EVIDENCE.jsonl', ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in messages), True)
        private_write(batch / 'CONTEXT.jsonl', ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in context), True)
        save_json(batch / 'BATCH.json', manifest, True)
        state['pending'] = batch_id
        save_json(root / 'STATE.json', state)
        return {'state': 'awaiting_editor', 'batch_id': batch_id, 'directory': str(batch), 'messages': len(messages)}


def note_text(meta, body):
    return '---\n' + yaml.safe_dump(normalized(meta), allow_unicode=True, sort_keys=False) + '---\n\n' + body.strip() + '\n'


def plan_decisions(plan, manifest, evidence, cards, state, context=()):
    if plan.get('batch_id') != manifest['batch_id']:
        raise ValueError('plan does not match pending batch')
    required = {r['evidence_id'] for r in evidence}
    by_id = {r['evidence_id']: r for r in [*context, *evidence]}
    used, ids, keys, targets = set(), set(), set(), set()
    decisions = plan.get('decisions')
    if not isinstance(decisions, list):
        raise ValueError('decisions must be a list')
    for item in decisions:
        ident = item.get('id', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', ident) or ident in ids:
            raise ValueError('invalid or duplicate decision ID')
        ids.add(ident)
        refs = item.get('evidence_ids')
        if not isinstance(refs, list) or not refs or any(r not in by_id for r in refs):
            raise ValueError('decision requires actual batch evidence')
        used.update(refs)
        action = item.get('action')
        if action not in ('new', 'update', 'ignore', 'hold'):
            raise ValueError('invalid automatic decision')
        if action in ('ignore', 'hold'):
            if not item.get('reason') or item.get('body'):
                raise ValueError('non-writing decisions require reason and no body')
            continue
        if not required.intersection(refs):
            raise ValueError('writing requires new evidence, not context alone')
        kind = item.get('type')
        if kind not in KINDS:
            raise ValueError('unsupported automatic type; scripts and weekly records require their own workflow')
        title = item.get('title', '')
        if not title or title != title.strip() or len(title) > 100 or any(c in title for c in '/\\\0\n\r[]#|:') or title.startswith('.'):
            raise ValueError('unsafe card title')
        body = item.get('body', '')
        if not isinstance(body, str) or not body.strip() or re.search(r'^# ', body, re.M):
            raise ValueError('card body must exclude the top-level title')
        if item.get('attribution') not in ('owner_view', 'external_knowledge', 'verified_project', 'documented_tool'):
            raise ValueError('explicit attribution required')
        if kind == 'thought':
            quotes = item.get('owner_quotes', [])
            if item['attribution'] != 'owner_view' or not quotes or any(
                    not isinstance(q, str) or not q.strip() or not any(q in by_id[r]['text'] and by_id[r]['role'] == 'user' for r in refs)
                    for q in quotes):
                raise ValueError('thought needs exact owner quotes, not assistant-only inference')
        if kind == 'knowledge':
            if item['attribution'] != 'external_knowledge' or not item.get('verification'):
                raise ValueError('knowledge needs checked external evidence')
            for check in item['verification']:
                when = timestamp(check.get('checked_at'))
                if not re.match(r'^https?://[^\s]+$', check.get('url', '')) or not when or when > datetime.now(ZONE) or not check.get('finding'):
                    raise ValueError('external verification requires URL, actual time, and finding')
            if not item.get('subtype') and any('## ' + heading not in body for heading in ('知识定义', '核心原理', '适用场景', '使用方法')):
                raise ValueError('general knowledge card needs the four V1.2 sections')
        if kind in ('project', 'tool'):
            expected = 'verified_project' if kind == 'project' else 'documented_tool'
            if item['attribution'] != expected or not item.get('fact_basis'):
                raise ValueError('project/tool requires a documented fact basis and matching attribution')
        if kind == 'tool':
            prior_subtype = cards.get(item.get('existing_id'), {}).get('metadata', {}).get('subtype')
            if item.get('subtype', prior_subtype) not in ('internal', 'external'):
                raise ValueError('tool card requires internal/external ownership classification')
            if item.get('tool_registry_id') and not re.fullmatch(r'TOOL-\d{3}', item['tool_registry_id']):
                raise ValueError('invalid tool registry ID')
        elif item.get('subtype') in ('internal', 'external'):
            raise ValueError('tool ownership subtype requires tool card')
        key = item.get('core_key', '')
        if not isinstance(key, str) or not key.strip() or key in keys:
            raise ValueError('unique semantic core key required')
        keys.add(key)
        existing = item.get('existing_id')
        if action == 'new':
            if existing or key in state['core_keys']:
                raise ValueError('existing core key must update its stable card')
            if any(Path(r['path']).stem.casefold() == title.casefold() for r in cards.values()):
                raise ValueError('matching card title already exists; update instead')
        else:
            if existing not in cards or existing in targets or cards[existing]['metadata']['type'] != kind:
                raise ValueError('update must preserve one existing stable ID and type')
            if key in state['core_keys'] and state['core_keys'][key] != existing:
                raise ValueError('semantic core belongs to another card')
            targets.add(existing)
        related = item.get('related', [])
        if not isinstance(related, list) or any(r not in cards or r == existing for r in related):
            raise ValueError('related references must point to other existing cards')
        if item.get('audience') not in (None, 'self') or item.get('subtype') not in (None, 'term', 'person', 'counterintuitive', 'internal', 'external'):
            raise ValueError('unsupported audience or subtype')
        if item.get('subtype') in ('term', 'person') and kind != 'knowledge' or item.get('subtype') == 'counterintuitive' and kind not in ('thought', 'knowledge'):
            raise ValueError('subtype does not match the card type')
    if not required <= used:
        raise ValueError('every selected message needs an explicit disposition')
    held_keys = {(h['batch_id'], h['id']) for h in state['held']}
    resolved = plan.get('resolved_holds', [])
    if any(not isinstance(h, dict) or (h.get('batch_id'), h.get('id')) not in held_keys for h in resolved):
        raise ValueError('resolved hold does not exist')
    return decisions, by_id


def rollback(journal, directory):
    # Do not overwrite edits made after the interrupted write.
    for item in journal['targets']:
        if file_digest(Path(item['path'])) not in (item['before_sha'], item['after_sha']):
            raise ValueError('rollback target changed externally; preserve for review')
    for item in reversed(journal['targets']):
        path = Path(item['path'])
        if item['before_sha'] is None:
            path.unlink(missing_ok=True)
        else:
            private_write(path, (directory / item['backup']).read_text(encoding='utf-8'))
    journal.update(state='rolled_back', finished_at=now())
    save_json(directory / 'JOURNAL.json', journal)


def transact(cfg, batch_id, writes, validator=checked_vault):
    directory = cfg['system_data'] / 'automatic/transactions' / batch_id
    if directory.exists():
        directory = directory.with_name(batch_id + '-retry-' + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    # Full local Vault snapshot; raw chats and source code are not copied.
    shutil.copytree(cfg['vault'], directory / 'vault-before', symlinks=True)
    journal = {'state': 'writing', 'batch_id': batch_id, 'started_at': now(), 'targets': []}
    for number, (path, text) in enumerate(writes.items()):
        backup = f'before/{number}.txt' if path.exists() else None
        if backup:
            private_write(directory / backup, path.read_text(encoding='utf-8'), True)
        journal['targets'].append({'path': str(path), 'before_sha': file_digest(path),
                                   'after_sha': digest(text.encode()), 'backup': backup})
    save_json(directory / 'JOURNAL.json', journal, True)
    try:
        for path, text in writes.items():
            private_write(path, text)
        validation = validator(cfg)
    except Exception:
        rollback(journal, directory)
        raise
    journal.update(state='committed', finished_at=now(), validation=validation)
    save_json(directory / 'JOURNAL.json', journal)
    return directory


def apply(cfg, plan, validator=checked_vault):
    root = cfg['system_data'] / 'automatic'
    with lock(root), lock(cfg['system_data'] / 'vault-writer'):
        require_ready(cfg)
        state = state_at(cfg)
        if state['pending'] is None:
            raise ValueError('no pending batch; already applied or not prepared')
        batch = within(root / 'batches', state['pending'])
        manifest = read_json(batch / 'BATCH.json')
        if file_digest(cfg['card_index']) != manifest['card_index_sha'] or file_digest(cfg['source_index']) != manifest['source_index_sha'] or vault_digest(cfg) != manifest['vault_sha']:
            raise ValueError('indexes changed since prepare; rebase batch before editing')
        checked_vault(cfg)
        evidence = [json.loads(line) for line in (batch / 'EVIDENCE.jsonl').read_text().splitlines()]
        context = [json.loads(line) for line in (batch / 'CONTEXT.jsonl').read_text().splitlines()]
        index, sources = read_json(cfg['card_index']), read_json(cfg['source_index'])
        decisions, by_id = plan_decisions(plan, manifest, evidence, index['cards'], state, context)
        resolved = {(h['batch_id'], h['id']) for h in plan.get('resolved_holds', [])}
        state['held'] = [h for h in state['held'] if (h['batch_id'], h['id']) not in resolved]
        writes, summary = {}, []
        date = datetime.now(ZONE).date().isoformat()
        for item in decisions:
            if item['action'] in ('ignore', 'hold'):
                if item['action'] == 'hold':
                    state['held'].append({'batch_id': state['pending'], **item})
                summary.append({'decision': item['id'], 'action': item['action'], 'reason': item['reason']})
                continue
            for ref in item['evidence_ids']:
                record = by_id[ref]
                source_file = cfg['source_index'].parent / (ref + '.md')
                text = '# ' + ref + '\n\n' + json_text({k: v for k, v in record.items() if k != 'text'}) + '\n' + record['text'] + '\n'
                if ref in sources['sources'] and sources['sources'][ref]['sha256'] != digest(text.encode()):
                    raise ValueError('existing source identity has conflicting content')
                writes[source_file] = text
                sources['sources'][ref] = {'file': source_file.name, 'sha256': digest(text.encode()),
                                          'kind': 'learning_chat_message', 'role': record['role'],
                                          'observed_date': record['timestamp'], 'original': record['path']}
            if item['action'] == 'new':
                ident = index['next_id']
                if ident in index['cards']:
                    raise ValueError('next ID collision')
                index['next_id'] = 'PKOS-' + format(int(ident.split('-')[1]) + 1, '06d')
                routing = cfg['routing'].get(item['type'])
                if routing is None:
                    raise ValueError('automatic creation not configured for this card type')
                path = within(cfg['vault'], str(Path(routing['folder']) / (item['title'] + '.md')))
                if path.exists() or path in writes:
                    raise ValueError('new card would overwrite an existing file')
                meta = {'id': ident, 'type': item['type'], 'created': date, 'schema_version': '1.2', 'status': 'draft'}
                overview = within(cfg['vault'], routing['overview'])
                navigation = writes.get(overview, overview.read_text(encoding='utf-8'))
                link = '[[' + str(path.relative_to(cfg['vault'])).removesuffix('.md') + '|' + item['title'] + ']]'
                writes[overview] = navigation.rstrip() + '\n\n- ' + link + '\n'
            else:
                ident = item['existing_id']
                path = within(cfg['vault'], index['cards'][ident]['path'])
                meta, _ = load_note(path)
                # Stable path/title keeps pre-existing Obsidian links intact.
                if item['title'] != path.stem:
                    raise ValueError('automatic update must retain existing title/path')
            meta.update(visibility='private', updated=date, editorial_status='auto_ingested',
                        automation_batch=state['pending'], automation_authorization=cfg['authorization']['reference'],
                        attribution=item['attribution'], statement_kind='ai_editorial_summary',
                        core_key=item['core_key'])
            meta['source_refs'] = list(dict.fromkeys(meta.get('source_refs', []) + item['evidence_ids']))
            meta['related'] = list(dict.fromkeys(meta.get('related', []) + item.get('related', [])))
            if meta.get('audience') == 'self' or item.get('audience') == 'self' or 'kufucat' in item['title'].lower():
                meta['audience'] = 'self'
            for field in ('subtype', 'owner_quotes', 'verification', 'fact_basis', 'tool_registry_id', 'public_distribution_key', 'skill_name', 'invocation_phrase'):
                if item.get(field):
                    meta[field] = item[field]
            if item['type'] == 'thought':
                meta.setdefault('cognition_label', '🌱 正在形成')
            if item.get('subtype') == 'counterintuitive':
                meta['tags'] = list(dict.fromkeys(meta.get('tags', []) + ['反常识']))
                meta['insight_origin'] = 'personal' if item['type'] == 'thought' else 'external'
            body = item['body']
            if item['type'] == 'tool' and meta.get('subtype') == 'internal':
                from maintenance.skill_publication import ensure_takeaway
                body = ensure_takeaway(body)
            writes[path] = note_text(meta, '# ' + item['title'] + '\n\n' + body)
            index['cards'][ident] = {'path': str(path.relative_to(cfg['vault'])), 'metadata': normalized(meta)}
            state['core_keys'][item['core_key']] = ident
            summary.append({'decision': item['id'], 'action': item['action'], 'id': ident, 'path': str(path)})
        from maintenance.skill_publication import enqueue
        tool_cards = [row['id'] for row in summary if row.get('id') and index['cards'][row['id']]['metadata']['type'] == 'tool']
        enqueue(writes, cfg['system_data'], manifest['batch_id'], tool_cards)
        writes[cfg['card_index']] = json_text(index)
        writes[cfg['source_index']] = json_text(sources)
        state.update(cursors=manifest['cursors'], pending=None, last_applied_at=now(), last_batch=manifest['batch_id'])
        writes[root / 'STATE.json'] = json_text(state)
        result = {'batch_id': manifest['batch_id'], 'finished_at': now(), 'decisions': summary,
                  'cards_written': sum(r['action'] in ('new', 'update') for r in summary)}
        writes[batch / 'RESULT.json'] = json_text(result)
        writes[batch / 'PLAN.json'] = json_text(plan)
        snapshot = transact(cfg, manifest['batch_id'], writes, validator)
        publication = {'state': 'not_needed'}
        if tool_cards:
            publication = {'state': 'pending_configuration'}
            if cfg.get('publication_config'):
                try:
                    from maintenance.skill_publication import reconcile
                    publication = reconcile(cfg['publication_config'])
                except Exception as exc:
                    # Card transaction is already durable; outbox remains pending for retry.
                    publication = {'state': 'pending_retry', 'error_type': type(exc).__name__}
        return {**result, 'snapshot': str(snapshot), 'validation': 'passed', 'publication': publication}


def recover(cfg, transaction):
    directory = within(cfg['system_data'] / 'automatic/transactions', transaction)
    with lock(cfg['system_data'] / 'automatic'), lock(cfg['system_data'] / 'vault-writer'):
        journal = read_json(directory / 'JOURNAL.json')
        if journal['state'] != 'writing':
            raise ValueError('only unfinished transactions may be recovered')
        rollback(journal, directory)
        return {'state': 'rolled_back', 'validation': checked_vault(cfg)}


def rebase(cfg):
    root = cfg['system_data'] / 'automatic'
    with lock(root):
        require_ready(cfg)
        state = state_at(cfg)
        if not state['pending']:
            raise ValueError('no pending batch')
        checked_vault(cfg)
        path = within(root / 'batches', state['pending']) / 'BATCH.json'
        manifest = read_json(path)
        save_json(path.parent / ('BASE-' + uuid.uuid4().hex[:8] + '.json'), manifest, True)
        manifest.update(card_index_sha=file_digest(cfg['card_index']), source_index_sha=file_digest(cfg['source_index']), vault_sha=vault_digest(cfg), rebased_at=now())
        save_json(path, manifest)
        return {'state': 'awaiting_editor', 'batch_id': state['pending'], 'note': 'reread current cards and rebuild plan'}


def set_enabled(path, enabled):
    cfg = read_json(path)
    cfg['enabled'] = enabled
    save_json(path, cfg)
    return {'enabled': enabled}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('status', 'prepare', 'rebase', 'pause', 'resume'):
        commands.add_parser(name)
    commands.add_parser('apply').add_argument('--plan', type=Path, required=True)
    commands.add_parser('recover').add_argument('--transaction', required=True)
    args = parser.parse_args(argv)
    try:
        cfg = config_at(args.config)
        if args.command in ('pause', 'resume'):
            result = set_enabled(args.config, args.command == 'resume')
        elif args.command == 'apply':
            result = apply(cfg, read_json(args.plan))
        elif args.command == 'recover':
            result = recover(cfg, args.transaction)
        else:
            result = {'status': status, 'prepare': prepare, 'rebase': rebase}[args.command](cfg)
        print(json_text(result), end='')
        return 0
    except (ValueError, KeyError, TypeError, OSError, yaml.YAMLError) as exc:
        print(json_text({'state': 'blocked', 'error': str(exc)}), end='')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
