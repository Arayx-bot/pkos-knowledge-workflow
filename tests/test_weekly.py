import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime
from weekly.workflow import ZONE, prepare, render, week_bounds, validate_plan
from weekly import interview


class WeeklyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = datetime(2026, 10, 19, 9, tzinfo=ZONE)
        self.week = '2026-W42'
        self.evidence = [{'evidence_id': 'E00001', 'source': 'D', 'session_id': 'synthetic', 'message_id': 'u1',
                          'role': 'user', 'text': '合成原话', 'timestamp': '2026-10-13T10:00:00+08:00',
                          'path': 'synthetic.jsonl', 'line': 2, 'attribution': 'owner_statement_needs_context',
                          'time_basis': 'session_message', 'week_eligible': True}]
        self.plan = {'candidates': [{'id': 'R01', 'title': '合成观点', 'type': 'thought', 'action': '新建',
                     'reason': '本人有明确解释', 'body': '合成正文，仅用于测试。', 'evidence_ids': ['E00001'], 'attribution': 'owner_view'}],
                     'interview_questions': [{'id': f'Q{i}', 'question': f'合成事件{i}为什么这样决定？',
                                             'event_anchor': f'合成事件{i}', 'evidence_ids': ['E00001']} for i in range(1,4)]}

    def tearDown(self):
        self.tmp.cleanup()

    def prepared(self):
        with patch('weekly.workflow.subprocess.run') as run, patch('weekly.workflow.gather', return_value=(self.evidence, {'A': {'errors': []}})):
            run.return_value.returncode = 0
            return prepare(self.root, {}, self.week, self.now)

    def test_iso_week_previous_complete_and_future_blocked(self):
        week, start, end = week_bounds(now=self.now)
        self.assertEqual(week, '2026-W42')
        self.assertEqual(start.strftime('%Y-%m-%d'), '2026-10-12')
        with self.assertRaises(ValueError):
            week_bounds('2026-W43', self.now)
        self.assertEqual(week_bounds(now=datetime(2027,1,4,9,tzinfo=ZONE))[0], '2026-W53')

    def test_check_failure_prevents_extraction_and_file_creation(self):
        with patch('weekly.workflow.subprocess.run') as run, patch('weekly.workflow.gather') as gather:
            run.return_value.returncode = 2
            with self.assertRaises(ValueError):
                prepare(self.root, {}, self.week, self.now)
            gather.assert_not_called()
            self.assertFalse((self.root/'phase-05-a').exists())

    def test_provisional_review_idempotence_six_columns_and_no_formal_write(self):
        self.assertTrue(self.prepared()['provisional'])
        self.assertTrue(self.prepared()['already_exists'])
        result = render(self.root, self.week, self.plan)
        directory = Path(result['directory'])
        text = (directory/'REVIEW.md').read_text()
        self.assertIn('暂定', text)
        self.assertEqual(len(next(l for l in text.splitlines() if l.startswith('| 编号')).split('|'))-2, 6)
        self.assertIn('visibility: private', (directory/'previews/R01.md').read_text())
        self.assertFalse((self.root/'vault').exists())
        with self.assertRaises(ValueError):
            render(self.root, self.week, self.plan)

    def test_ai_only_or_baseline_cannot_establish_owner_view(self):
        self.evidence[0]['role'] = 'assistant'
        with self.assertRaises(ValueError):
            validate_plan(self.plan, self.evidence)
        self.evidence[0]['role'] = 'user'; self.evidence[0]['week_eligible'] = False
        with self.assertRaises(ValueError):
            validate_plan(self.plan, self.evidence)

    def test_unknown_meaning_has_no_body(self):
        self.plan['candidates'][0]['action'] = '待答'
        with self.assertRaises(ValueError):
            validate_plan(self.plan, self.evidence)

    def test_interview_unchanged_raw_one_question_followup_and_quotes(self):
        self.prepared(); render(self.root, self.week, self.plan)
        interview.start(self.root, self.week)
        first = interview.next_question(self.root, self.week)
        self.assertEqual(first['id'], 'Q1')
        self.assertEqual(interview.next_question(self.root, self.week)['id'], 'Q1')
        raw = '嗯，我很开心。\n因为经验能变成工具。'
        answer = {'role': 'user', 'source_kind': 'voice_transcript', 'source_message_id': 'synthetic-owner-1', 'text_unchanged': raw}
        interview.answer(self.root, self.week, answer, '我很开心，因为经验能变成工具。')
        events = [json.loads(l) for l in (self.root/'interviews'/self.week/'raw.jsonl').read_text().splitlines()]
        self.assertEqual(next(r for r in events if r['role']=='user')['text_unchanged'], raw)
        self.assertEqual(interview.next_question(self.root, self.week, '是哪条经验？')['id'], 'Q1-followup')
        interview.answer(self.root, self.week, {**answer, 'source_message_id': 'synthetic-owner-2', 'text_unchanged': '预算规则。'})
        with self.assertRaises(ValueError):
            interview.next_question(self.root, self.week, '再问一次？')
        result = interview.draft_outputs(self.root, self.week, {'thought_candidates': [{'question_id': 'Q1', 'quote': '经验能变成工具', 'core': '待审观点'}]})
        self.assertFalse(result['formal_changes'])
        with self.assertRaises(ValueError):
            interview.draft_outputs(self.root, self.week, {'thought_candidates': [{'question_id': 'Q1', 'quote': '虚构原话'}]})

    def test_never_accept_ai_answer_or_change_meaning(self):
        self.prepared(); render(self.root, self.week, self.plan); interview.start(self.root, self.week); interview.next_question(self.root, self.week)
        answer = {'role': 'assistant', 'source_kind': 'user_message', 'source_message_id': 'synthetic', 'text_unchanged': '开心。'}
        with self.assertRaises(ValueError):
            interview.answer(self.root, self.week, answer)
        with self.assertRaises(ValueError):
            interview.answer(self.root, self.week, {**answer, 'role': 'user'}, '不开心。')

    def test_skipping_week_is_not_fabricated_feelings(self):
        result = interview.skip(self.root, self.week, True)
        self.assertEqual(result['label'], '本周未访谈')
        with self.assertRaises(ValueError):
            interview.draft_outputs(self.root, self.week, {})
