"""Read the one module mapping; never infer or rename Vault directories."""
import json
import os
from pathlib import Path
import unicodedata

MODULE_IDS = ('thinking', 'knowledge', 'skills', 'projects', 'timeline', 'media')
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / 'config/modules.json'


def component(value):
    if not isinstance(value, str) or not value or value in ('.', '..') or any(c in value for c in '/\\\0:'):
        raise ValueError('module directory must be one safe relative component')
    if value.startswith('.'):
        raise ValueError('hidden module directory is not supported')
    return value


def read_config(path=None, profile='current'):
    path = Path(path or os.environ.get('PKOS_MODULE_CONFIG') or DEFAULT_CONFIG)
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('modules'), dict) or data.get('schema_version') != 1 or set(data['modules']) != set(MODULE_IDS):
        raise ValueError('invalid module mapping schema or stable IDs')
    modules = data['modules']
    if any(not isinstance(row, dict) for row in modules.values()):
        raise ValueError('module entries must be objects')
    if profile == 'current':
        directories = {key: component(modules[key]['directory']) for key in MODULE_IDS}
    else:
        selected = data.get('profiles', {}).get(profile)
        if not isinstance(selected, dict) or set(selected) != set(MODULE_IDS):
            raise ValueError('unknown or incomplete module profile')
        directories = {key: component(selected[key]) for key in MODULE_IDS}
    names = [unicodedata.normalize('NFC', name).casefold() for name in directories.values()]
    if len(set(names)) != len(MODULE_IDS):
        raise ValueError('duplicate module directories')
    types = set()
    for key in MODULE_IDS:
        kinds = modules[key].get('types')
        aliases = modules[key].get('legacy_aliases', [])
        if not isinstance(kinds, list) or not kinds or any(not isinstance(x, str) or not x or x in types for x in kinds):
            raise ValueError('invalid or overlapping card types')
        types.update(kinds)
        if not isinstance(aliases, list):
            raise ValueError('module aliases must be a list')
        for alias in aliases:
            component(alias)
            if alias in directories.values() and alias != directories[key]:
                raise ValueError('module alias conflicts with another module')
    return data, directories


def check_directories(vault, data, directories, profile='current'):
    vault = Path(vault).resolve()
    for key, name in directories.items():
        path = vault / name
        if not path.is_dir() or not path.resolve().is_relative_to(vault):
            raise ValueError('missing module or module escapes Vault: ' + key)
        if profile == 'current':
            for alias in data['modules'][key].get('legacy_aliases', []):
                if alias != name and (vault / alias).exists():
                    raise ValueError('legacy/current module conflict: ' + key)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module-config', type=Path)
    parser.add_argument('--profile', default='current')
    parser.add_argument('--id', choices=MODULE_IDS)
    args = parser.parse_args()
    try:
        _, mapping = read_config(args.module_config, args.profile)
        print(mapping[args.id] if args.id else json.dumps(mapping, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
