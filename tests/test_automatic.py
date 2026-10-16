"""Synthetic intake tests; never read real chats or mutate an actual Vault."""
import json
from pathlib import Path
import tempfile
import unittest

from weekly.automatic import (apply, config_at, prepare, recover, rebase, status,
                              read_json, file_digest, save_json, set_enabled)


class AutomaticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / 'vault'
        self.data = self.root / 'system'
        self.data.mkdir()
        self.vault.mkdir()
        mapping = json.loads((Path(__file__).resolve().parents[1] / 'config/modules.json').read_text())
        for key, row in mapping['modules'].items():
            row['directory'] = key
            row['legacy_aliases'] = []
            folder = self.vault / key
            folder.mkdir()
            (folder / (key + '-overview.md')).write_text('# Overview\n')
            (folder / 'cards').mkdir()
        self.module = self.data / 'modules.json'
        save_json(self.module, mapping)
        (self.vault / '00-首页.md').write_text('# Home\n')
        self.card_index = self.data / 'cards.json'
        save_json(self.card_index, {'schema_version': 1, 'cards': {}, 'next_id': 'PKOS-000001'})
        (self.data / 'sources').mkdir()
        self.source_index = self.data / 'sources/index.json'
        save_json(self.source_index, {'schema_version': 1, 'sources': {}})
        self.session = self.root / 'session.jsonl'
        self.session.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'learning', 'source': 'cli'}}) + '\n')
        self.config = self.root / 'automatic.json'
        save_json(self.config, {'schema_version': 1, 'enabled': True,
                               'authorization': {'reference': 'synthetic-owner-request', 'quote': 'Please organize this learning chat automatically.'},
                               'system_data': str(self.data), 'vault': str(self.vault),
                               'module_config': str(self.module), 'card_index': str(self.card_index),
                               'source_index': str(self.source_index),
                               'sessions': [{'id': 'learning', 'path': str(self.session)}],
                               'routing': {kind: {'folder': module + '/cards', 'overview': module + '/' + module + '-overview.md'}
                                           for kind, module in [('thought', 'thinking'), ('knowledge', 'knowledge'), ('project', 'projects'), ('tool', 'skills')]}})
        self.cfg = config_at(self.config)

    def message(self, text, role='user', phase=None):
        payload = {'type': 'message', 'id': 'msg-' + str(len(self.session.read_text())), 'role': role,
                   'content': [{'type': 'input_text', 'text': text}]}
        if phase:
            payload['phase'] = phase
        with self.session.open('a') as handle:
            handle.write(json.dumps({'type': 'response_item', 'timestamp': '2026-01-01T10:00:00Z', 'payload': payload}) + '\n')

    def batch(self):
        prepared = prepare(self.cfg)
        folder = Path(prepared['directory'])
        evidence = [json.loads(line) for line in (folder / 'EVIDENCE.jsonl').read_text().splitlines()]
        return prepared, evidence

    def thought(self, prepared, evidence, action='new'):
        decision = {'id': 'T1', 'action': action, 'type': 'thought', 'title': 'Learn by using',
                    'core_key': 'learning-practice', 'body': 'Learning becomes useful through practice.',
                    'attribution': 'owner_view', 'owner_quotes': ['I learn by using it.'],
                    'evidence_ids': [r['evidence_id'] for r in evidence]}
        if action == 'update':
            decision['existing_id'] = 'PKOS-000001'
        return {'batch_id': prepared['batch_id'], 'decisions': [decision]}

    def test_incremental_write_and_stable_update(self):
        self.message('I learn by using it.')
        prepared, evidence = self.batch()
        self.assertEqual(prepare(self.cfg)['batch_id'], prepared['batch_id'])
        result = apply(self.cfg, self.thought(prepared, evidence))
        self.assertEqual(result['cards_written'], 1)
        self.assertTrue((Path(result['snapshot']) / 'vault-before/00-首页.md').is_file())
        self.assertEqual(prepare(self.cfg)['state'], 'idle')
        self.message('I learn by using it. Practice includes reflection.')
        prepared, evidence = self.batch()
        plan = self.thought(prepared, evidence, 'update')
        plan['decisions'][0]['body'] += '\n\nReflection helps make practice reusable.'
        apply(self.cfg, plan)
        index = read_json(self.card_index)
        self.assertEqual(len(index['cards']), 1)
        self.assertEqual(index['next_id'], 'PKOS-000002')
        self.assertEqual(len(index['cards']['PKOS-000001']['metadata']['source_refs']), 2)
        self.assertTrue(status(self.cfg)['validation']['pass'])

    def test_tool_write_enqueues_release_in_same_transaction(self):
        self.message('This is a synthetic reusable local tool.')
        prepared, evidence = self.batch()
        plan = self.thought(prepared, evidence)
        item = plan['decisions'][0]
        item.update(type='tool', subtype='internal', attribution='documented_tool',
                    fact_basis='Synthetic test implementation only', tool_registry_id='TOOL-001',
                    body='## 它是什么\n\nSynthetic tool.\n\n## 怎么用\n\nRun a local check.')
        result = apply(self.cfg, plan)
        card = self.vault / 'skills/cards/Learn by using.md'
        self.assertIn('## 拿去用', card.read_text())
        events = list((self.data / 'publications/events').glob('*.json'))
        self.assertEqual(len(events), 1)
        self.assertEqual(read_json(events[0])['card_ids'], ['PKOS-000001'])
        self.assertEqual(result['publication']['state'], 'pending_configuration')
        self.assertTrue(status(self.cfg)['validation']['pass'])

    def test_scope_and_feedback_filter(self):
        self.message('Developer narration', 'assistant', 'commentary')
        self.message('[PKOS_AUTO_INTAKE] scheduled instructions')
        self.message('<external_codex_apps_open_page>{}</external_codex_apps_open_page>')
        self.message('Automatic result', 'assistant', 'final_answer')
        self.assertEqual(prepare(self.cfg)['state'], 'idle')
        self.message('I learn by using it.')
        _, evidence = self.batch()
        self.assertEqual([r['text'] for r in evidence], ['I learn by using it.'])
        changed = self.session.read_text().replace('"id": "learning"', '"id": "unauthorized"', 1)
        self.session.write_text(changed)
        from weekly.automatic import selected_messages
        with self.assertRaisesRegex(ValueError, 'authorization'):
            selected_messages(self.cfg['sessions'][0], 0)

    def test_assistant_cannot_become_owner_thought(self):
        self.message('Please explain this.')
        self.message('I learn by using it.', 'assistant', 'final_answer')
        prepared, evidence = self.batch()
        with self.assertRaisesRegex(ValueError, 'exact owner quotes'):
            apply(self.cfg, self.thought(prepared, evidence))
        self.assertEqual(read_json(self.card_index)['cards'], {})

    def test_failed_validation_rolls_back_all_files_and_cursor(self):
        self.message('I learn by using it.')
        prepared, evidence = self.batch()
        before = {str(p): p.read_bytes() for p in [*self.vault.rglob('*.md'), self.card_index, self.source_index, self.data / 'automatic/STATE.json']}

        def fail(_):
            raise ValueError('injected validation failure')

        with self.assertRaisesRegex(ValueError, 'injected'):
            apply(self.cfg, self.thought(prepared, evidence), validator=fail)
        self.assertEqual({p: Path(p).read_bytes() for p in before}, before)
        self.assertFalse((self.vault / 'thinking/cards/Learn by using.md').exists())
        self.assertFalse(list((self.data / 'sources').glob('AUTO-*.md')))
        self.assertEqual(prepare(self.cfg)['batch_id'], prepared['batch_id'])

    def test_missing_disposition_is_rejected(self):
        self.message('I learn by using it.')
        prepared, _ = self.batch()
        with self.assertRaisesRegex(ValueError, 'every selected message'):
            apply(self.cfg, {'batch_id': prepared['batch_id'], 'decisions': []})

    def test_hold_keeps_original_for_later_clarification(self):
        self.message('I learn by using it.')
        prepared, evidence = self.batch()
        hold = {'id': 'H1', 'action': 'hold', 'reason': 'Need meaning', 'evidence_ids': [evidence[0]['evidence_id']]}
        apply(self.cfg, {'batch_id': prepared['batch_id'], 'decisions': [hold]})
        self.assertEqual(status(self.cfg)['held'], 1)
        self.message('By using I mean applying it to a small task.')
        later, rows = self.batch()
        context = [json.loads(line) for line in (Path(later['directory']) / 'CONTEXT.jsonl').read_text().splitlines()]
        plan = self.thought(later, rows)
        plan['decisions'][0]['evidence_ids'].append(context[0]['evidence_id'])
        plan['resolved_holds'] = [{'batch_id': prepared['batch_id'], 'id': 'H1'}]
        apply(self.cfg, plan)
        self.assertEqual(status(self.cfg)['held'], 0)

    def test_duplicate_core_and_conflicting_index_stop(self):
        self.message('I learn by using it.')
        prepared, evidence = self.batch()
        apply(self.cfg, self.thought(prepared, evidence))
        self.message('I learn by using it. Again.')
        prepared, evidence = self.batch()
        with self.assertRaisesRegex(ValueError, 'core key'):
            apply(self.cfg, self.thought(prepared, evidence))
        index = read_json(self.card_index)
        index['next_id'] = 'PKOS-000003'
        save_json(self.card_index, index)
        with self.assertRaisesRegex(ValueError, 'indexes changed'):
            apply(self.cfg, self.thought(prepared, evidence, 'update'))
        rebase(self.cfg)
        apply(self.cfg, self.thought(prepared, evidence, 'update'))

    def test_path_escape_and_pause_are_blocked(self):
        self.message('I learn by using it.')
        prepared, evidence = self.batch()
        plan = self.thought(prepared, evidence)
        plan['decisions'][0]['title'] = '../escape'
        with self.assertRaisesRegex(ValueError, 'unsafe card title'):
            apply(self.cfg, plan)
        set_enabled(self.config, False)
        with self.assertRaisesRegex(ValueError, 'paused'):
            prepare(config_at(self.config))

    def test_crash_recovery_and_external_edit_protection(self):
        self.message('I learn by using it.')
        prepared, evidence = self.batch()

        def crash(_):
            raise SystemExit('simulated process interruption')

        with self.assertRaises(SystemExit):
            apply(self.cfg, self.thought(prepared, evidence), validator=crash)
        with self.assertRaisesRegex(ValueError, 'unfinished transaction'):
            prepare(self.cfg)
        card = self.vault / 'thinking/cards/Learn by using.md'
        original = card.read_text()
        card.write_text(original + '\nExternal edit\n')
        with self.assertRaisesRegex(ValueError, 'changed externally'):
            recover(self.cfg, prepared['batch_id'])
        card.write_text(original)
        self.assertTrue(recover(self.cfg, prepared['batch_id'])['validation']['pass'])
        self.assertEqual(read_json(self.card_index)['cards'], {})

    def test_bad_links_rollback_and_preserve_private_audience(self):
        self.message('I learn by using it.')
        prepared, evidence = self.batch()
        plan = self.thought(prepared, evidence)
        plan['decisions'][0]['body'] += '\n\n[[Missing card]]'
        with self.assertRaisesRegex(ValueError, 'Vault validation failed'):
            apply(self.cfg, plan)
        self.assertEqual(status(self.cfg)['pending'], prepared['batch_id'])
        plan['decisions'][0]['body'] = 'Learning becomes useful through practice.'
        plan['decisions'][0]['audience'] = 'self'
        apply(self.cfg, plan)
        self.message('I learn by using it. This stays personal.')
        prepared, evidence = self.batch()
        apply(self.cfg, self.thought(prepared, evidence, 'update'))
        self.assertEqual(read_json(self.card_index)['cards']['PKOS-000001']['metadata']['audience'], 'self')

    def test_manual_body_edit_requires_rebase(self):
        self.message('I learn by using it.')
        prepared, evidence = self.batch()
        home = self.vault / '00-首页.md'
        home.write_text('# Home\n\nManually edited.\n')
        with self.assertRaisesRegex(ValueError, 'indexes changed'):
            apply(self.cfg, self.thought(prepared, evidence))
        rebase(self.cfg)
        apply(self.cfg, self.thought(prepared, evidence))
        self.assertIn('Manually edited.', home.read_text())

    def test_knowledge_requires_actual_external_verification(self):
        self.message('Explain a well documented principle.')
        prepared, evidence = self.batch()
        decision = {'id': 'K1', 'action': 'new', 'type': 'knowledge', 'title': 'Example principle',
                    'core_key': 'example-principle', 'attribution': 'external_knowledge',
                    'evidence_ids': [evidence[0]['evidence_id']],
                    'body': '## 知识定义\nDefinition\n\n## 核心原理\nPrinciple\n\n## 适用场景\nContext\n\n## 使用方法\nSteps'}
        plan = {'batch_id': prepared['batch_id'], 'decisions': [decision]}
        with self.assertRaisesRegex(ValueError, 'checked external evidence'):
            apply(self.cfg, plan)
        decision['verification'] = [{'url': 'https://example.org/principle',
                                     'checked_at': '2026-01-01T10:00:00Z', 'finding': 'Synthetic primary-source finding.'}]
        apply(self.cfg, plan)
        self.assertEqual(status(self.cfg)['validation']['card_types'], {'knowledge': 1})


if __name__ == '__main__':
    unittest.main()
