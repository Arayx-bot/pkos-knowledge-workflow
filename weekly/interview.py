"""One-question interview journal with immutable received-text events."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
from weekly.workflow import ZONE, lock, outside_repo, private_write, save_json


def directory_for(system_data, week):
    if not re.fullmatch(r'\d{4}-W\d{2}', week):
        raise ValueError('invalid ISO week')
    return outside_repo(system_data)/'interviews'/week


def append_raw(directory, value):
    value = {**value, 'recorded_at': datetime.now(ZONE).isoformat()}
    if (directory/'raw.jsonl').is_symlink():
        raise ValueError('raw archive must not be a symlink')
    fd = os.open(directory/'raw.jsonl', os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(value, ensure_ascii=False)+'\n'); handle.flush(); os.fsync(handle.fileno())


def start(system_data, week):
    directory = directory_for(system_data, week)
    with lock(directory):
        state_file = directory/'session.json'
        if state_file.exists():
            return json.loads(state_file.read_text())
        questions_path = outside_repo(system_data)/'phase-05-a/weekly'/week/'INTERVIEW_QUESTIONS.json'
        questions = json.loads(questions_path.read_text())
        state = {'week': week, 'state': 'in_progress', 'questions': questions,
                 'index': 0, 'pending': None, 'followed': [], 'answered': [], 'skipped': []}
        save_json(state_file, state, True)
    return {'state': state['state'], 'questions': len(questions)}


def next_question(system_data, week, followup=None):
    directory = directory_for(system_data, week)
    with lock(directory):
        state = json.loads((directory/'session.json').read_text())
        if state['state'] != 'in_progress':
            return {'state': state['state'], 'question': None}
        if state['pending']:
            return {'state': 'awaiting_owner_answer', **state['pending']}
        if followup:
            if not state['answered']:
                raise ValueError('follow-up requires a real prior answer')
            identifier = state['answered'][-1]
            if identifier in state['followed'] or identifier.endswith('-followup'):
                raise ValueError('at most one follow-up per primary question')
            state['followed'].append(identifier)
            question = {'id': identifier+'-followup', 'question': followup}
        elif state['index'] < len(state['questions']):
            question = state['questions'][state['index']]
            state['index'] += 1
        else:
            state['state'] = 'completed'
            save_json(directory/'session.json', state)
            return {'state': 'completed', 'question': None}
        state['pending'] = question
        append_raw(directory, {'role': 'assistant', 'question_id': question['id'], 'text_unchanged': question['question']})
        save_json(directory/'session.json', state)
        return {'state': 'awaiting_owner_answer', **question}


def answer(system_data, week, received, cleaned=None):
    if received.get('role') != 'user' or received.get('source_kind') not in ('voice_transcript', 'user_message'):
        raise ValueError('only a received owner message can be archived as an answer')
    if not isinstance(received.get('text_unchanged'), str) or not received['text_unchanged'].strip():
        raise ValueError('empty owner answer')
    if not received.get('source_message_id'):
        raise ValueError('record actual source message ID or literal unknown; never fabricate')
    if cleaned is not None and normalize(cleaned) != normalize(received['text_unchanged']):
        raise ValueError('cleaning must only change punctuation, whitespace and filler words')
    directory = directory_for(system_data, week)
    with lock(directory):
        state = json.loads((directory/'session.json').read_text())
        if state['state'] != 'in_progress' or not state['pending']:
            raise ValueError('no pending question')
        qid = state['pending']['id']
        raw_path = directory/'raw.jsonl'
        prior = [json.loads(line) for line in raw_path.read_text().splitlines()] if raw_path.exists() else []
        prior_answer = next((r for r in reversed(prior) if r.get('role') == 'user' and r.get('question_id') == qid), None)
        if prior_answer:
            if prior_answer['text_unchanged'] != received['text_unchanged']:
                raise ValueError('answer already appended before interruption; preserve raw and report')
        else:
            append_raw(directory, {**received, 'question_id': qid, 'event_week': week})
        if cleaned is not None:
            path = directory/'cleaned.md'
            existing = path.read_text() if path.exists() else '# 整理稿\n\nvisibility: private\n'
            private_write(path, existing+'\n## '+qid+'\n\n'+cleaned+'\n')
        state['answered'].append(qid); state['pending'] = None
        save_json(directory/'session.json', state)
    return {'state': 'answer_archived', 'question_id': qid}


def normalize(text):
    # A conservative mechanical guard; the Skill must also check whether a filler has meaning.
    text = re.sub(r'嗯+|呃+|额+|那个|就是说|就是', '', text)
    return ''.join(c for c in text if c.isalnum())


def skip(system_data, week, whole_week=False):
    directory = directory_for(system_data, week)
    with lock(directory):
        state = json.loads((directory/'session.json').read_text()) if (directory/'session.json').exists() else {'week': week, 'answered': []}
        if whole_week:
            state['state'] = 'partial_skipped' if state['answered'] else 'skipped'
            state['label'] = '访谈未完成' if state['answered'] else '本周未访谈'
            state['pending'] = None
        else:
            if not state.get('pending'):
                raise ValueError('no pending question')
            state['skipped'].append(state['pending']['id']); state['pending'] = None
        append_raw(directory, {'role': 'user_action', 'action': 'skip_week' if whole_week else 'skip_question'})
        save_json(directory/'session.json', state)
    return {'state': state['state'], 'label': state.get('label')}


def draft_outputs(system_data, week, plan):
    directory = directory_for(system_data, week)
    with lock(directory):
        raw = [json.loads(line) for line in (directory/'raw.jsonl').read_text().splitlines()]
        answers = {r['question_id']: r for r in raw if r['role'] == 'user'}
        if not answers:
            raise ValueError('no owner answers; never generate feelings or thoughts')
        quoted = []
        for item in plan.get('thought_candidates', []):
            source = answers.get(item.get('question_id'))
            if not source or not item.get('quote') or item['quote'] not in source['text_unchanged']:
                raise ValueError('thought candidate must quote a real answer exactly')
            quoted.append({**item, 'status': 'pending_owner_review', 'visibility': 'private',
                           'source_message_id': source['source_message_id']})
        # Feelings preview is made from raw/cleaned answers, never invented by a summarizer.
        cleaned = directory/'cleaned.md'
        text = cleaned.read_text() if cleaned.exists() else '\n\n'.join(a['text_unchanged'] for a in answers.values())
        target = outside_repo(system_data)/'phase-05-a/weekly'/week/'INTERVIEW_JOURNAL_PREVIEW.md'
        private_write(target, '# 这周的感受（待确认入库）\n\nvisibility: private\n\n'+text+'\n')
        save_json(directory/'candidates.json', quoted)
    return {'journal_preview': str(target), 'thought_candidates': len(quoted), 'formal_changes': False}
