"""Deterministic weekly workspace, provenance validation, and review rendering."""
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid
from zoneinfo import ZoneInfo
from weekly.sources import gather, timestamp, reconcile

ROOT = Path(__file__).resolve().parents[1]
ZONE = ZoneInfo('Asia/Shanghai')


def week_bounds(week=None, now=None):
    now = now or datetime.now(ZONE)
    if week is None:
        monday = now.astimezone(ZONE).date() - timedelta(days=now.astimezone(ZONE).weekday()+7)
    else:
        match = re.fullmatch(r'(\d{4})-W(\d{2})', week)
        if not match:
            raise ValueError('invalid ISO week')
        monday = date.fromisocalendar(int(match[1]), int(match[2]), 1)
    start = datetime.combine(monday, time(), ZONE)
    end = start + timedelta(days=7)
    if end > now:
        raise ValueError('target week is not complete')
    year, number, _ = monday.isocalendar()
    return f'{year}-W{number:02}', start, end


def private_write(path, value, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if exclusive:
        with path.open('x', encoding='utf-8') as handle:
            os.chmod(path, 0o600); handle.write(value)
    else:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix='.'+path.name, delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600); handle.write(value)
        os.replace(temporary, path)



def save_json(path, value, exclusive=False):
    private_write(path, json.dumps(value, ensure_ascii=False, indent=2)+'\n', exclusive)


@contextmanager
def lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / '.lock'
    # Never remove another run's lock automatically: a crash is an explicit stop.
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, str(os.getpid()).encode()); os.close(fd)
        yield
    finally:
        path.unlink()


def outside_repo(system_data):
    system_data = Path(system_data).expanduser().resolve()
    if any((p/'.obsidian').is_dir() for p in [system_data, *system_data.parents]):
        raise ValueError('private outputs must be outside Obsidian Vault')
    if system_data == ROOT or ROOT in system_data.parents:
        raise ValueError('private outputs must be outside source repository')
    return system_data


def prepare(system_data, config, week=None, now=None):
    system_data = outside_repo(system_data)
    week, start, end = week_bounds(week, now)
    # No bypass option in production. Tests replace the subprocess at its boundary.
    checked = subprocess.run(['python3', '-B', str(ROOT/'maintenance/tool_sync.py'), 'check'], capture_output=True, text=True)
    if checked.returncode:
        raise ValueError('tool consistency check failed; report before extraction')
    directory = system_data / 'phase-05-a/weekly' / week
    with lock(directory):
        state = directory/'RUN.json'
        if state.exists():
            previous = json.loads(state.read_text())
            return {'state': previous['state'], 'week': week, 'directory': str(directory), 'already_exists': True}
        started = (now or datetime.now(ZONE)).isoformat()
        records, coverage = gather(config, start, end)
        official = config.get('official_exports', [])
        valid = [item for item in official if item.get('complete_confirmed') is True and
                 timestamp(item.get('generated_at')) and timestamp(item['generated_at']) >= end]
        covered = bool(valid) and not coverage['A']['errors']
        current = (now or datetime.now(ZONE)).astimezone(ZONE)
        prior_month = (current.date().replace(day=1)-timedelta(days=1)).strftime('%Y-%m')
        monthly_reports = system_data/'phase-05-a/reconciliation'/prior_month
        monthly_done = any(json.loads(p.read_text()).get('official_period_covered') is True
                           for p in monthly_reports.glob('*.json'))
        coverage.update(monthly_export_reminder=None if monthly_done else f'{prior_month}官方导出核对待完成；请提供覆盖该月的官方包。',
                        official_week_covered=covered, provisional=not covered,
                        export_reminder=None if covered else '请提供覆盖目标周的官方导出；本轮仅生成标明来源缺口的暂定预览。',
                        start=start.isoformat(), end=end.isoformat(), timezone='Asia/Shanghai')
        private_write(directory/'EVIDENCE.jsonl', ''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in records), True)
        save_json(directory/'SOURCE_COVERAGE.json', coverage, True)
        save_json(state, {'week': week, 'state': 'awaiting_editor', 'started_at': started,
                          'run_id': str(uuid.uuid4()), 'provisional': not covered,
                          'source_errors': any(v.get('errors') for v in coverage.values() if isinstance(v, dict))}, True)
    return {'state': 'awaiting_editor', 'week': week, 'directory': str(directory), 'provisional': not covered,
            'records': len(records), 'errors': {k: v['errors'] for k, v in coverage.items() if isinstance(v, dict) and v.get('errors')}}


