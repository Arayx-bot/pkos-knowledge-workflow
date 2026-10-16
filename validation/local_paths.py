"""Explicit local paths for PKOS validators; no personal data defaults."""
import json
from pathlib import Path

def load_paths(parser):
    parser.add_argument('--paths', type=Path, default=Path(__file__).resolve().parents[1] / 'config/paths.json')
    for key in ('system_data', 'run', 'context_script', 'workspace', 'legacy_vault', 'baseline', 'output_dir', 'module_config', 'card_index', 'source_index'):
        parser.add_argument('--' + key.replace('_', '-'), type=Path)
    args = parser.parse_args()
    settings = {}
    if args.paths.is_file():
        settings = json.loads(args.paths.read_text())
        if not isinstance(settings, dict):
            parser.error('Path config must be a JSON object')
    for key in ('system_data', 'run', 'context_script', 'workspace', 'legacy_vault', 'baseline', 'output_dir', 'module_config', 'card_index', 'source_index'):
        override = getattr(args, key)
        if override is not None:
            settings[key] = str(override)
    return args, settings

def required_path(parser, settings, key):
    value = settings.get(key)
    if not isinstance(value, str) or not value or value.startswith('REPLACE_'):
        parser.error('Configure explicit local path: ' + key)
    path = Path(value).expanduser().resolve()
    if not path.exists():
        parser.error('Configured path does not exist: ' + key)
    return path
