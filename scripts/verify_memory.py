#!/usr/bin/env python3
"""Exercise the optional native memory profile with disposable synthetic facts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import queue
import re
import threading
import time


def verify_mcp(repository, state, runtime, project, sha):
    (repository / 'docs/automation').mkdir(parents=True)
    (repository / 'docs/GOAL.md').write_text('Verify synthetic cloud memory and project-bound retrieval.\n')
    (repository / 'docs/automation/STATE.json').write_text(json.dumps({'project_id': project, 'work_branch': 'main',
        'status': 'ACTIVE', 'goal_id': 'synthetic-memory-acceptance', 'code_commit_sha': sha}))
    command = [sys.executable, '-m', 'cloudkit', 'mcp', '--runtime-root', str(runtime),
               '--state-dir', str(state), '--project-root', str(repository), '--project-id', project]
    incoming = queue.Queue()
    with tempfile.TemporaryFile(mode='w+') as errors:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, bufsize=1)
        def read():
            for line in process.stdout:
                try:
                    incoming.put(json.loads(line))
                except ValueError:
                    continue
        threading.Thread(target=read, daemon=True).start()
        sequence = 0
        def send(value):
            process.stdin.write(json.dumps(value) + '\n');process.stdin.flush()
        def request(method, params):
            nonlocal sequence
            sequence += 1;identifier = sequence
            send({'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params})
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                value = incoming.get(timeout=max(.01, deadline - time.monotonic()))
                if value.get('id') != identifier:
                    continue
                assert 'error' not in value
                return value['result']
            raise AssertionError('MCP request timed out')
        def call(name, arguments):
            value = request('tools/call', {'name': name, 'arguments': arguments})
            assert not value.get('isError') and not value.get('is_error')
            if isinstance(value.get('structuredContent'), dict):
                return value['structuredContent']
            return json.loads(next(item['text'] for item in value['content'] if item.get('type') == 'text'))
        try:
            request('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                'clientInfo': {'name': 'cloudkit-acceptance', 'version': '1.0'}})
            send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            names = sorted(item['name'] for item in request('tools/list', {})['tools'])
            assert names == ['memory_read', 'memory_record', 'memory_retrieve', 'memory_status', 'memory_sync']
            status = call('memory_status', {})
            assert status['project_id'] == project and status['active_generation'] > 0
            retrieved = call('memory_retrieve', {'query': 'memoryfixture-record', 'mode': 'text',
                'source_revision': sha, 'required_memory_ids': ['recorded-cloud-fact']})
            assert 'recorded-cloud-fact' in {item['memory_id'] for item in retrieved['candidates']}
            return {'passed': True, 'tools': names, 'actual_initialize_list_status_retrieve': True, 'project_id': project}
        finally:
            try:
                process.stdin.close();process.wait(timeout=10)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait(timeout=5)


def verify_semantic_recall(base, backend, policy, sha):
    """Exercise native cross-language ranking with independent distractors."""
    from cloudkit.runtime.memoryctl import Memory, digest, now, note_text
    project = 'example/cloud-semantic-fixture'
    source = base / 'semantic-source'; source.mkdir()
    facts = {
        'semantic-314159': ('支付操作防重入',
            '支付系统为每个交易请求保存唯一标识和已经成功的执行结果。断电或进程崩溃后，'
            '相同交易再次送达时，直接返回保存的结果，不再从客户账户扣除资金。'),
        'semantic-271828': ('番茄幼苗种植条件',
            '种植番茄幼苗需要充足的阳光、肥沃且排水良好的土壤。定期浇水保持适当湿度，'
            '并给植株留下足够的生长空间，能够促进健康的根系和果实发育。'),
    }
    cases = [
        ('How can a restarted payment service avoid charging the same customer twice?', 'semantic-314159', 'semantic-271828'),
        ('What does a young tomato plant need to thrive outdoors?', 'semantic-271828', 'semantic-314159'),
    ]
    for ident, (title, body) in facts.items():
        metadata = {'memory_id': ident, 'project_id': project, 'category': 'verification',
            'title': title, 'summary': body, 'source_uri': 'https://example.invalid/semantic-fixture',
            'source_revision': sha, 'code_sha': sha, 'status': 'verified', 'content_hash': digest(body),
            'evidence_uri': 'https://example.invalid/semantic-fixture/acceptance',
            'conditions': '仅用于跨语言语义检索的合成测试', 'verified_at': now(),
            'observed_at': now(), 'valid_from': now(), 'valid_to': None, 'supersedes': []}
        (source / (ident + '.md')).write_text(note_text(metadata, body))
    memory = Memory(project, root=base / 'semantic-state', backend=backend)
    memory.govern(policy_path=policy)
    indexed = memory.sync(source, sha)
    assert indexed['new'] == len(facts), indexed
    assert indexed['embedding_count'] >= len(facts), indexed
    checks = []
    for query, expected, distractor in cases:
        # The English questions share no words with either Chinese title/body.
        words = set(re.findall(r'[a-z]+', query.lower()))
        for title, body in facts.values():
            assert words.isdisjoint(re.findall(r'[a-z]+', (title + body).lower()))
        for mode in ('vector', 'hybrid'):
            result = memory.retrieve(query, mode=mode, source_revision=sha)
            candidates = result['candidates']
            ids = [item['memory_id'] for item in candidates]
            assert ids and ids[0] == expected, {'mode': mode, 'query': query, 'expected': expected, 'ranked_ids': ids}
            assert distractor not in ids or ids.index(expected) < ids.index(distractor), ids
            checks.append({'mode': mode, 'query': query, 'expected_memory_id': expected,
                'distractor_memory_id': distractor, 'ranked_memory_ids': ids,
                'scores': {item['memory_id']: item.get('score') for item in candidates},
                'correct_fact_rank': ids.index(expected) + 1,
                'distractor_rank': ids.index(distractor) + 1 if distractor in ids else None,
                'cross_language_no_shared_content_words': True, 'passed': True})
    return {'passed': True, 'scope': 'Native vector and hybrid ranking of synthetic cross-language facts; not broad retrieval-quality certification',
            'initial_index': indexed, 'checks': checks, 'production_recall_limit_changed': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    runtime = args.runtime_root.resolve()
    os.environ['CLOUDKIT_RUNTIME_ROOT'] = str(runtime)
    from cloudkit.runtime.memoryctl import Memory, digest, now, note_text
    import cloudkit.runtime as packaged_runtime
    policy = Path(packaged_runtime.__file__).parent / 'storage-policy.json'
    backend = runtime / 'memory/backend.py'
    with tempfile.TemporaryDirectory(prefix='cloudkit-memory-check-') as temporary:
        base = Path(temporary);source = base / 'source';source.mkdir()
        repository = base / 'project';repository.mkdir()
        subprocess.run(['git', 'init', '-b', 'main', str(repository)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(repository), 'remote', 'add', 'origin', 'https://github.com/example/cloud-memory-fixture.git'], check=True, capture_output=True)
        (repository / 'fixture.txt').write_text('Disposable memory acceptance fixture\n')
        subprocess.run(['git', '-C', str(repository), 'add', '.'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                        'commit', '-m', 'Create synthetic fixture'], check=True, capture_output=True)
        sha = subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'HEAD'], text=True).strip()
        project = 'example/cloud-memory-fixture'
        memory = Memory(project, root=base / 'state', backend=backend)
        memory.govern(policy_path=policy)
        proof = base / 'proof.json'
        proof.write_text(json.dumps({'passed': True, 'code_sha': sha, 'scope': 'Synthetic memory protocol fixture'}))

        def metadata(ident, body):
            return {'memory_id': ident, 'project_id': project, 'category': 'verification',
                    'title': 'memoryfixture 云端记忆验收', 'summary': body, 'source_uri': 'https://example.invalid/fixture',
                    'source_revision': sha, 'code_sha': sha, 'status': 'verified', 'content_hash': digest(body),
                    'evidence_uri': 'https://example.invalid/fixture/acceptance', 'conditions': 'Disposable synthetic functional fixture only',
                    'verified_at': now(), 'observed_at': now(), 'valid_from': now(), 'valid_to': None, 'supersedes': []}

        body = 'memoryfixture-record：云端结算流程先验证金额，再执行持久步骤；恢复时已完成步骤不会重复执行。'
        memory.record(None, metadata('recorded-cloud-fact', body), body, proof)
        changed_body = 'memoryfixture-source：发票金额包含商品总价与税费。'
        (source / 'invoice.md').write_text(note_text(metadata('indexed-source-fact', changed_body), changed_body))
        initial = memory.sync(source, sha)
        assert initial['new'] == 2

        def zero_work(value):
            assert all(value[key] == 0 for key in ['new', 'modified', 'renamed', 'deleted', 'parse_count', 'embedding_count'])

        repeated = memory.sync(source, sha);zero_work(repeated)
        (source / 'invoice.md').rename(source / 'renamed-invoice.md')
        renamed = memory.sync(source, sha)
        assert renamed['renamed'] == 1 and renamed['embedding_count'] == 0
        changed_body += ' 更新后允许折扣，并保留原始币种。'
        (source / 'renamed-invoice.md').write_text(note_text(metadata('indexed-source-fact', changed_body), changed_body))
        modified = memory.sync(source, sha)
        assert modified['modified'] == 1 and modified['parse_count'] == 1
        (source / 'renamed-invoice.md').unlink()
        deleted = memory.sync(source, sha)
        assert deleted['deleted'] == 1
        recalled = memory.retrieve('memoryfixture-record', mode='text', source_revision=sha,
                                   required_memory_ids=['recorded-cloud-fact'])
        identifiers = {item['memory_id'] for item in recalled['candidates']}
        assert 'recorded-cloud-fact' in identifiers and 'indexed-source-fact' not in identifiers
        mcp = verify_mcp(repository, base / 'state', runtime, project, sha)
        first = memory.read('recorded-cloud-fact', session='first-session')
        assert first['body'].rstrip('\n') == body
        assert first['source_content_hash'] == digest(body)
        assert memory.read('recorded-cloud-fact', session='first-session')['body_omitted']
        exported = memory.export(base / 'snapshot.tar.gz')
        recovered = Memory(project, root=base / 'fresh-directory', backend=backend)
        restored = recovered.restore(exported['archive'], exported['sha256'])
        after = recovered.read('recorded-cloud-fact', session='new-session')
        assert after['body'] == first['body']
        assert recovered.status()['storage']['limits'] == memory.status()['storage']['limits']
        assert recovered.status()['storage']['categories'] == memory.status()['storage']['categories']
        after_repeat = recovered.sync(source, sha);zero_work(after_repeat)
        isolated = Memory('example/another-project', root=base / 'fresh-directory', backend=backend)
        try:
            isolated.restore(exported['archive'], exported['sha256'])
        except ValueError:
            partition_isolation = True
        else:
            raise AssertionError('A different project accepted the snapshot')
        semantic = verify_semantic_recall(base, backend, policy, sha)
        installed = subprocess.check_output([str(runtime / 'memory/.venv/bin/python'), '-c',
            'import importlib.metadata as m,json,sys;print(json.dumps({"python":sys.version.split()[0],"basic_memory":m.version("basic-memory"),"fastembed":m.version("fastembed")}))'], text=True)
        result = {'passed': True, 'scope': 'Actual native memory profile and synthetic facts on this cloud machine; not a model request or hosted Runner test',
                  'source_sha': sha, 'installed': json.loads(installed), 'initial': initial, 'repeat': repeated,
                  'renamed': renamed, 'modified': modified, 'deleted': deleted, 'native_recall_passed': True,
                  'same_session_body_deduplicated': True, 'new_session_body_verified': True,
                  'actual_memory_mcp': mcp,
                  'semantic_recall': semantic,
                  'fresh_directory_restore': restored, 'restore_repeat': after_repeat,
                  'project_partition_isolation': partition_isolation,
                  'snapshot_sha256': exported['sha256'], 'storage': recovered.status()['storage']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': True, 'output': str(args.output), 'native_recall_verified': True, 'snapshot_restore_verified': True}))


if __name__ == '__main__':
    main()
