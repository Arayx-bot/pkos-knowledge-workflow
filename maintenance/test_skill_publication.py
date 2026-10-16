"""End-to-end synthetic cards and release manifests; no live publication."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import yaml

from maintenance import test_public_release as fixtures
from maintenance import skill_publication as pub
from validation.validate import validate, load_note


class CardPublicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PublicReleaseTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.home = f.root / 'private-system'; (self.home / 'sources').mkdir(parents=True)
        self.vault = f.root / 'vault'; self.vault.mkdir()
        types = dict(thinking='thought', knowledge='knowledge', skills='tool', projects='project', timeline='weekly', media='script')
        mapping = {'schema_version': 1, 'modules': {k: {'directory': k, 'label': k, 'types': [t]} for k, t in types.items()}}
        self.modules = f.root / 'modules.json'; self.modules.write_text(json.dumps(mapping))
        for name in types:
            p = self.vault / name; p.mkdir()
            (p / (name + '-index.md')).write_text('# ' + name + '\n')
        (self.vault / '00-首页.md').write_text('# Synthetic home\n')
        self.card = self.vault / 'skills/example.md'
        self.meta = {'id': 'PKOS-000001', 'type': 'tool', 'subtype': 'internal', 'created': '2026-10-16',
                     'status': 'draft', 'visibility': 'private', 'tool_registry_id': 'TOOL-001', 'source_refs': []}
        self.write_card('## 拿去用\n\n公开版尚未发布。\n\n## 怎么用\n\nSynthetic usage.\n')
        self.source_index = self.home / 'sources/index.json'; self.source_index.write_text('{"sources": {}}')
        self.config = f.root / 'publication.json'
        self.config.write_text(json.dumps({'schema_version': 1, 'vault': str(self.vault), 'system_data': str(self.home),
            'module_config': str(self.modules), 'card_index': str(self.home / 'cards.json'),
            'source_index': str(self.source_index), 'registry': str(f.registry)}))

    def write_card(self, body):
        self.card.write_text('---\n' + yaml.safe_dump(self.meta) + '---\n\n# example\n\n' + body)
        (self.home / 'cards.json').write_text(json.dumps({'cards': {self.meta['id']: {
            'path': str(self.card.relative_to(self.vault)), 'metadata': self.meta}}, 'next_id': 'PKOS-000002'}))

    def state(self):
        return json.loads((self.home / 'publications/STATE.json').read_text())['cards'][self.meta['id']]

    def test_new_card_prepares_and_repeat_is_unchanged(self):
        with patch.object(pub.release, 'publish', side_effect=AssertionError('publication forbidden')):
            result = pub.reconcile(self.config)
            self.assertEqual(result['state'], 'updated')
            self.assertEqual(self.state()['status'], 'awaiting_human_file_approval')
            candidate = Path(self.state()['candidate'])
            self.assertTrue((candidate / 'assets').is_dir())
            self.assertEqual(pub.reconcile(self.config)['state'], 'unchanged')
            self.assertFalse(list(self.home.rglob('approval.json')))

    def test_card_edit_and_source_edit_are_detected_without_overwrite(self):
        pub.reconcile(self.config); first = self.state()['candidate']
        self.card.write_text(self.card.read_text() + '\nUpdated usage.\n')
        self.assertEqual(pub.reconcile(self.config)['changed_cards'], [self.meta['id']])
        self.assertEqual(first, self.state()['candidate'])
        (self.fixture.source / 'README.md').write_text('Updated synthetic source\n')
        pub.reconcile(self.config)
        self.assertNotEqual(first, self.state()['candidate'])
        self.assertTrue(Path(first).is_dir())

    def test_missing_ownership_is_durable_blocker(self):
        self.meta.pop('tool_registry_id'); self.write_card('## 拿去用\n\nPending.\n')
        result = pub.reconcile(self.config)
        self.assertEqual(result['state'], 'attention_required')
        self.assertEqual(self.state()['status'], 'blocked')
        self.assertEqual(pub.reconcile(self.config)['state'], 'unchanged')

    def test_outbox_is_staged_with_card_transaction_and_acknowledged(self):
        writes = {}
        pub.enqueue(writes, self.home, 'synthetic-event', [self.meta['id']])
        self.assertEqual(len(writes), 1)
        path, value = next(iter(writes.items())); path.parent.mkdir(parents=True); path.write_text(value)
        pub.reconcile(self.config)
        self.assertEqual(json.loads(path.read_text())['state'], 'reconciled')

    def test_published_source_change_requires_new_version(self):
        pub.reconcile(self.config)
        data = pub.sync.load_ledger(self.fixture.registry)
        dist = data['tools'][0]['public_distribution']
        dist.update(status='public_release_verified', last_approved_digest=self.state()['approval_digest'],
                    public_release_url='https://github.com/synthetic/example/releases/tag/v0.1.0')
        pub.sync.save_ledger(self.fixture.registry, data)
        (self.fixture.source / 'README.md').write_text('Changed source after release\n')
        pub.reconcile(self.config)
        self.assertEqual(self.state()['reason'], 'public_source_changed_requires_new_version')

    def test_verified_backfill_preserves_private_and_validates(self):
        pub.reconcile(self.config)
        data = pub.sync.load_ledger(self.fixture.registry)
        dist = data['tools'][0]['public_distribution']
        dist.update(status='public_release_verified', last_approved_digest=self.state()['approval_digest'],
                    public_repo_url='https://github.com/synthetic/example',
                    public_release_url='https://github.com/synthetic/example/releases/tag/v0.1.0')
        pub.sync.save_ledger(self.fixture.registry, data)
        result = pub.backfill_release(self.config, self.fixture.registry, 'TOOL-001')
        self.assertEqual(result['status'], 'passed')
        meta, body = load_note(self.card)
        self.assertEqual(meta['visibility'], 'private')
        self.assertIn(dist['public_release_url'], body)
        self.assertTrue(Path(result['backup']).is_dir())
        self.assertTrue(validate(self.vault, self.modules, self.home / 'cards.json', self.source_index)['pass'])
        self.assertEqual(pub.backfill_release(self.config, self.fixture.registry)['status'], 'unchanged')


    def test_unclassified_card_in_configured_internal_folder_cannot_be_missed(self):
        self.meta.pop('subtype'); self.meta.pop('tool_registry_id')
        self.write_card('## 拿去用\n\nPending.\n')
        config = json.loads(self.config.read_text()); config['internal_card_folders'] = ['skills']
        self.config.write_text(json.dumps(config))
        result = pub.reconcile(self.config)
        self.assertEqual(result['state'], 'attention_required')
        self.assertEqual(self.state()['reason'], 'card_tool_ownership_missing_or_ambiguous')

    def test_failed_backfill_validation_restores_card_and_index(self):
        pub.reconcile(self.config)
        data = pub.sync.load_ledger(self.fixture.registry)
        data['tools'][0]['public_distribution'].update(status='public_release_verified',
            last_approved_digest=self.state()['approval_digest'], public_repo_url='https://github.com/synthetic/example',
            public_release_url='https://github.com/synthetic/example/releases/tag/v0.1.0')
        pub.sync.save_ledger(self.fixture.registry, data)
        before = self.card.read_bytes(); index = (self.home/'cards.json').read_bytes()
        with patch.object(pub, 'validate', return_value={'pass': False}):
            with self.assertRaisesRegex(ValueError, 'validation failed'):
                pub.backfill_release(self.config, self.fixture.registry)
        self.assertEqual(self.card.read_bytes(), before)
        self.assertEqual((self.home/'cards.json').read_bytes(), index)


if __name__ == '__main__':
    unittest.main()
