"""Codex SessionStart/Stop adapter. Reads source metadata, never transcripts.

New/unclassified ownership is returned to the developing chat, which knows the
user's request. The hook never declares downloaded third-party code self-built.
"""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from maintenance import skill_lifecycle as lifecycle
from maintenance import skill_publication as publication
from maintenance import tool_sync as sync
from weekly.workflow import save_json, lock


def snapshot(c, data, cwd):
    rows = {}
    for entry in lifecycle.discover(data, cwd, all_repositories=False):
        try:
            digest, _ = lifecycle.fingerprint(entry)
            tool = lifecycle.owner(data, Path(entry))
            declared = {}
            if tool:
                specs = [tool.get('public_distribution'), *tool.get('public_distributions', {}).values()]
                for spec in specs:
                    if not spec:
                        continue
                    root = Path(spec['source_directory'])
                    for dest in spec['files']:
                        rel = spec.get('file_map', {}).get(dest, dest)
                        declared[str(root / rel)] = sync.digest(publication.release.regular(sync, root, rel))
            rows[entry] = sync.digest(lifecycle.json_text({'skill': digest, 'declared_sources': declared}).encode())
        except (OSError, ValueError, sync.Blocked):
            rows[entry] = 'unavailable'
    return rows


def handle(config_file, event):
    c = publication.configuration(config_file)
    session = event.get('session_id', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session):
        raise ValueError('invalid hook session ID')
    cwd = event.get('cwd')
    if not isinstance(cwd, str) or not Path(cwd).is_dir():
        raise ValueError('missing hook working directory')
    home = c['system_data'] / 'skill-lifecycle/sessions' / session
    with lock(home):
        data = sync.load_ledger(c['registry'])
        current = snapshot(c, data, cwd)
        baseline = home / 'BASELINE.json'
        if event.get('hook_event_name') == 'SessionStart':
            if not baseline.exists():
                save_json(baseline, {'entries': current, 'cwd': cwd})
            return {'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext':
                '自建 Skill 交付自动执行登记、建卡和公开版准备。发现/更新 Skill 后使用 pkos-system 的技能纳管参考，'
                '运行 skill_lifecycle.py finish；新自建来源用本任务真实请求引用声明，第三方用 external 分类。'
                '不要询问常规纳管权限，不要自行批准公开上传。'}}
        if event.get('hook_event_name') != 'Stop':
            return {}
        old = json.loads(baseline.read_text())['entries'] if baseline.exists() else {}
        state_file = c['system_data'] / 'skill-lifecycle/STATE.json'
        state = json.loads(state_file.read_text()) if state_file.exists() else {'skills': {}}
        changed, pending, finished = [], [], []
        for entry, digest in current.items():
            if digest == old.get(entry):
                continue
            if state['skills'].get(entry, {}).get('fingerprint') == digest:
                continue
            changed.append(entry)
            known = lifecycle.owner(data, Path(entry))
            if known:
                try:
                    documented = lifecycle.finish(config_file, entry,
                        reference='codex:' + session + ':' + str(event.get('turn_id', 'unknown')),
                        prepare_release=False)
                    finished.append(documented)
                    if documented.get('needs_semantic_card_review'):
                        pending.append({'entry': entry, 'reason': 'review_preserved_manual_card_against_changed_source'})
                except (OSError, ValueError, sync.Blocked) as exc:
                    pending.append({'entry': entry, 'reason': getattr(exc, 'reason', type(exc).__name__)})
            else:
                pending.append({'entry': entry, 'reason': 'classify_self_built_or_external_from_development_context'})
        result = None
        if finished:
            result = publication.reconcile(config_file, discover_skills=False)
        receipt = {'event': 'Stop', 'cwd': cwd, 'changed': changed, 'pending': pending,
                   'finished': finished, 'publication': result, 'transcripts_read': False,
                   'public_upload_performed': False}
        save_json(home / 'LAST_STOP.json', receipt)
        if not pending:
            save_json(baseline, {'entries': current, 'cwd': cwd})
        attention = pending or (result and result['state'] == 'attention_required')
        if attention and not event.get('stop_hook_active'):
            return {'decision': 'block', 'reason':
                '本次 Skill 交付还有待办，依据当前开发任务判断来源并完成，不要求用户重复授权常规纳管。'
                '只对确属自建的 Skill 执行 skill_lifecycle.py finish --path ... --self-built --reference <本任务真实请求引用>；'
                '第三方使用 external --reference <实际来源>，不能冒充自建。'
                '人工正文保留但源码变动时，据实际变更审阅正文并通过 --card-body 更新，不直接覆盖本人经验。'
                '公开配方缺失时继续可移植化、README、依赖和精确文件清单准备，实际公开上传才等本人确认。'
                '扫描命中/归属不明/目录或历史冲突按同步规则保留现场报告，不绕过。'
                '待办记录（仅位置与状态）：' + json.dumps({'pending': pending, 'publication': result}, ensure_ascii=False)}
        if attention:
            return {'systemMessage': 'Skill 纳管待办仍保留；避免重复续跑，详见本机 skill-lifecycle/sessions 中的 LAST_STOP.json。'}
        return {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config/publication.local.json')
    args = parser.parse_args()
    try:
        result = handle(args.config, json.load(sys.stdin))
    except Exception as exc:
        # Valid hook JSON even on errors; no secret-bearing exception text.
        result = {'systemMessage': 'Skill lifecycle hook待重试：' + type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
