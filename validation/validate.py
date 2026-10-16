"""Read-only validation of the current PKOS; JSON goes only to stdout."""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlparse
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_paths import load_paths, required_path
from module_config import read_config, check_directories


def without_code(text):
    text = re.sub(r'(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$', '', text)
    return re.sub(r'(`+).*?\1', '', text, flags=re.S)


def load_note(path):
    text = path.read_text(encoding='utf-8')
    match = re.match(r'\A---\n(.*?)\n---(?:\n|$)', text, re.S)
    if match:
        meta = yaml.safe_load(match[1])
        if not isinstance(meta, dict):
            raise ValueError('frontmatter must be a mapping')
        return meta, text[match.end():]
    return {}, text


def normalized(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def validate(vault, module_config=None, card_index=None, source_index=None):
    vault = Path(vault).resolve()
    data, directories = read_config(module_config)
    check_directories(vault, data, directories)
    errors, cards, bodies = [], {}, {}
    actual_dirs = {p.name for p in vault.iterdir() if p.is_dir() and not p.name.startswith('.')}
    if actual_dirs != set(directories.values()):
        errors.append('unexpected top-level module directories')
    if {p.name for p in vault.glob('*.md')} != {'00-首页.md'}:
        errors.append('unexpected top-level notes')
    files = sorted(vault.rglob('*.md'))
    stems = collections.defaultdict(list)
    kinds = {kind: key for key, row in data['modules'].items() for kind in row['types']}
    for path in files:
        rel = str(path.relative_to(vault))
        if not path.resolve().is_relative_to(vault):
            errors.append(rel + ': symlink escapes Vault')
            continue
        try:
            meta, body = load_note(path)
        except (ValueError, OSError, yaml.YAMLError) as exc:
            errors.append(rel + ': ' + str(exc))
            continue
        clean = without_code(body)
        if len(re.findall(r'^# .+', clean, re.M)) != 1:
            errors.append(rel + ': must have one title')
        stems[path.stem].append(path)
        bodies[path] = clean
        if not meta:
            if path != vault / '00-首页.md' and len(path.relative_to(vault).parts) != 2:
                errors.append(rel + ': content card has no metadata')
            continue
        for key in ('id', 'type', 'created', 'status'):
            if not meta.get(key):
                errors.append(rel + ': missing ' + key)
        ident = meta.get('id')
        if not isinstance(ident, str) or not re.fullmatch(r'PKOS-\d{6}', ident):
            errors.append(rel + ': invalid card ID')
            continue
        if ident in cards:
            errors.append('duplicate card ID: ' + ident)
            continue
        module_id = kinds.get(meta.get('type'))
        if module_id is None or path.relative_to(vault).parts[0] != directories[module_id]:
            errors.append(ident + ': type/module mismatch')
        if meta.get('visibility', 'private') not in ('private', 'public'):
            errors.append(ident + ': invalid visibility')
        cards[ident] = {'path': rel, 'metadata': normalized(meta)}
    for stem, candidates in stems.items():
        if len(candidates) > 1:
            errors.append('ambiguous display name: ' + stem)

    source_rows = {}
    if source_index is not None:
        source_index = Path(source_index)
        source_rows = json.loads(source_index.read_text())['sources']
        if not isinstance(source_rows, dict):
            raise ValueError('source index must contain a mapping')
        source_root = source_index.resolve().parent
        for ident, row in source_rows.items():
            candidate = (source_root / row['file']).resolve()
            if not candidate.is_relative_to(source_root) or not candidate.is_file():
                errors.append('source path escapes or is missing: ' + ident)
            elif hashlib.sha256(candidate.read_bytes()).hexdigest() != row['sha256']:
                errors.append('source hash mismatch: ' + ident)
    for ident, row in cards.items():
        for field in ('source_refs', 'related'):
            refs = row['metadata'].get(field, [])
            if not isinstance(refs, list) or any(not isinstance(x, str) for x in refs):
                errors.append(ident + ': invalid ' + field)
                continue
            for ref in refs:
                if ref not in cards and (field == 'related' or ref not in source_rows):
                    errors.append(ident + ': unresolved ' + field + ' ' + ref)

    def resolve(target, origin):
        target = unquote(target).removesuffix('.md')
        if not target:
            return origin
        for candidate in (vault / (target + '.md'), origin.parent / (target + '.md')):
            if candidate.is_file() and candidate.resolve().is_relative_to(vault):
                return candidate
        choices = stems.get(target, [])
        return choices[0] if len(choices) == 1 else None

    wiki_count, local_count = 0, 0
    for path, clean in bodies.items():
        rel = str(path.relative_to(vault))
        for raw in re.findall(r'\[\[([^\]]+)\]\]', clean):
            wiki_count += 1
            target, _, anchor = raw.split('|')[0].partition('#')
            resolved = resolve(target, path)
            if resolved is None:
                errors.append(rel + ': unresolved wiki link ' + raw)
            elif anchor:
                linked = bodies.get(resolved, without_code(load_note(resolved)[1]))
                if anchor.startswith('^'):
                    found = re.search(r'(?:^|\s)' + re.escape(anchor) + r'(?:\s|$)', linked)
                else:
                    headings = re.findall(r'^#{1,6}\s+(.+?)\s*#*$', linked, re.M)
                    found = unquote(anchor).casefold() in [h.casefold() for h in headings]
                if not found:
                    errors.append(rel + ': unresolved link anchor ' + raw)
        for match in re.finditer(r'!?\[[^\]\n]*\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+"[^"]*")?\s*\)', clean):
            target = match[1].strip('<>')
            parsed = urlparse(target)
            if parsed.scheme not in ('', 'file') or target.startswith('#'):
                continue
            local_count += 1
            candidate = Path(unquote(parsed.path))
            if not candidate.is_absolute():
                candidate = path.parent / candidate
            if not candidate.exists():
                errors.append(rel + ': missing local Markdown link ' + target)

    expected_next = 'PKOS-' + format(max((int(key.split('-')[1]) for key in cards), default=0) + 1, '06d')
    actual_next = None
    if card_index is not None:
        stored = json.loads(Path(card_index).read_text())
        actual_next = stored.get('next_id')
        if stored.get('cards') != cards:
            errors.append('card index differs from actual card IDs/paths/metadata')
        if not isinstance(actual_next, str) or not re.fullmatch(r'PKOS-\d{6}', actual_next) or actual_next < expected_next:
            errors.append('next_id is invalid or collides with an existing allocation')
    return {'pass': not errors, 'read_only': True, 'errors': errors, 'markdown_notes': len(files),
            'card_count': len(cards), 'unique_id_count': len(cards),
            'card_types': dict(collections.Counter(x['metadata']['type'] for x in cards.values())),
            'next_id': actual_next, 'minimum_next_id': expected_next,
            'wiki_links_checked': wiki_count, 'local_markdown_links_checked': local_count,
            'source_records_checked': len(source_rows), 'module_directories': directories,
            'historical_sealed_audit': 'separate_legacy_audit_not_rewritten'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, required=True)
    args, settings = load_paths(parser)
    try:
        root = required_path(parser, settings, 'system_data')
        result = validate(args.vault, settings.get('module_config'),
                          settings.get('card_index') or root / '卡片索引.json',
                          settings.get('source_index') or root / 'sources/index.json')
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        result = {'pass': False, 'read_only': True, 'errors': [str(exc)]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['pass'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
