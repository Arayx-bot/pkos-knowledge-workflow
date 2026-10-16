import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from weekly.sources import pi_records, gather, reconcile


class SourceTests(unittest.TestCase):
    def test_pi_original_owner_and_ai_text_only_active_branch(self):
        rows = [
            {'type': 'session', 'version': 3, 'id': 'session'},
            {'type': 'message', 'id': 'u1', 'parentId': None, 'timestamp': '2026-10-13T09:00:00Z',
             'message': {'role': 'user', 'content': [{'type': 'text', 'text': '合成的本人决定'}]}},
            {'type': 'message', 'id': 'discarded', 'parentId': 'u1', 'timestamp': '2026-10-13T09:01:00Z',
             'message': {'role': 'assistant', 'content': '放弃的分支'}},
            {'type': 'message', 'id': 'a1', 'parentId': 'u1', 'timestamp': '2026-10-13T09:02:00Z',
             'message': {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': '隐藏推理'}, {'type': 'text', 'text': '合成建议'}]}},
            {'type': 'message', 'id': 't1', 'parentId': 'a1', 'message': {'role': 'toolResult', 'content': '执行输出'}},
            {'type': 'compaction', 'id': 'compact', 'parentId': 't1', 'summary': '助手摘要'},
            {'type': 'context_edit', 'id': 'edit', 'parentId': 'compact'},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'session.jsonl'
            path.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows))
            records = list(pi_records(path))
            self.assertEqual([r['message_id'] for r in records], ['u1', 'a1'])
            self.assertEqual(records[1]['text'], '合成建议')
            self.assertEqual(records[1]['attribution'], 'ai_suggestion')
            self.assertTrue(records[0]['context_edit_present'])
            self.assertNotEqual(records[0]['attribution'], 'confirmed_decision')

    def test_unknown_pi_version_and_broken_branch_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'session.jsonl'
            path.write_text('{"type":"session","version":99}')
            with self.assertRaises(ValueError):
                list(pi_records(path))
            path.write_text('{"type":"session","version":3}\n{"type":"message","id":"x","parentId":"missing"}')
            with self.assertRaises(ValueError):
                list(pi_records(path))

    def test_official_time_wins_over_later_capture_and_legacy_is_unassigned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            official = root / 'conversations.json'
            official.write_text(json.dumps([{'id': 'chat', 'current_node': 'node', 'mapping': {
                'node': {'parent': None, 'message': {'id': 'matched', 'create_time': 1791244800,
                        'author': {'role': 'user'}, 'content': {'parts': ['official']}}}}}]))
            db = root / 'web.sqlite3'
            with sqlite3.connect(db) as conn:
                conn.execute('CREATE TABLE messages(conversation_id,message_id,role,content,captured_at,message_time,capture_mode)')
                for mid, mode in [('matched', 'live_incremental'), ('old', 'baseline'), ('new', 'live_incremental')]:
                    conn.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)', ('chat', mid, 'user', 'synthetic', '2026-10-13T09:00:00Z', None, mode))
            config = {'official_exports': [{'path': str(official)}], 'web_database': str(db)}
            rows, _ = gather(config, datetime(2026, 10, 12, tzinfo=timezone.utc), datetime(2026, 10, 19, tzinfo=timezone.utc))
            eligible = [r['message_id'] for r in rows if r['week_eligible']]
            self.assertEqual(eligible, ['new'])
            findings = reconcile(config)
            self.assertTrue(next(r for r in findings if r['message_id'] == 'matched')['content_conflict'])
            self.assertEqual(next(r for r in findings if r['message_id'] == 'old')['state'], 'unmatched_keep')
