"""Synthetic cross-session intake; no user files, GitHub calls, or publication."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from maintenance import test_skill_publication as fixtures
from maintenance import skill_lifecycle as life, skill_lifecycle_hook as hook
from validation.validate import load_note


class SkillLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CardPublicationTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture; origin = f.fixture
        self.config, self.root, self.repo = f.config, origin.root, origin.repo
        self.source = origin.source
        self.cfg = json.loads(self.config.read_text())
        self.cfg.update(internal_card_folders=['skills'], skills_overview_filename='skills-index.md',
                        general_skill_root=str(self.repo / 'skills'), general_skill_repository='source')
        self.config.write_text(json.dumps(self.cfg))
        f.meta.update(skill_path=str(self.source / 'SKILL.md'), skill_name='example')
        f.write_card('## 拿去用\n\nPending.\n\n## 怎么用\n\nHand-edited usage must survive.\n')
        data = life.sync.load_ledger(origin.registry)
        data['skill_roots'] = []; data['external_skill_entries'] = []
        data['tools'][0].update(skill_name='example', skill_entries=[str(self.source / 'SKILL.md')], links=[])
        origin.registry.write_text('# Tool registry\n\n## 总览（1 项）\n\n| ID / 原编号 | 名称 | 类型 | 用途 | 状态 | 归属 |\n|---|---|---|---|---|---|\n| TOOL-001 | example | Skill | Synthetic | Local | source |\n\n## 本地位置与审查记录\n\n### TOOL-001 · example\n\n' + life.sync.START + '\n```json\n' + json.dumps(data) + '\n```\n' + life.sync.END)

    def make_skill(self, directory, name):
        directory.mkdir(parents=True)
        (directory / 'SKILL.md').write_text('---\nname: ' + name + '\ndescription: Synthetic reusable source\n---\n# Synthetic\n')
        (directory / 'README.md').write_text('Synthetic installation guide; no private inputs.\n')
        (directory / 'LICENSE').write_text('MIT License\n')
        (directory / 'distribution').mkdir()
        (directory / 'distribution/workbuddy.json').write_text(json.dumps({'description_zh': '合成工具', 'description_en': 'Synthetic tool', 'version': '0.1.0', 'author': 'synthetic'}))
        return ['SKILL.md', 'README.md', 'LICENSE', 'distribution/workbuddy.json']

    def ledger(self):
        return life.sync.load_ledger(self.fixture.fixture.registry)

    def test_new_owned_skill_registers_card_and_ready_candidate_without_publication(self):
        source = self.repo / 'skills/new-example'; files = self.make_skill(source, 'new-example')
        with patch.object(life.publication.release, 'publish', side_effect=AssertionError('no publication')):
            result = life.finish(self.config, source, True, 'synthetic owner development request', public_files=files)
        self.assertTrue(result['new_tool']); self.assertTrue(result['new_card'])
        self.assertEqual(result['tool'], 'TOOL-002'); self.assertEqual(result['card'], 'PKOS-000002')
        cards = json.loads((self.fixture.home / 'cards.json').read_text())
        self.assertEqual(cards['cards']['PKOS-000002']['metadata']['visibility'], 'private')
        state = json.loads((self.fixture.home / 'publications/STATE.json').read_text())
        self.assertEqual(state['cards']['PKOS-000002']['status'], 'awaiting_human_file_approval')
        self.assertEqual(len(self.ledger()['tools']), 2)
        text = self.fixture.fixture.registry.read_text()
        self.assertIn('### TOOL-002 · new-example', text); self.assertIn('下一编号', text) if '下一编号' in text else None
        self.assertFalse(list(self.root.rglob('approval.json')))

    def test_update_keeps_ids_and_manual_body(self):
        one = life.finish(self.config, self.source, prepare_release=False)
        self.assertEqual(one['card'], 'PKOS-000001')
        before_count = len(self.ledger()['tools'])
        (self.source / 'README.md').write_text('Updated synthetic docs\n')
        two = life.finish(self.config, self.source, prepare_release=False)
        self.assertEqual(two['tool'], one['tool']); self.assertEqual(two['card'], one['card'])
        self.assertIn('Hand-edited usage must survive', self.fixture.card.read_text())
        self.assertEqual(len(self.ledger()['tools']), before_count)
        self.assertEqual(life.finish(self.config, self.source, prepare_release=False)['state'], 'unchanged')

    def test_unknown_origin_cannot_be_guessed_from_local_installation(self):
        source = self.repo / 'skills/unknown'; self.make_skill(source, 'unknown')
        with self.assertRaisesRegex(life.sync.Blocked, 'explicit_self_built'):
            life.finish(self.config, source, prepare_release=False)
        self.assertEqual(len(self.ledger()['tools']), 1)

    def test_unmanaged_general_skill_moves_once_and_preserves_old_symlink(self):
        source = self.root / 'installed/my-new-skill'; files = self.make_skill(source, 'my-new-skill')
        original = life.source_files(source)
        result = life.finish(self.config, source, True, 'synthetic creation request', public_files=files)
        self.assertTrue(source.is_symlink())
        canonical = self.repo / 'skills/my-new-skill'
        self.assertEqual(source.resolve(), canonical)
        self.assertEqual(life.source_files(canonical), original)
        self.assertEqual(result['canonical_move']['target'], str(canonical))

    def test_destination_collision_does_not_move_or_overwrite(self):
        source = self.root / 'installed/collision'; self.make_skill(source, 'collision')
        target = self.repo / 'skills/collision'; target.mkdir(); (target / 'keep.txt').write_text('Keep original')
        with self.assertRaisesRegex(life.sync.Blocked, 'destination_conflict'):
            life.finish(self.config, source, True, 'synthetic source', prepare_release=False)
        self.assertFalse(source.is_symlink()); self.assertEqual((target / 'keep.txt').read_text(), 'Keep original')

    def test_failed_card_validation_restores_registry_indexes_and_move(self):
        source = self.root / 'installed/rollback-example'; files = self.make_skill(source, 'rollback-example')
        ledger_before = self.fixture.fixture.registry.read_bytes()
        cards_before = (self.fixture.home / 'cards.json').read_bytes()
        with patch.object(life, 'validate', return_value={'pass': False}):
            with self.assertRaisesRegex(ValueError, 'validation'):
                life.finish(self.config, source, True, 'synthetic request', public_files=files, prepare_release=False)
        self.assertFalse(source.is_symlink()); self.assertTrue((source / 'SKILL.md').is_file())
        self.assertFalse((self.repo / 'skills/rollback-example').exists())
        self.assertEqual(self.fixture.fixture.registry.read_bytes(), ledger_before)
        self.assertEqual((self.fixture.home / 'cards.json').read_bytes(), cards_before)

    def test_external_skill_is_excluded_and_cannot_be_relabelled_owned(self):
        source = self.repo / 'skills/external-example'; self.make_skill(source, 'external-example')
        life.mark_external(self.config, source, 'Synthetic upstream installation')
        self.assertNotIn(str(source / 'SKILL.md'), life.discover(self.ledger()))
        with self.assertRaisesRegex(life.sync.Blocked, 'external_skill'):
            life.finish(self.config, source, True, 'cannot override upstream provenance')

    def test_hook_other_session_detects_new_skill_and_continues_for_origin(self):
        event = {'session_id': 'another-codex-window', 'cwd': str(self.repo), 'hook_event_name': 'SessionStart'}
        hook.handle(self.config, event)
        source = self.repo / 'skills/cross-window'; files = self.make_skill(source, 'cross-window')
        event.update(hook_event_name='Stop', turn_id='synthetic-turn', stop_hook_active=False)
        response = hook.handle(self.config, event)
        self.assertEqual(response['decision'], 'block')
        self.assertIn('classify_self_built', response['reason'])
        life.finish(self.config, source, True, 'other-window owner request', public_files=files)
        event['stop_hook_active'] = True
        self.assertEqual(hook.handle(self.config, event), {})

    def test_scan_bootstraps_registered_card_without_repeated_ids(self):
        one = life.reconcile(self.config)
        two = life.reconcile(self.config)
        self.assertEqual(one['state'], 'updated'); self.assertEqual(two['state'], 'unchanged')
        self.assertFalse(one['transcripts_read']); self.assertFalse(one['whole_disk_scanned'])


    def test_project_linking_same_skill_does_not_count_as_duplicate_tool_card(self):
        import yaml
        project = self.fixture.vault / 'projects/Example project.md'
        metadata = {'id': 'PKOS-000002', 'type': 'project', 'created': '2026-10-16',
                    'status': 'draft', 'visibility': 'private', 'skill_path': str(self.source/'SKILL.md')}
        project.write_text('---\n'+yaml.safe_dump(metadata)+'---\n\n# Example project\n')
        index_path = self.fixture.home / 'cards.json'
        index = json.loads(index_path.read_text())
        index['cards']['PKOS-000002'] = {'path': str(project.relative_to(self.fixture.vault)), 'metadata': metadata}
        index['next_id'] = 'PKOS-000003'; index_path.write_text(json.dumps(index))
        result = life.finish(self.config, self.source, prepare_release=False)
        self.assertEqual(result['card'], 'PKOS-000001')
        self.assertFalse(result['new_card'])


    def test_project_recipe_exports_sibling_implementation_without_source_copy(self):
        source = self.repo / 'skills/project-tool'; files = self.make_skill(source, 'project-tool')
        sibling = self.repo / 'helper.py'; sibling.write_text('print("synthetic helper")\n')
        mapping = {rel: str((source/rel).relative_to(self.repo)) for rel in files}
        mapping['scripts/helper.py'] = 'helper.py'
        recipe = {'source_directory': str(self.repo), 'files': list(mapping), 'file_map': mapping}
        result = life.finish(self.config, source, True, 'synthetic project request', public_files=recipe)
        self.assertEqual(result['publication']['state'], 'updated')
        self.assertFalse((source/'scripts/helper.py').exists())
        data = self.ledger(); config = data['tools'][-1]['public_distribution']
        exported = Path(config['candidate'])/'repository/scripts/helper.py'
        self.assertEqual(exported.read_bytes(), sibling.read_bytes())

    def test_sibling_change_is_seen_by_session_hook_and_unchanged_finish(self):
        source = self.repo / 'skills/sibling-watch'; files = self.make_skill(source, 'sibling-watch')
        sibling = self.repo / 'helper.py'; sibling.write_text('print("one")\n')
        mapping = {rel: str((source/rel).relative_to(self.repo)) for rel in files}
        mapping['scripts/helper.py'] = 'helper.py'
        recipe = {'source_directory': str(self.repo), 'files': list(mapping), 'file_map': mapping}
        life.finish(self.config, source, True, 'synthetic origin', public_files=recipe)
        event = {'session_id': 'sibling-window', 'cwd': str(self.repo), 'hook_event_name': 'SessionStart'}
        hook.handle(self.config, event)
        sibling.write_text('print("two")\n')
        event.update(hook_event_name='Stop', turn_id='sibling-change', stop_hook_active=False)
        hook.handle(self.config, event)
        receipt = json.loads((self.fixture.home/'skill-lifecycle/sessions/sibling-window/LAST_STOP.json').read_text())
        self.assertIn(str(source/'SKILL.md'), receipt['changed'])
        sibling.write_text('print("three")\n')
        result = life.finish(self.config, source)
        self.assertEqual(result['state'], 'unchanged')
        self.assertTrue(result['publication']['prepared_candidates'])

    def test_manual_card_update_requests_semantic_review_until_explicit_body_saved(self):
        life.finish(self.config, self.source, prepare_release=False)
        entry = self.source/'SKILL.md'; entry.write_text(entry.read_text()+'\nNew synthetic capability.\n')
        result = life.finish(self.config, self.source, prepare_release=False)
        self.assertTrue(result['needs_semantic_card_review'])
        repeated = life.finish(self.config, self.source, prepare_release=False)
        self.assertTrue(repeated['needs_semantic_card_review'])
        body = self.root/'reviewed-body.md'; body.write_text('## 拿去用\n\nSynthetic reviewed instructions.\n')
        reviewed = life.finish(self.config, self.source, body_file=body, prepare_release=False)
        self.assertFalse(reviewed['needs_semantic_card_review'])


if __name__ == '__main__':
    unittest.main()
