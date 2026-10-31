#!/usr/bin/env python3
"""Install explicit control-repo agent config without replacing existing auth/settings."""
import os
from pathlib import Path
import shutil
import tomllib


def activate(source, target):
    config = source / 'config.toml'
    if not config.is_file():
        return {'activated': False, 'reason': 'control-repo-agent-config-absent'}
    parsed = tomllib.loads(config.read_text())
    # This deployment only imports role topology. Billing, model and permissions
    # stay with the operator's existing Codex account configuration.
    if set(parsed) - {'agents', 'features'}:
        raise ValueError('Control config must contain only agents/features')
    files = [config, *sorted((source / 'agents').glob('*.toml'))]
    for role in files[1:]:
        if set(tomllib.loads(role.read_text())) - {'developer_instructions'}:
            raise ValueError('Role files must contain only developer_instructions')
    for role in parsed.get('agents', {}).values():
        if isinstance(role, dict) and 'config_file' in role:
            relative = Path(role['config_file'])
            if relative.is_absolute() or len(relative.parts) != 2 or relative.parts[0] != 'agents' or relative.suffix != '.toml':
                raise ValueError('Role config_file must reference agents/<role>.toml')
            if source / relative not in files:
                raise ValueError('Referenced role file is missing')
    for path in files:
        destination = target / path.relative_to(source)
        if destination.exists() and destination.read_bytes() != path.read_bytes():
            raise ValueError('Existing Codex configuration preserved: ' + str(destination))
    for path in files:
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    return {'activated': True, 'files': len(files), 'authentication_preserved': True}


if __name__ == '__main__':
    import json
    print(json.dumps(activate(Path('/data/control/.codex'), Path(os.environ['CODEX_HOME']))))
