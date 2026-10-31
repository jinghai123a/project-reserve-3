#!/usr/bin/env python3
"""Validate rendered Compose JSON without credentials, containers, or networking."""
import json
from pathlib import Path
import sys

config = json.load(sys.stdin)
assert set(config['services']) == {'tailscale'}, 'Networking must be independent of worker/Redis'
service = config['services']['tailscale']
assert service['image'] == 'ghcr.io/tailscale/tailscale:v1.104.2'
for name in ('ports', 'devices', 'cap_add', 'network_mode', 'privileged'):
    assert not service.get(name), 'Unexpected exposure or privilege: ' + name
assert service['environment']['TS_USERSPACE'] == 'true'
assert service['environment']['TS_ACCEPT_DNS'] == 'false'
assert service['environment']['TS_AUTH_ONCE'] == 'true'
assert service['environment']['TS_EXTRA_ARGS'] == '--accept-routes=false --ssh=false --advertise-tags=tag:cloudkit-admin'
assert 'TS_AUTHKEY' not in service['environment'], 'Key must be read at process startup'
assert not service['environment'].get('TS_ROUTES')
here = Path(__file__).resolve().parent
serve = json.loads((here / 'config/serve.json').read_text())
assert serve == {'TCP': {'2222': {'TCPForward': 'host.docker.internal:22'}}}
policy = json.loads((here / 'policy.example.json').read_text())
assert policy['grants'] == [{'src': ['autogroup:admin'], 'dst': ['tag:cloudkit-admin'], 'ip': ['tcp:2222']}]
print('PASS: isolated userspace administration configuration; no live VPN connection asserted')
