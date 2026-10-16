#!/usr/bin/env python3
"""Weekly preparation, review rendering, monthly matching, and owner interviews."""
import argparse
from datetime import datetime
import json
from pathlib import Path
from weekly import workflow, interview

ROOT = Path(__file__).resolve().parents[1]


def settings(system_data=None, sources=None):
    local = ROOT/'config/paths.json'
    paths = json.loads(local.read_text()) if local.exists() else {}
    system = Path(system_data or paths['system_data']).expanduser().resolve()
    source_path = Path(sources or ROOT/'config/sources.local.json')
    if source_path.exists():
        config = json.loads(source_path.read_text())
    else:
        config = {'official_exports': [], 'web_database': str(ROOT/'data/pkos.sqlite3'),
                  'codex_roots': [str(Path.home()/'.codex/sessions'), str(system/'raw/codex/sessions')],
                  'pi_roots': [str(Path.home()/'.pi/agent/sessions')]}
    # Discover exports as unknown-coverage snapshots; filename dates do not prove completeness.
    if not config.get('official_exports'):
        config['official_exports'] = [{'path': str(path), 'generated_at': None, 'complete_confirmed': False}
                                      for path in sorted((system/'raw/chatgpt').glob('*.zip'))]
    return system, config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--system-data', type=Path)
    parser.add_argument('--sources', type=Path)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('prepare', 'render', 'interview-start', 'interview-next', 'interview-answer', 'interview-skip', 'interview-draft'):
        command = sub.add_parser(action)
        command.add_argument('--week', required=action != 'prepare')
        if action in ('render', 'interview-draft'):
            command.add_argument('--plan', type=Path, required=True)
        if action == 'interview-next':
            command.add_argument('--followup')
        if action == 'interview-answer':
            command.add_argument('--received-file', type=Path, required=True)
            command.add_argument('--cleaned-file', type=Path)
        if action == 'interview-skip':
            command.add_argument('--whole-week', action='store_true')
    monthly = sub.add_parser('monthly')
    monthly.add_argument('--month', required=True)
    args = parser.parse_args()
    system, config = settings(args.system_data, args.sources)
    if args.action == 'prepare':
        result = workflow.prepare(system, config, args.week)
    elif args.action == 'render':
        result = workflow.render(system, args.week, json.loads(args.plan.read_text()))
    elif args.action == 'monthly':
        result = workflow.monthly(system, config, args.month)
    elif args.action == 'interview-start':
        result = interview.start(system, args.week)
    elif args.action == 'interview-next':
        result = interview.next_question(system, args.week, args.followup)
    elif args.action == 'interview-answer':
        result = interview.answer(system, args.week, json.loads(args.received_file.read_text()),
                                  args.cleaned_file.read_text() if args.cleaned_file else None)
    elif args.action == 'interview-skip':
        result = interview.skip(system, args.week, args.whole_week)
    else:
        result = interview.draft_outputs(system, args.week, json.loads(args.plan.read_text()))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
