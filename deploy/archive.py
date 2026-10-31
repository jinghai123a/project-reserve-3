#!/usr/bin/env python3
"""Offline local-volume backup/restore; credentials are intentionally excluded."""
import sys
import tarfile
from pathlib import Path

root = Path('/data')
if sys.argv[1] == 'backup':
    with tarfile.open(fileobj=sys.stdout.buffer, mode='w|gz') as archive:
        for name in ('control', 'runs'):
            if (root / name).exists():
                archive.add(root / name, arcname=name)
elif sys.argv[1] == 'restore':
    if any((root / name).exists() and any((root / name).iterdir()) for name in ('control', 'runs')):
        raise SystemExit('Restore requires empty control/runs directories; existing data preserved')
    with tarfile.open(fileobj=sys.stdin.buffer, mode='r|gz') as archive:
        for member in archive:
            if Path(member.name).parts[0] not in ('control', 'runs'):
                raise SystemExit('Archive contains an unexpected top-level path')
            def restricted(item, destination):
                item = tarfile.data_filter(item, destination)
                allowed = root / Path(item.name).parts[0]
                (root / item.name).resolve().relative_to(allowed.resolve())
                if item.issym():
                    (root / item.name).parent.joinpath(item.linkname).resolve().relative_to(allowed.resolve())
                elif item.islnk():
                    (root / item.linkname).resolve().relative_to(allowed.resolve())
                return item
            archive.extract(member, root, filter=restricted)
else:
    raise SystemExit('Expected backup or restore')
