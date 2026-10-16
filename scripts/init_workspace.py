"""Create an empty user workspace; never overwrite an existing destination."""
import argparse
import json
from pathlib import Path


def initialize(output, module_config):
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('workspace already exists; no files overwritten')
    config = json.loads(Path(module_config).read_text())
    output.mkdir(parents=True, mode=0o700)
    vault, system = output / 'vault', output / 'system'
    vault.mkdir(); (system / 'sources').mkdir(parents=True)
    links = []
    for ident, row in config['modules'].items():
        name = row['directory']
        if not name or Path(name).name != name or name in ('.', '..'):
            raise ValueError('unsafe module directory')
        folder = vault / name; folder.mkdir()
        index = ident + '-index'
        (folder / (index + '.md')).write_text('# ' + row['label'] + '\n', encoding='utf-8')
        links.append('- [[' + index + '|' + row['label'] + ']]')
    (vault / '00-首页.md').write_text('# 我的知识世界\n\n' + '\n'.join(links) + '\n', encoding='utf-8')
    (system / 'cards.json').write_text(json.dumps({'schema_version': 1, 'cards': {}, 'next_id': 'PKOS-000001'}, indent=2) + '\n')
    (system / 'sources/index.json').write_text('{"schema_version": 1, "sources": {}}\n')
    ledger = {'schema': 1, 'account': 'REPLACE_WITH_YOUR_ACCOUNT', 'repositories': {}, 'tools': [], 'skill_roots': [], 'external_skill_entries': [], 'global_instruction_files': []}
    (system / 'tools.md').write_text('# 我的工具总账\n\n<!-- tool-sync-index:start -->\n```json\n' + json.dumps(ledger, indent=2) + '\n```\n<!-- tool-sync-index:end -->\n')
    (system / 'sources.local.json').write_text('{"official_exports": [], "codex_roots": [], "pi_roots": []}\n')
    return {'workspace': str(output), 'vault': str(vault), 'system_data': str(system), 'source_scope': 'empty_explicitly_configure_before_use'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--module-config', type=Path, default=Path(__file__).resolve().parents[1] / 'config/modules.json')
    args = parser.parse_args()
    print(json.dumps(initialize(args.output, args.module_config), ensure_ascii=False, indent=2))
