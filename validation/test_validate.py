"""Synthetic module/validator regressions; no personal Vault or backup access."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate as validator
from module_config import read_config, MODULE_IDS, DEFAULT_CONFIG


class ModuleValidationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root/'vault'; self.vault.mkdir()
        self.system = self.root/'system'; self.system.mkdir()
        self.config = self.root/'modules.json'
        kinds = ('thought', 'knowledge', 'tool', 'project', 'weekly', 'script')
        self.mapping = {'schema_version': 1, 'modules': {
            key: {'directory': 'module-'+key, 'types': [kind], 'legacy_aliases': ['previous-'+key]}
            for key, kind in zip(MODULE_IDS, kinds)}, 'profiles': {
                'legacy_v3': {key: 'legacy-'+key for key in MODULE_IDS}}}
        self.save_mapping()
        for row in self.mapping['modules'].values(): (self.vault/row['directory']).mkdir()
        (self.vault/'00-首页.md').write_text('# Synthetic Home\n')
        self.cards = {}
        self.write_card('thinking', 'PKOS-000001', 'thought')
        self.write_card('projects', 'PKOS-000002', 'project')
        self.index = self.system/'卡片索引.json'
        self.save_index()
        (self.system/'sources').mkdir()
        self.source_index = self.system/'sources/index.json'
        self.source_index.write_text('{"sources":{}}')

    def save_mapping(self): self.config.write_text(json.dumps(self.mapping))

    def write_card(self, module, ident, kind, name=None):
        path = self.vault/self.mapping['modules'][module]['directory']/'cards'/(name or (ident+'.md'))
        path.parent.mkdir(exist_ok=True)
        meta = {'id': ident, 'type': kind, 'created': '2026-10-13', 'status': 'draft', 'source_refs': []}
        path.write_text('---\n'+yaml.safe_dump(meta, sort_keys=False)+'---\n# Synthetic '+ident+'\n')
        self.cards[ident] = {'path': str(path.relative_to(self.vault)), 'metadata': meta}
        return path

    def save_index(self, next_id='PKOS-000003'):
        self.index.write_text(json.dumps({'schema_version': 1, 'cards': self.cards, 'next_id': next_id}))

    def check(self):
        return validator.validate(self.vault, self.config, self.index, self.source_index)

    def test_not_fixed_to_twelve_cards(self):
        result = self.check()
        self.assertTrue(result['pass'], result['errors'])
        self.assertEqual(result['card_count'], 2)

    def test_duplicate_card_ids_rejected(self):
        self.write_card('thinking', 'PKOS-000001', 'thought', 'duplicate.md')
        self.assertTrue(any('duplicate card ID' in e for e in self.check()['errors']))

    def test_next_id_collision_is_read_only(self):
        self.save_index('PKOS-000001'); before = self.index.read_bytes()
        self.assertFalse(self.check()['pass'])
        self.assertEqual(before, self.index.read_bytes())

    def test_advanced_allocation_cursor_preserved(self):
        self.save_index('PKOS-000023'); before = self.index.read_bytes()
        self.assertTrue(self.check()['pass'])
        self.assertEqual(before, self.index.read_bytes())

    def test_renamed_module_without_code_changes(self):
        old = self.mapping['modules']['thinking']['directory']
        self.mapping['modules']['thinking']['directory'] = 'renamed-thinking'; self.save_mapping()
        (self.vault/old).rename(self.vault/'renamed-thinking')
        self.cards['PKOS-000001']['path'] = self.cards['PKOS-000001']['path'].replace(old+'/', 'renamed-thinking/', 1)
        self.save_index(); self.assertTrue(self.check()['pass'])

    def test_duplicate_module_names_rejected(self):
        self.mapping['modules']['knowledge']['directory'] = self.mapping['modules']['thinking']['directory']; self.save_mapping()
        with self.assertRaises(ValueError): self.check()

    def test_missing_config_rejected(self):
        self.config.unlink()
        with self.assertRaises(OSError): self.check()

    def test_unknown_profile_rejected(self):
        with self.assertRaises(ValueError): read_config(self.config, 'unknown')

    def test_non_object_configuration_rejected(self):
        self.config.write_text('[]')
        with self.assertRaises(ValueError): self.check()

    def test_non_object_module_entry_rejected(self):
        self.mapping['modules']['thinking'] = None; self.save_mapping()
        with self.assertRaises(ValueError): self.check()

    def test_traversal_config_rejected(self):
        self.mapping['modules']['thinking']['directory'] = '../outside'; self.save_mapping()
        with self.assertRaises(ValueError): self.check()

    def test_missing_module_rejected(self):
        (self.vault/self.mapping['modules']['knowledge']['directory']).rmdir()
        with self.assertRaises(ValueError): self.check()

    def test_old_and_new_conflict_rejected(self):
        (self.vault/'previous-thinking').mkdir()
        with self.assertRaises(ValueError): self.check()

    def test_module_symlink_escape_rejected(self):
        p=self.vault/self.mapping['modules']['knowledge']['directory'];p.rmdir()
        outside=self.root/'outside';outside.mkdir();p.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError): self.check()

    def test_duplicate_display_names_rejected(self):
        path = self.vault/self.mapping['modules']['knowledge']['directory']/'PKOS-000001.md'
        path.write_text('# Navigation\n')
        self.assertTrue(any('ambiguous display name' in e for e in self.check()['errors']))

    def test_broken_link_rejected_and_code_example_ignored(self):
        path=self.vault/self.cards['PKOS-000001']['path']
        path.write_text(path.read_text()+'\n`[[Example only]]`\n[[Missing card]]\n')
        result=self.check(); self.assertEqual(result['wiki_links_checked'], 1)
        self.assertTrue(any('Missing card' in e for e in result['errors']))

    def test_alias_path_heading_and_block_links(self):
        path=self.vault/self.cards['PKOS-000001']['path']
        path.write_text(path.read_text()+'\n## Detail\nParagraph ^evidence\n[[PKOS-000002|Project]]\n[[#Detail]]\n[[#^evidence]]\n')
        self.assertTrue(self.check()['pass'], self.check()['errors'])

    def test_missing_local_file_link_rejected(self):
        path=self.vault/self.cards['PKOS-000001']['path']
        path.write_text(path.read_text()+'\n[missing](<missing file.md>)\n')
        self.assertTrue(any('missing local Markdown link' in e for e in self.check()['errors']))

    def test_source_hash_and_unknown_relation(self):
        raw=self.system/'sources/evidence.md';raw.write_text('Synthetic evidence')
        self.source_index.write_text(json.dumps({'sources': {'S1': {'file':'evidence.md','sha256':hashlib.sha256(raw.read_bytes()).hexdigest()}}}))
        row=self.cards['PKOS-000001'];row['metadata']['source_refs']=['S1'];row['metadata']['related']=['PKOS-999999']
        path=self.vault/row['path'];path.write_text('---\n'+yaml.safe_dump(row['metadata'])+'---\n# Synthetic\n');self.save_index()
        self.assertTrue(any('unresolved related' in e for e in self.check()['errors']))
        raw.write_text('Changed synthetic evidence')
        self.assertTrue(any('source hash mismatch' in e for e in self.check()['errors']))

    def test_type_not_renamed_to_module_id(self):
        self.assertEqual(self.cards['PKOS-000001']['metadata']['type'], 'thought')
        self.assertTrue(self.check()['pass'])

    def test_shipped_profiles_have_same_stable_ids(self):
        _, current=read_config(DEFAULT_CONFIG)
        _, legacy=read_config(DEFAULT_CONFIG, 'legacy_v3')
        self.assertEqual(set(current), set(legacy))
        self.assertNotEqual(current['thinking'], legacy['thinking'])

    def test_cli_does_not_write_any_fixture_file(self):
        before={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        command=[sys.executable,'-B',str(Path(validator.__file__)), '--paths',str(self.root/'absent.json'),
                 '--vault',str(self.vault),'--system-data',str(self.system),'--module-config',str(self.config)]
        result=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})


if __name__ == '__main__': unittest.main()