def cell(value):
    return str(value or '').replace('|', '\\|').replace('\n', '<br>')


def validate_plan(plan, evidence):
    by_id = {r['evidence_id']: r for r in evidence}
    candidates = plan.get('candidates', [])
    questions = plan.get('interview_questions', [])
    if not 3 <= len(questions) <= 5:
        raise ValueError('require 3-5 concrete interview questions; insufficient evidence must be reported, never padded')
    ids = set()
    for item in candidates + questions:
        if not isinstance(item.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]+', item['id']) or item['id'] in ids:
            raise ValueError('invalid or duplicate preview/question ID')
        ids.add(item['id'])
        refs = item.get('evidence_ids', [])
        if not refs or any(ref not in by_id or not by_id[ref].get('week_eligible') for ref in refs):
            raise ValueError('missing or ineligible evidence reference')
    for item in candidates:
        if item.get('audience') not in (None, 'self'):
            raise ValueError('unsupported preview audience')
        if item.get('type') not in ('thought', 'knowledge', 'project', 'tool', 'weekly'):
            raise ValueError('unsupported candidate type')
        if item.get('action') not in ('新建', '更新', '忽略', '待答'):
            raise ValueError('unsupported action')
        if len(item.get('reason', '')) > 20 or not item.get('title'):
            raise ValueError('invalid title or reason')
        if item['action'] == '待答' and item.get('body'):
            raise ValueError('unresolved meaning must not receive a body')
        if item['action'] in ('新建', '更新') and not item.get('body'):
            raise ValueError('approved-quality candidates require body previews')
        if item['action'] == '更新' and not re.fullmatch(r'PKOS-\d{6}', item.get('existing_id', '')):
            raise ValueError('update requires existing stable ID')
        if item.get('attribution') == 'owner_view' and not any(by_id[r]['role'] == 'user' for r in item['evidence_ids']):
            raise ValueError('AI-only evidence cannot establish an owner viewpoint')
    for question in questions:
        if not question.get('question') or not question.get('event_anchor'):
            raise ValueError('interview question requires a concrete event anchor')
    return by_id


