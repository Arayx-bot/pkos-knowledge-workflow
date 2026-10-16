"""Self-built Skill intake: canonical ownership, stable tool/card IDs, release jobs.

New ownership is supplied by the development chat, never inferred from an
installation directory. Registered ownership can be reused without asking again.
All source references and lifecycle journals remain in private system data.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import yaml
from maintenance import tool_sync as sync
from maintenance import skill_publication as publication
from validation.module_config import read_config
from validation.validate import load_note, normalized, validate
from weekly.workflow import lock, save_json, ZONE

SKIP = {'.git', '.venv', 'node_modules', '__pycache__', 'data', 'input', 'output', 'private', 'archive', 'backups'}


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def skill_info(path):
    path = Path(path).expanduser().resolve()
    entry = path if path.name == 'SKILL.md' else path / 'SKILL.md'
    if not entry.is_file():
        raise ValueError('Skill entry is missing')
    meta, body = load_note(entry)
    name, description = meta.get('name'), meta.get('description')
    if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', name):
        raise ValueError('invalid Skill name')
    if not isinstance(description, str) or not description.strip():
        raise ValueError('Skill description is missing')
    title = name
    interface = entry.parent / 'agents/openai.yaml'
    if interface.is_file():
        ui = yaml.safe_load(interface.read_text()) or {}
        details = ui.get('interface', {}) if isinstance(ui, dict) else {}
        title = details.get('display_name') or title if isinstance(details, dict) else title
    return {'entry': entry, 'directory': entry.parent, 'name': name,
            'description': description.strip(), 'title': title, 'metadata': meta}


def source_files(directory):
    """Bounded source inventory; never enumerate private runtime folders."""
    directory = Path(directory).resolve()
    paths = []
    for parent, dirs, names in os.walk(directory, followlinks=False):
        dirs[:] = [x for x in dirs if x not in SKIP and not x.startswith('.')]
        if len(Path(parent).relative_to(directory).parts) > 10:
            raise ValueError('Skill source depth exceeds discovery scope')
        paths.extend(Path(parent) / x for x in names)
        if len(paths) > 2000:
            raise ValueError('Skill source inventory exceeds bounded scope')
    result = {}
    for p in sorted(set(paths)):
        rel = p.relative_to(directory)
        if any(x in SKIP for x in rel.parts) or p.suffix in ('.pyc', '.db', '.sqlite3') or p.name.startswith('.') or p.name.endswith(('.local.json', '.local.zsh', '.key', '.pem')):
            continue
        if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(directory):
            raise ValueError('Skill source has an unsafe file or symlink')
        if p.stat().st_size > sync.MAX_FILE:
            raise ValueError('Skill source file exceeds size limit')
        result[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    if 'SKILL.md' not in result:
        raise ValueError('Skill entry is ignored or missing from source inventory')
    return result


def fingerprint(path):
    info = skill_info(path)
    files = source_files(info['directory'])
    return hashlib.sha256(json_text(files).encode()).hexdigest(), files


def owner(data, entry):
    resolved = str(Path(entry).resolve())
    matches = [t for t in data['tools'] if resolved in {str(Path(p).resolve()) for p in t.get('skill_entries', [])}]
    if len(matches) > 1:
        raise sync.Blocked('skill_ownership_ambiguous')
    return matches[0] if matches else None


def eligible_entries(data):
    for tool in data['tools']:
        for entry in tool.get('skill_entries', []):
            if tool.get('skill_entry_roles', {}).get(entry) == 'release_source':
                continue
            yield tool, Path(entry).resolve()


def discover(data, cwd=None, all_repositories=True):
    """Inspect declared roots and Git candidate filenames, never chat transcripts."""
    found = {str(p) for _, p in eligible_entries(data)}
    external = {str(Path(p).resolve()) for p in data.get('external_skill_entries', [])}
    excluded = {str(Path(p).resolve()) for t in data['tools'] for p, role in t.get('skill_entry_roles', {}).items() if role == 'release_source'}
    for value in data['skill_roots']:
        root = Path(value)
        if root.is_dir():
            for child in root.iterdir():
                if child.name.startswith('.'):
                    continue
                p = child / 'SKILL.md'
                if p.is_file():
                    found.add(str(p.resolve()))
    roots = {Path(r['root']).resolve() for r in data['repositories'].values()} if all_repositories else set()
    if cwd:
        p = Path(cwd).expanduser().resolve()
        probe = subprocess.run(['git', '-C', str(p), 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
        if probe.returncode == 0:
            roots.add(Path(probe.stdout.strip()).resolve())
        elif p.is_dir():
            visited = 0
            for parent, dirs, files in os.walk(p, followlinks=False):
                dirs[:] = [x for x in dirs if x not in SKIP and not x.startswith('.')]
                visited += 1
                if visited > 3000:
                    raise ValueError('non-Git discovery scope exceeds limit')
                if 'SKILL.md' in files:
                    found.add(str((Path(parent) / 'SKILL.md').resolve()))
    for root in roots:
        raw = sync.git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z', binary=True)
        for item in raw.split(b'\0'):
            if not item:
                continue
            rel = Path(os.fsdecode(item))
            if rel.name == 'SKILL.md' and not any(x in SKIP for x in rel.parts):
                p = root / rel
                if p.is_file():
                    found.add(str(p.resolve()))
    return sorted(found - external - excluded)


def ledger_text(path, data, added=None):
    text = Path(path).read_text()
    before, remainder = text.split(sync.START)
    _, after = remainder.split(sync.END)
    if added:
        table = ('| ' + added['id'] + ' / 新增 | ' + added['name'].replace('|', '/') + ' | Skill | '
                 + added['description'].replace('|', '/').replace('\n', ' ') + ' | 自动纳管，待同步／待公开确认 | '
                 + added['repository'] + ' |\n')
        at = before.index('\n## 本地位置与审查记录')
        before = before[:at].rstrip() + '\n' + table + '\n' + before[at:]
        before += ('\n### ' + added['id'] + ' · ' + added['name'] + '\n\n'
                   '- 自动登记：由自建Skill开发任务触发，保留来源引用，不推定第三方所有权。\n'
                   '- 唯一主位置：`' + added['primary_paths'][0] + '`。\n'
                   '- 归属仓库：`' + added['repository'] + '`；Skill：`' + added['skill_name'] + '`。\n'
                   '- 状态：已登记，源码待精确同步，公开版准备后待本人确认；未公开。\n\n')
    nxt = 'TOOL-' + format(max((int(t['id'].split('-')[1]) for t in data['tools']), default=0) + 1, '03d')
    before = re.sub(r'下一编号 TOOL-\d{3}', '下一编号 ' + nxt, before)
    before = re.sub(r'## 总览（\d+ 项）', '## 总览（' + str(len(data['tools'])) + ' 项）', before)
    return before + sync.START + '\n```json\n' + json.dumps(data, ensure_ascii=False, indent=2) + '\n```\n' + sync.END + after


def source_repository(data, directory):
    matches = [(key, Path(r['root']).resolve()) for key, r in data['repositories'].items()
               if directory.is_relative_to(Path(r['root']).resolve())]
    if not matches:
        return None
    return max(matches, key=lambda row: len(row[1].parts))


def canonicalize(c, data, info, self_built):
    directory = info['directory']
    belonging = source_repository(data, directory)
    if belonging:
        return info, belonging, None
    if not self_built or not c.get('general_skill_root') or not c.get('general_skill_repository'):
        raise sync.Blocked('new_skill_needs_explicit_self_built_origin')
    # A Git repository inside or outside the Skill is never silently moved or merged.
    probe = subprocess.run(['git', '-C', str(directory), 'rev-parse', '--show-toplevel'], capture_output=True)
    if probe.returncode == 0 or any(p.name == '.git' for p in directory.rglob('.git')):
        raise sync.Blocked('unmanaged_git_skill_requires_repository_mapping')
    target = Path(c['general_skill_root']).expanduser().resolve() / info['name']
    repo_key = c['general_skill_repository']
    repo_root = Path(data['repositories'][repo_key]['root']).resolve()
    if not target.is_relative_to(repo_root) or target.exists() or target.is_symlink():
        raise sync.Blocked('canonical_skill_destination_conflict')
    if directory.stat().st_dev != target.parent.stat().st_dev:
        raise sync.Blocked('canonical_skill_move_cross_filesystem')
    hashes = source_files(directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    directory.rename(target)
    directory.symlink_to(target, target_is_directory=True)
    try:
        if source_files(target) != hashes:
            raise ValueError('canonical Skill move integrity mismatch')
    except Exception:
        directory.unlink(); target.rename(directory)
        raise
    return skill_info(target), (repo_key, repo_root), {'path': str(directory), 'target': str(target)}


def allow_source_files(writes, repo_root, directory, files):
    """Extend an existing deny-by-default ignore file using exact owned paths."""
    ignore = repo_root / '.gitignore'
    if not ignore.is_file():
        return
    additions = []
    for rel in files:
        path = directory / rel
        checked = subprocess.run(['git', '-C', str(repo_root), 'check-ignore', '-q', '--', str(path.relative_to(repo_root))])
        if checked.returncode == 0:
            parts = path.relative_to(repo_root).parts
            additions += ['!/' + '/'.join(parts[:i]) + '/' for i in range(1, len(parts))]
            additions.append('!/' + '/'.join(parts))
    if additions:
        base = writes.get(ignore, ignore.read_text())
        escaped = ['!' + ''.join('\\' + char if char in '*?[]!#' else char for char in value[1:]) for value in additions]
        lines = [x for x in dict.fromkeys(escaped) if x not in base.splitlines()]
        if lines:
            writes[ignore] = base.rstrip() + '\n\n# Exact self-built Skill intake sources.\n' + '\n'.join(lines) + '\n'


def render_card(info, invocation, public_ready=False):
    return ('# ' + info['title'] + '\n\nSkill · 对 AI 说“' + invocation + '”。\n\n'
            '## 它是什么\n\n' + info['description'] + '\n\n'
            '## 为什么做\n\n作为自建工具统一维护源码与调用入口，方便在后续任务中复用。具体创建经历以本人补充为准。\n\n'
            '## 拿去用\n\n公开版等待文件与产物清单确认；尚无公开下载入口。\n\n'
            '## 怎么用\n\n### 一句话用法\n\n' + invocation + '\n\n'
            '### 对 Codex 说\n\n“' + invocation + '”\n\n'
            '## 源码\n\n主源码与依赖按本卡私人元数据定位；公开入口在实际发行核实后补充。\n\n'
            '## 现在怎样\n\n已纳入本机维护。使用与测试范围以实际记录为准，公开版准备不代表业务效果已验证。\n')


def _finish(config_file, path, self_built=False, reference=None, title=None, invocation=None,
           public_files=None, body_file=None, prepare_release=True):
    c = publication.configuration(config_file)
    home = c['system_data'] / 'skill-lifecycle'
    with lock(home), sync.ledger_lock(c['registry']), lock(c['system_data'] / 'vault-writer'):
        data = sync.load_ledger(c['registry']); info = skill_info(path)
        registered = owner(data, info['entry'])
        external = {str(Path(p).resolve()) for p in data.get('external_skill_entries', [])}
        if str(info['entry']) in external:
            raise sync.Blocked('external_skill_not_self_built')
        if not registered and (not self_built or not isinstance(reference, str) or not reference.strip()):
            raise sync.Blocked('new_skill_needs_explicit_self_built_origin')
        moved = None
        try:
            info, belonging, moved = canonicalize(c, data, info, bool(self_built or registered))
            repo_key, repo_root = belonging
            title = title or info['title']
            if not isinstance(title, str) or not title.strip() or any(x in title for x in '/\\\n\r[]#|:'):
                raise ValueError('unsafe tool card title')
            info['title'] = title
            observed, files = fingerprint(info['entry'])
            state_path = home / 'STATE.json'
            state = json.loads(state_path.read_text()) if state_path.exists() else {'schema': 1, 'skills': {}}
            prior = state['skills'].get(str(info['entry']), {})
            index = json.loads(c['card_index'].read_text()); sources = json.loads(c['source_index'].read_text())
            tool = registered
            if tool is None:
                ident = 'TOOL-' + format(max((int(t['id'].split('-')[1]) for t in data['tools']), default=0) + 1, '03d')
                tool = {'id': ident, 'name': title, 'description': info['description'], 'repository': repo_key,
                        'primary_paths': [str(info['directory'])], 'skill_entries': [str(info['entry'])],
                        'skill_name': info['name'], 'links': [moved] if moved else [], 'status': 'pending_sync',
                        'ownership': {'kind': 'self_built', 'reference': reference}}
                data['tools'].append(tool)
            elif str(info['entry']) not in tool['skill_entries']:
                raise sync.Blocked('registered_source_entry_changed')
            if moved and moved not in tool.get('links', []):
                tool.setdefault('links', []).append(moved)
            matches = [(ident, row) for ident, row in index['cards'].items()
                       if row['metadata'].get('type') == 'tool' and row['metadata'].get('skill_path') and Path(row['metadata']['skill_path']).resolve() == info['entry']]
            # Existing custom tool cards can bind by registered tool and exact Skill name.
            if not matches:
                matches = [(ident, row) for ident, row in index['cards'].items()
                           if row['metadata'].get('type') == 'tool' and row['metadata'].get('tool_registry_id') == tool['id'] and row['metadata'].get('skill_name') == info['name']]
            # Shared component cards are not duplicate canonical Skill cards. Prefer default variant.
            primary_matches = [(i, row) for i, row in matches if not row['metadata'].get('public_distribution_key')]
            if primary_matches:
                matches = primary_matches
            if len(matches) > 1:
                raise sync.Blocked('multiple_canonical_skill_cards')
            if prior.get('fingerprint') == observed and matches and not public_files and not body_file:
                return {'state': 'unchanged', 'tool': tool['id'], 'card': matches[0][0], 'source': str(info['entry']),
                        'needs_semantic_card_review': bool(prior.get('card_review_needed'))}
            _, modules = read_config(c['module_config'])
            folder = c.get('internal_card_folders', [])[0] if c.get('internal_card_folders') else None
            if not folder:
                raise ValueError('configure the existing internal card folder')
            parent = (c['vault'] / folder).resolve()
            if not parent.is_relative_to(c['vault'] / modules['skills']) or not parent.is_dir():
                raise ValueError('internal card folder is missing or in the wrong module')
            date = datetime.now(ZONE).date().isoformat()
            writes = {}; new_card = not matches
            if matches:
                card_id, row = matches[0]; card = c['vault'] / row['path']; meta, old_body = load_note(card)
            else:
                card_id = index['next_id']; index['next_id'] = 'PKOS-' + format(int(card_id.split('-')[1]) + 1, '06d')
                card = parent / (title + '.md')
                if card.exists() or any(Path(row['path']).stem == title for row in index['cards'].values()):
                    card = parent / (title + '-' + tool['id'] + '.md'); info['title'] = card.stem
                if card.exists():
                    raise ValueError('new card destination exists')
                meta = {'id': card_id, 'type': 'tool', 'subtype': 'internal', 'schema_version': '1.2',
                        'created': date, 'status': 'draft', 'visibility': 'private', 'source_refs': []}
                old_body = ''
            invocation = invocation or meta.get('invocation_phrase') or ('使用 ' + info['name'] + '，先检查环境并按 Skill 说明执行。')
            generated = render_card(info, invocation)
            if body_file:
                replacement = Path(body_file).read_text()
                if re.search(r'^# ', replacement, re.M):
                    raise ValueError('card body override must exclude the top-level title')
                body = '# ' + card.stem + '\n\n' + publication.ensure_takeaway(replacement)
            elif new_card or prior.get('generated_body_sha256') == hashlib.sha256(old_body.strip().encode()).hexdigest():
                body = generated
            else:
                body = publication.ensure_takeaway(old_body)
            reference = reference or ('registered-owner:' + tool['id'])
            source_id = 'SRC-SKILL-' + tool['id'] + '-' + observed[:16]
            source = c['source_index'].parent / (source_id + '.md')
            source_text = ('# 自建 Skill 文档依据\n\n' + json_text({'tool': tool['id'], 'entry': str(info['entry']),
                'source_fingerprint': observed, 'definition_name': info['name'], 'definition_description': info['description'],
                'reference': reference, 'observed_date': date, 'scope': 'source_definition_not_business_validation'}) + '\n')
            if source_id not in sources['sources']:
                writes[source] = source_text
                sources['sources'][source_id] = {'file': source.name, 'sha256': hashlib.sha256(source_text.encode()).hexdigest(),
                                                'kind': 'self_built_skill_definition', 'observed_date': date}
            meta.update(skill_name=info['name'], skill_path=str(info['entry']), tool_registry_id=tool['id'],
                        skill_source_fingerprint=observed, skill_latest_description=info['description'],
                        invocation_phrase=invocation, updated=date, documentation_checked_on=date)
            meta.setdefault('visibility', 'private')
            meta.setdefault('subtype', 'internal')
            meta.setdefault('tool_form', 'Skill')
            meta['source_refs'] = list(dict.fromkeys(meta.get('source_refs', []) + [source_id]))
            if data['repositories'][repo_key].get('ssh_url'):
                # Derive only the registered repository identity, never a fabricated public URL.
                url = data['repositories'][repo_key]['ssh_url']
                match = re.fullmatch(r'git' + '@' + r'github\.com:([^\s]+)\.git', url)
                if match:
                    relative = info['directory'].relative_to(repo_root).as_posix()
                    meta.update(repo_url='https://github.com/' + match[1], repo_visibility='private',
                                source_repo_path=relative, source_url='https://github.com/' + match[1] + '/tree/main/' + relative)
            writes[card] = '---\n' + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + '---\n\n' + body.strip() + '\n'
            index['cards'][card_id] = {'path': str(card.relative_to(c['vault'])), 'metadata': normalized(meta)}
            if new_card:
                overview = c['vault'] / modules['skills'] / c.get('skills_overview_filename', '技能总览.md')
                navigation = overview.read_text()
                link = '- [[' + card.stem + ']]'
                heading = c.get('internal_overview_heading', '## 自创工具')
                if heading in navigation:
                    navigation = navigation.replace(heading, heading + '\n\n' + link, 1)
                else:
                    navigation = navigation.rstrip() + '\n\n' + link + '\n'
                writes[overview] = navigation
            proposal = {'tool': tool['id'], 'source': str(info['directory']), 'source_fingerprint': observed,
                        'files': [{'path': p, 'sha256': h} for p, h in files.items()],
                        'state': 'needs_portable_public_recipe', 'owner_reference': reference}
            allowed_root, allowed_files = info['directory'], [p for p in ('SKILL.md', 'README.md', 'LICENSE') if p in files]
            if public_files is not None:
                recipe = public_files if isinstance(public_files, dict) else {'files': public_files}
                selected = recipe.get('files')
                mapping = recipe.get('file_map', {})
                package_root = Path(recipe.get('source_directory', info['directory'])).expanduser().resolve()
                if not package_root.is_relative_to(repo_root) or not isinstance(selected, list) or not isinstance(mapping, dict):
                    raise ValueError('public recipe must stay within the registered source repository')
                required = {'SKILL.md', 'README.md', 'LICENSE', 'distribution/workbuddy.json'}
                if not required <= set(selected) or set(mapping) - set(selected):
                    raise ValueError('portable public recipe requires Skill, README, license and adapter metadata')
                from maintenance import public_release
                material = {dest: public_release.regular(sync, package_root, mapping.get(dest, dest)) for dest in selected}
                if (package_root / mapping.get('SKILL.md', 'SKILL.md')).resolve() != info['entry']:
                    raise ValueError('public recipe must export this exact Skill entry')
                if not material['LICENSE'].startswith(b'MIT License\n'):
                    raise ValueError('MIT candidate license must already exist; do not replace another license')
                adapter = json.loads(material['distribution/workbuddy.json'])
                if str(package_root) not in tool['primary_paths']:
                    tool['primary_paths'].append(str(package_root))
                target_name = recipe.get('repository', info['name'])
                current = tool.get('public_distribution')
                if current and Path(current['source_directory']).resolve() != package_root:
                    raise sync.Blocked('public_recipe_source_differs_from_registered_primary')
                if not current:
                    if target_name in data['repositories'] or any(
                            x.get('public_distribution', {}).get('repository') == target_name for x in data['tools'] if x is not tool):
                        raise sync.Blocked('public_repository_name_collision')
                    current = {'repository': target_name, 'license': 'MIT', 'source_directory': str(package_root),
                        'workbuddy_metadata': 'distribution/workbuddy.json', 'status': 'preparation_configured',
                        'license_confirmation': 'proposed_MIT_awaiting_owner_release_confirmation'}
                    tool['public_distribution'] = current
                current.update(files=sorted(set(selected)), file_map=mapping, version=adapter['version'])
                proposal['state'] = 'public_recipe_configured'
                allowed_root, allowed_files = package_root, [mapping.get(p, p) for p in selected]
            elif tool.get('public_distribution'):
                proposal['state'] = 'existing_public_recipe'
            allow_source_files(writes, repo_root, allowed_root, allowed_files)
            writes[home / 'proposals' / (tool['id'] + '-' + observed[:16] + '.json')] = json_text(proposal)
            state['skills'][str(info['entry'])] = {'fingerprint': observed, 'tool': tool['id'], 'card': card_id,
                'state': proposal['state'], 'card_review_needed': bool(not body_file and not new_card and body != generated),
                'generated_body_sha256': hashlib.sha256(body.strip().encode()).hexdigest() if body == generated else prior.get('generated_body_sha256')}
            writes[state_path] = json_text(state)
            writes[c['registry']] = ledger_text(c['registry'], data, tool if registered is None else None)
            writes[c['card_index']] = json_text(index); writes[c['source_index']] = json_text(sources)
            event_id = 'skill-' + uuid.uuid4().hex
            publication.enqueue(writes, c['system_data'], event_id, [card_id])
            def checked(settings):
                result = validate(settings['vault'], settings['module_config'], settings['card_index'], settings['source_index'])
                if not result['pass']:
                    raise ValueError('Skill card transaction failed validation')
                return result
            from weekly.automatic import transact
            try:
                snapshot = transact(c, event_id, writes, checked)
            except Exception:
                if moved and Path(moved['path']).is_symlink() and Path(moved['path']).resolve() == info['directory']:
                    Path(moved['path']).unlink(); info['directory'].rename(moved['path'])
                raise
        except Exception:
            if moved and Path(moved['path']).is_symlink() and Path(moved['path']).resolve() == info['directory']:
                Path(moved['path']).unlink(); info['directory'].rename(moved['path'])
            raise
    prepared = publication.reconcile(config_file, discover_skills=False) if prepare_release else {'state': 'deferred_to_parent'}
    return {'state': 'registered_and_documented', 'tool': tool['id'], 'card': card_id,
            'new_tool': registered is None, 'new_card': new_card, 'source': str(info['entry']),
            'canonical_move': moved, 'snapshot': str(snapshot), 'publication': prepared,
            'needs_semantic_card_review': state['skills'][str(info['entry'])]['card_review_needed'],
            'public_upload_performed': False}



def finish(config_file, path, self_built=False, reference=None, title=None, invocation=None,
           public_files=None, body_file=None, prepare_release=True):
    result = _finish(config_file, path, self_built, reference, title, invocation,
                     public_files, body_file, prepare_release=False)
    if prepare_release:
        # Recheck declared sibling implementations even when Skill.md itself is unchanged.
        result['publication'] = publication.reconcile(config_file, discover_skills=False)
    result['public_upload_performed'] = False
    return result


def reconcile(config_file, cwd=None):
    """Bootstrap known ownership and persist unknown entries for their development chat."""
    c = publication.configuration(config_file); data = sync.load_ledger(c['registry'])
    home = c['system_data'] / 'skill-lifecycle'
    prior_path = home / 'DISCOVERY.json'
    prior = json.loads(prior_path.read_text()) if prior_path.exists() else {'entries': {}}
    rows, updates, notices = {}, [], []
    discovered = set(discover(data, cwd))
    # Pending events from other Codex windows preserve explicit paths without reading chats.
    for event in (home / 'sessions').glob('*/LAST_STOP.json'):
        record = json.loads(event.read_text())
        for item in record.get('pending', []):
            candidate = Path(item['entry'])
            if candidate.is_file():
                discovered.add(str(candidate.resolve()))
    for value in sorted(discovered):
        try:
            info = skill_info(value); digest, files = fingerprint(value); tool = owner(data, info['entry'])
            if tool:
                result = finish(config_file, value, reference='registered-owner:' + tool['id'], prepare_release=False)
                if result['state'] != 'unchanged':
                    updates.append(result)
                row = {'fingerprint': digest, 'state': 'owned', 'tool': tool['id'],
                       'card_review_needed': bool(result.get('needs_semantic_card_review'))}
            else:
                row = {'fingerprint': digest, 'state': 'needs_development_origin', 'entry': value}
        except (ValueError, OSError, sync.Blocked) as exc:
            row = {'state': 'blocked', 'reason': getattr(exc, 'reason', type(exc).__name__), 'entry': value}
        rows[value] = row
        if row != prior['entries'].get(value) and row['state'] != 'owned':
            notices.append(row)
    current = {'schema': 1, 'entries': rows}
    if current != prior:
        save_json(prior_path, current)
    return {'state': 'attention_required' if notices else ('updated' if updates else 'unchanged'),
            'skills_updated': updates, 'origin_pending': notices,
            'unclassified_skills': [row for row in rows.values() if row['state'] == 'needs_development_origin'],
            'pending_card_reviews': [{'entry': entry, 'tool': row['tool']} for entry, row in rows.items()
                                     if row.get('card_review_needed')],
            'transcripts_read': False,
            'whole_disk_scanned': False, 'public_upload_performed': False}


def mark_external(config_file, path, reference):
    c = publication.configuration(config_file); entry = skill_info(path)['entry']
    with sync.ledger_lock(c['registry']):
        data = sync.load_ledger(c['registry'])
        if owner(data, entry) or not reference:
            raise ValueError('external classification requires context and cannot overwrite owned entry')
        value = str(entry)
        if value not in data['external_skill_entries']:
            data['external_skill_entries'].append(value)
            data.setdefault('external_skill_origins', {})[value] = reference
            sync.save_ledger(c['registry'], data)
    return {'state': 'external_classified', 'entry': value, 'tool_created': False, 'card_created': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config/publication.local.json')
    sub = parser.add_subparsers(dest='command', required=True)
    q = sub.add_parser('finish'); q.add_argument('--path', required=True); q.add_argument('--self-built', action='store_true')
    q.add_argument('--reference'); q.add_argument('--title'); q.add_argument('--invocation'); q.add_argument('--public-files', type=Path); q.add_argument('--card-body', type=Path)
    q = sub.add_parser('scan'); q.add_argument('--cwd')
    q = sub.add_parser('external'); q.add_argument('--path', required=True); q.add_argument('--reference', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'finish':
            files = json.loads(args.public_files.read_text()) if args.public_files else None
            result = finish(args.config, args.path, args.self_built, args.reference, args.title, args.invocation, files, args.card_body)
        elif args.command == 'external':
            result = mark_external(args.config, args.path, args.reference)
        else:
            result = reconcile(args.config, args.cwd)
        print(json_text(result), end='')
        return 2 if result['state'] == 'attention_required' else 0
    except (ValueError, OSError, KeyError, sync.Blocked) as exc:
        print(json_text({'state': 'blocked', 'reason': getattr(exc, 'reason', type(exc).__name__)}), end='')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
