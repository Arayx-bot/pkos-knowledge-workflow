"""Read four local sources without executing embedded commands or AI reasoning."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import zipfile


def timestamp(value):
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(value / 1000 if value > 100_000_000_000 else value, timezone.utc)
    if isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return result if result.tzinfo else None
        except ValueError:
            return None
    return None


def text_blocks(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    return '\n'.join(block if isinstance(block, str) else block.get('text', '')
                     for block in content if isinstance(block, str) or
                     isinstance(block, dict) and block.get('type') in ('text', 'input_text', 'output_text'))


def record(source, session, identifier, role, text, when, path, line=None, **extra):
    return {'source': source, 'session_id': session, 'message_id': identifier,
            'role': role, 'text': text, 'timestamp': when.isoformat() if when else None,
            'path': str(path), 'line': line,
            'attribution': 'owner_statement_needs_context' if role == 'user' else 'ai_suggestion',
            **extra}


def active_chain(rows, leaf):
    nodes = {r['id']: r for r in rows if r.get('id') and r.get('type') != 'session'}
    chain, seen = set(), set()
    while leaf:
        if leaf in seen or leaf not in nodes:
            raise ValueError('broken or cyclic branch ancestry')
        seen.add(leaf); chain.add(leaf)
        leaf = nodes[leaf].get('parentId')
    return chain


def pi_records(path):
    lines = [(n, json.loads(line)) for n, line in enumerate(path.read_text().splitlines(), 1) if line.strip()]
    if not lines or lines[0][1].get('type') != 'session' or lines[0][1].get('version') != 3:
        raise ValueError('unsupported Pi session format')
    header = lines[0][1]
    rows = [row for _, row in lines]
    leaf = next((row['id'] for row in reversed(rows) if row.get('id') and row.get('type') != 'session'), None)
    active = active_chain(rows, leaf)
    context_edited = any(row.get('type') == 'context_edit' for row in rows)
    for line, row in lines:
        if row.get('type') != 'message' or row.get('id') not in active:
            continue
        message = row.get('message', {})
        role = message.get('role')
        if role not in ('user', 'assistant'):
            continue
        text = text_blocks(message.get('content'))
        if text:
            yield record('D', header.get('id'), row.get('id'), role, text,
                         timestamp(row.get('timestamp')) or timestamp(message.get('timestamp')), path, line,
                         time_basis='session_message', branch_leaf=leaf,
                         context_edit_present=context_edited, original_entry=True)


def codex_records(path):
    session = None
    for line, raw in enumerate(path.read_text().splitlines(), 1):
        if not raw.strip():
            continue
        row = json.loads(raw); payload = row.get('payload', {})
        if row.get('type') == 'session_meta' and session is None:
            session = payload.get('id')
            if payload.get('source') == 'subagent' or isinstance(payload.get('source'), dict) and 'subagent' in payload['source']:
                return
        if row.get('type') != 'response_item' or payload.get('type') != 'message':
            continue
        role = payload.get('role')
        if role not in ('user', 'assistant'):
            continue
        text = text_blocks(payload.get('content'))
        if not text or text.startswith(('<environment_context>', '# AGENTS.md instructions', '<permissions instructions>')):
            continue
        yield record('C', session, payload.get('id'), role, text, timestamp(row.get('timestamp')), path, line,
                     time_basis='session_message')


def official_documents(path):
    if path.suffix == '.zip':
        with zipfile.ZipFile(path) as archive:
            names = sorted(n for n in archive.namelist() if Path(n).name.startswith('conversations') and n.endswith('.json'))
            if not names:
                raise ValueError('no conversation volumes')
            for name in names:
                with archive.open(name) as handle:
                    value = json.load(handle)
                if not isinstance(value, list):
                    raise ValueError('invalid conversation volume')
                yield from value
    else:
        value = json.loads(path.read_text())
        if not isinstance(value, list):
            raise ValueError('invalid conversation volume')
        yield from value


def official_records(path):
    for conversation in official_documents(path):
        mapping = conversation.get('mapping', {})
        leaf = conversation.get('current_node')
        active = set()
        if not leaf:
            raise ValueError('official active branch unknown')
        while leaf:
            if leaf in active or leaf not in mapping:
                raise ValueError('invalid official branch')
            active.add(leaf); leaf = mapping[leaf].get('parent')
        for key, node in mapping.items():
            message = node.get('message')
            if key not in active or not message:
                continue
            role = message.get('author', {}).get('role')
            if role not in ('user', 'assistant'):
                continue
            content = message.get('content', {})
            text = text_blocks(content.get('parts', []))
            if text:
                yield record('A', conversation.get('id') or conversation.get('conversation_id'), message.get('id'),
                             role, text, timestamp(message.get('create_time')), path,
                             time_basis='official_message')


def web_records(path):
    if not path.is_file():
        return
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        columns = {r[1] for r in connection.execute('PRAGMA table_info(messages)')}
        mode = 'capture_mode' if 'capture_mode' in columns else "'unknown' AS capture_mode"
        for row in connection.execute(f'SELECT conversation_id,message_id,role,content,captured_at,message_time,{mode} FROM messages'):
            yield record('B', row['conversation_id'], row['message_id'], row['role'], row['content'],
                         timestamp(row['captured_at']), path, time_basis='captured_at',
                         capture_mode=row['capture_mode'], message_time=row['message_time'])
    finally:
        connection.close()


def paths_under(roots):
    for root in roots:
        root = Path(root).expanduser()
        if root.exists():
            yield from sorted(root.rglob('*.jsonl'))


def gather(config, start, end):
    records, status = [], {}
    sources = {
        'A': [(Path(item['path']).expanduser(), official_records) for item in config.get('official_exports', [])],
        'B': [(Path(config['web_database']).expanduser(), web_records)] if config.get('web_database') else [],
        'C': [(p, codex_records) for p in paths_under(config.get('codex_roots', []))],
        'D': [(p, pi_records) for p in paths_under(config.get('pi_roots', []))],
    }
    for source, files in sources.items():
        status[source] = {'files': len(files), 'errors': [], 'messages_in_week': 0, 'unassigned': 0}
        for path, reader in files:
            try:
                # Reject a malformed file as a whole, rather than use a partial branch.
                rows = list(reader(path))
                for item in rows:
                    when = timestamp(item['timestamp'])
                    if not when:
                        status[source]['unassigned'] += 1
                        continue
                    if start <= when < end:
                        item['week_eligible'] = source != 'B' or item.get('capture_mode') == 'live_incremental'
                        if not item['week_eligible']:
                            status[source]['unassigned'] += 1
                        records.append(item)
                        status[source]['messages_in_week'] += 1
            except (ValueError, OSError, sqlite3.Error, zipfile.BadZipFile) as error:
                status[source]['errors'].append({'path': str(path), 'kind': type(error).__name__})
    # Match official identifiers across all dates, so later capture does not move an older message.
    official = {}
    for path, reader in sources['A']:
        try:
            official.update({(r['session_id'], r['message_id']): r for r in reader(path) if r['message_id']})
        except (ValueError, OSError, zipfile.BadZipFile):
            pass
    result, seen = [], set()
    for item in records:
        identity = (item['session_id'], item['message_id'])
        if item['source'] == 'B' and identity in official:
            item['official_match'] = True
            item['week_eligible'] = False
            item['canonical_timestamp'] = official[identity]['timestamp']
        key = (item['source'], item['session_id'], item['message_id'] or (item['path'], item['line']))
        if key not in seen:
            seen.add(key); item['evidence_id'] = f'E{len(result)+1:05d}'; result.append(item)
    return result, status


def reconcile(config):
    official = {}
    for item in config.get('official_exports', []):
        for row in official_records(Path(item['path']).expanduser()):
            official[(row['session_id'], row['message_id'])] = row
    findings, matched = [], set()
    if config.get('web_database'):
        for row in web_records(Path(config['web_database']).expanduser()):
            key = (row['session_id'], row['message_id']); canonical = official.get(key)
            if canonical:
                matched.add(key)
            findings.append({'session_id': key[0], 'message_id': key[1],
                             'state': 'matched' if canonical else 'unmatched_keep',
                             'captured_at': row['timestamp'],
                             'official_time': canonical['timestamp'] if canonical else None,
                             'content_conflict': canonical is not None and canonical['text'] != row['text']})
    for key, row in official.items():
        if key not in matched:
            findings.append({'session_id': key[0], 'message_id': key[1], 'state': 'official_only', 'official_time': row['timestamp']})
    return findings