def render(system_data, week, plan):
    if not re.fullmatch(r'\d{4}-W\d{2}', week):
        raise ValueError('invalid ISO week')
    directory = outside_repo(system_data)/'phase-05-a/weekly'/week
    with lock(directory):
        run = json.loads((directory/'RUN.json').read_text())
        if run['state'] != 'awaiting_editor' or (directory/'REVIEW.md').exists():
            raise ValueError('review already rendered; preserve approval snapshot')
        if run.get('source_errors') and not plan.get('source_limitations'):
            raise ValueError('source parse errors require explicit limitations in review')
        evidence = [json.loads(line) for line in (directory/'EVIDENCE.jsonl').read_text().splitlines()]
        by_id = validate_plan(plan, evidence)
        rows = ['# '+week+' 审阅表', '', 'visibility: private', '',
                '时间状态：'+('暂定；网页证据按采集时间，等待月度导出核对。' if run['provisional'] else '官方导出覆盖整周。'), '',
                '来源限制：'+plan.get('source_limitations', '见 SOURCE_COVERAGE.json。'), '',
                '| 编号 | 标题 | 类型 | 建议动作 | 一句话理由（20字以内） | 需要本人回答的问题 |',
                '|---|---|---|---|---|---|']
        for item in plan['candidates']:
            name = item['id']+'.md'
            title = '['+cell(item['title'])+'](previews/'+name+')' if item.get('body') else cell(item['title'])
            rows.append('| '+' | '.join([cell(item['id']), title, cell(item['type']), cell(item['action']), cell(item['reason']), cell(item.get('question'))])+' |')
            if item.get('body'):
                audience = 'self' if 'kufucat' in item['title'].lower() else item.get('audience')
                header = f'---\nid: PREVIEW-{item["id"]}\ntype: {item["type"]}\nvisibility: private\n'
                if audience:
                    header += 'audience: '+audience+'\n'
                header += '---\n\n# '+item['title']+'\n\n'
                private_write(directory/'previews'/name, header+item['body']+'\n', True)
        private_write(directory/'REVIEW.md', '\n'.join(rows)+'\n', True)
        appendix = ['# 证据附录', '']
        used = {ref for item in plan['candidates']+plan['interview_questions'] for ref in item['evidence_ids']}
        for identifier in sorted(used):
            row = by_id[identifier]
            appendix += [f'## {identifier}', f'- 来源：{row["source"]}；角色：{row["role"]}；归属：{row["attribution"]}',
                         f'- 定位：{row["path"]}；会话 {row["session_id"]}；消息 {row["message_id"] or "未知"}；行 {row["line"]}',
                         f'- 时间：{row["timestamp"]}；依据：{ "按采集时间" if row["time_basis"] == "captured_at" else row["time_basis"] }', '']
        private_write(directory/'EVIDENCE_APPENDIX.md', '\n'.join(appendix)+'\n', True)
        save_json(directory/'INTERVIEW_QUESTIONS.json', plan['interview_questions'], True)
        private_write(directory/'INTERVIEW_QUESTIONS.md', '# 访谈问题\n\n'+'\n\n'.join(q['id']+'：'+q['question'] for q in plan['interview_questions'])+'\n', True)
        finished = datetime.now(ZONE)
        seconds = (finished-datetime.fromisoformat(run['started_at'])).total_seconds()
        private_write(directory/'STATS.md', f'# {week} 统计\n\n开始：{run["started_at"]}\n\n结束：{finished.isoformat()}\n\n实际历时：{seconds:.1f} 秒（含阅读、编辑与验证，不含等待本人审阅）\n\n候选：{len(plan["candidates"])}；访谈问题：{len(plan["interview_questions"])}\n\n超过30分钟：{seconds > 1800}\n\n覆盖情况见 SOURCE_COVERAGE.json。\n', True)
        run.update(state='awaiting_owner_review', finished_at=finished.isoformat())
        save_json(directory/'RUN.json', run)
    return {'state': run['state'], 'directory': str(directory)}


def monthly(system_data, config, month):
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month):
        raise ValueError('invalid month')
    findings = reconcile(config)
    # Keep only pairs where one timestamp belongs to the requested Shanghai month.
    findings = [r for r in findings if any(timestamp(r.get(field)) and
                timestamp(r[field]).astimezone(ZONE).strftime('%Y-%m') == month
                for field in ('captured_at', 'official_time'))]
    directory = outside_repo(system_data)/'phase-05-a/reconciliation'/month
    with lock(directory):
        output = directory/(datetime.now(ZONE).strftime('%Y%m%d-%H%M%S')+'.json')
        year, number = map(int, month.split('-'))
        end = datetime(year+1 if number == 12 else year, 1 if number == 12 else number+1, 1, tzinfo=ZONE)
        covered = any(item.get('complete_confirmed') is True and timestamp(item.get('generated_at')) and
                      timestamp(item['generated_at']) >= end for item in config.get('official_exports', []))
        save_json(output, {'month': month, 'findings': findings, 'formal_changes': False,
                           'official_period_covered': covered,
                           'coverage_note': 'covered' if covered else 'provisional: official snapshot does not prove full month coverage',
                           'official_export_available': bool(config.get('official_exports'))}, True)
    return {'report': str(output), 'pairs': len(findings), 'formal_changes': False}
