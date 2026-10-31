#!/usr/bin/env python3
"""Real Git and subprocess tests for the worker adapter; no model is simulated as real."""
import concurrent.futures
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cloudkit.runtime import continuity as c
from cloudkit.runtime import codex_worker_backend as codex_backend
from cloudkit.runtime import worker_supervisor as w


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL, text=True).strip()


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        # Local pushes also launch receive-pack in the fixture bare repository.
        # Neither side may start background housekeeping that outlives cleanup.
        # Append process-local configuration, preserving any inherited Git config;
        # never change the user's global settings.
        count = int(os.environ.get('GIT_CONFIG_COUNT', '0'))
        configuration = {'gc.auto': '0', 'maintenance.auto': 'false', 'receive.autogc': 'false'}
        environment = {'GIT_CONFIG_COUNT': str(count + len(configuration))}
        for offset, (key, value) in enumerate(configuration.items(), count):
            environment['GIT_CONFIG_KEY_' + str(offset)] = key
            environment['GIT_CONFIG_VALUE_' + str(offset)] = value
        isolated_git = patch.dict(os.environ, environment)
        isolated_git.start()
        self.addCleanup(isolated_git.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix='worker-supervisor-', dir='/tmp')
        self.base = Path(self.temporary.name)
        self.bare = self.base/'remote.git'
        subprocess.run(['git','init','--bare','-q',str(self.bare)],check=True)
        # Local transport strips command-scope config before receive-pack;
        # server-side isolation therefore also needs repository-local settings.
        for key, value in configuration.items():
            git(self.bare, 'config', '--local', key, value)
        self.control = self.base/'control'
        subprocess.run(['git','clone','-q',str(self.bare),str(self.control)],check=True,capture_output=True)
        git(self.control,'switch','-c','automation/state')
        self.original = {'schema_version':1,'status':'IDLE','goal_id':'original',
            'execution_route':'chatgpt-private-runner','memory_partitions':{'untouched':{'revision':4}}}
        (self.control/'STATE.json').write_text(json.dumps(self.original))
        git(self.control,'add','STATE.json');git(self.control,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','control')
        git(self.control,'push','origin','automation/state')
        self.store = c.Store(self.control)
        self.project = self.base/'runner-a'/'project';self.project.mkdir(parents=True)
        git(self.project,'init','-q','-b','main');git(self.project,'remote','add','origin','https://github.com/fixture/project.git')
        (self.project/'result.txt').write_text('initial')
        self.worker = self.project/'worker.py'
        self.worker.write_text('''import json,sys,time
from pathlib import Path
r=json.load(sys.stdin)
if r['kind']=='capability':
 print(json.dumps({'capability':'model-worker','challenge':r['challenge'],'backend_id':r['backend_id'],'model_response':'Explicit command fixture, not a model'}));sys.exit()
p=Path('calls.txt');n=int(p.read_text())+1 if p.exists() else 1;p.write_text(str(n))
mode=sys.argv[1]
if mode=='repair':Path('result.txt').write_text('good' if r.get('previous_acceptance') else 'almost')
elif mode=='good':Path('result.txt').write_text('good')
elif mode=='sleep-good':time.sleep(1);Path('result.txt').write_text('good')
elif mode=='progressive':Path('result.txt').write_text('good' if n>=3 else 'partial-'+str(n))
elif mode=='churn':Path('result.txt').write_text('still-wrong-'+str(n))
elif mode=='milestones':Path('result.txt').write_text(str(n))
elif mode=='corrupt-contract':Path('contract.txt').write_text('corrupted');Path('result.txt').write_text('almost')
elif mode=='vary':print(['alpha','beta','gamma','delta'][(n-1)%4],file=sys.stderr);sys.exit(7)
elif mode=='timeout':time.sleep(5);Path('late.txt').write_text('should not exist')
print(json.dumps({'fixture':True,'request_id':r['request_id']}))
''')
        git(self.project,'add','.');git(self.project,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','project')
        self.plan = {'project_id':'fixture/project','branch':'main','source_sha':git(self.project,'rev-parse','HEAD'),
            'goal':'Produce good result via independent validation','progress_files':['result.txt'],
            'backends':[self.backend('primary','repair')],
            'acceptance':[[sys.executable,'-c',"from pathlib import Path; assert Path('result.txt').read_text()=='good', 'result is not good'"]],
            'acceptance_timeout_seconds':10}

    def tearDown(self):self.temporary.cleanup()

    def backend(self, name, mode):
        return {'id':name,'independence_key':name,'kind':'command_fixture','idempotent':True,
            'command':[sys.executable,'worker.py',mode], 'probe_command':[sys.executable,'worker.py','probe'],
            'timeout_seconds':10,'probe_timeout_seconds':10}

    def register(self):return w.register_worker(self.store,'goal',self.plan)

    def test_bound_workspace_cannot_silently_use_an_isolated_clone(self):
        self.plan['required_project_root'] = str(self.project.resolve())
        self.plan['backends'] = [self.backend('primary', 'good')]
        self.register()
        result = w.run_round(self.store, 'goal', 'missing-root', 'owner')
        self.assertFalse(result['claimed'])
        self.assertEqual(result['reason'], 'bound-workspace-required')
        other = self.fresh()
        result = w.run_round(self.store, 'goal', 'wrong-root', 'owner', other)
        self.assertFalse(result['claimed'])
        self.assertFalse((self.project / 'calls.txt').exists())
        self.assertFalse(self.store.read()[1]['autonomy_tasks']['goal'].get('lease'))
        self.assertEqual(self.tick('correct-root')['status'], 'DONE')

    def test_changed_dependency_blocks_worker_before_side_effects(self):
        (self.project / 'contract.txt').write_text('reviewed interface')
        self.plan['required_project_root'] = str(self.project.resolve())
        self.plan['required_input_hashes'] = w.e.file_hashes(self.project, ['contract.txt'])
        self.plan['backends'] = [self.backend('primary', 'good')]
        self.register()
        (self.project / 'contract.txt').write_text('changed interface')
        result = self.tick('stale-input')
        self.assertFalse(result['claimed'])
        self.assertEqual(result['reason'], 'dependency-artifacts-changed')
        self.assertFalse((self.project / 'calls.txt').exists())
        (self.project / 'contract.txt').write_text('reviewed interface')
        self.assertEqual(self.tick('restored-input')['status'], 'DONE')

    def bind_contract(self, mode='good'):
        (self.project/'contract.txt').write_text('reviewed interface')
        self.plan['required_project_root'] = str(self.project.resolve())
        self.plan['required_input_hashes'] = w.e.file_hashes(self.project, ['contract.txt'])
        self.plan['backends'] = [self.backend('primary', mode)]
        self.register()

    def test_live_lease_precedes_wrong_root_and_changed_input_mutations(self):
        self.bind_contract()
        def leased(state):
            state['autonomy_tasks']['goal'].update(status='ACTIVE',
                lease={'owner':'active-owner','event_id':'active-event','expires_at':c.utc()+600})
            return {}, None
        self.store.update(leased)
        before=self.store.read()
        wrong=self.fresh();(self.project/'contract.txt').write_text('changed during active work')
        for root in (wrong,self.project):
            result=self.tick('racing-wakeup',root)
            self.assertEqual(result['reason'],'live-lease',result)
            self.assertEqual(self.store.read(),before)

    def test_duplicate_event_precedes_binding_mutations(self):
        self.bind_contract('repair')
        self.assertEqual(self.tick('already-processed')['status'],'READY')
        (self.project/'contract.txt').write_text('changed after processed event')
        before=self.store.read()
        result=self.tick('already-processed')
        self.assertEqual(result['reason'],'duplicate-event',result)
        self.assertEqual(self.store.read(),before)

    def test_done_task_wrong_workspace_request_does_not_reopen(self):
        self.bind_contract()
        self.assertEqual(self.tick('finished')['status'],'DONE')
        before=self.store.read()
        for root in (self.fresh(),None):
            result=w.run_round(self.store,'goal','wrong-root-after-done','owner',root)
            self.assertEqual(result['reason'],'DONE',result)
            self.assertFalse(result['claimed'])
            self.assertEqual(self.store.read(),before)

    def test_input_changed_between_claim_and_lock_is_blocked_with_evidence(self):
        self.bind_contract()
        original=w.fcntl.flock
        def changed_before_lock(fd,operation):
            (self.project/'contract.txt').write_text('changed between claim and lock')
            return original(fd,operation)
        with patch.object(w.fcntl,'flock',side_effect=changed_before_lock),patch.object(w,'_process') as process:
            result=self.tick('input-race')
        self.assertTrue(result['claimed'])
        self.assertEqual(result['status'],'WAITING_CAPABILITY',result)
        self.assertEqual(result['reason'],'dependency-artifacts-changed')
        process.assert_not_called()
        parent,state=self.store.read();task=state['autonomy_tasks']['goal']
        self.assertIsNone(task['lease']);self.assertIsNone(task['outbox'])
        proof=json.loads(self.store.blob(parent,'autonomy/goal/worker-runs/input-race/binding-failure.json'))
        self.assertEqual(proof['check_phase'],'locked-after-restore')
        self.assertFalse(proof['model_invocation_attempted'])
        (self.project/'contract.txt').write_text('reviewed interface')
        self.assertEqual(self.tick('inputs-restored')['status'],'DONE')

    def test_restored_snapshot_cannot_bypass_dependency_hash_guard(self):
        self.bind_contract('corrupt-contract')
        self.assertEqual(self.tick('first-round')['status'],'READY')
        self.assertEqual((self.project/'contract.txt').read_text(),'corrupted')
        (self.project/'contract.txt').write_text('reviewed interface')
        shutil.rmtree(self.project.parent/'.cloudkit-worker-goal')
        with patch.object(w,'_process') as process:
            result=self.tick('restore-round')
        self.assertTrue(result['claimed'])
        self.assertEqual(result['status'],'WAITING_CAPABILITY',result)
        self.assertEqual(result['reason'],'dependency-artifacts-changed')
        process.assert_not_called()
        self.assertEqual((self.project/'calls.txt').read_text(),'1')
        self.assertEqual((self.project/'contract.txt').read_text(),'corrupted')
        parent,state=self.store.read()
        self.assertIsNone(state['autonomy_tasks']['goal']['lease'])
        proof=json.loads(self.store.blob(parent,'autonomy/goal/worker-runs/restore-round/binding-failure.json'))
        self.assertEqual(proof['check_phase'],'locked-after-restore')

    def tick(self,event='one',root=None,store=None):return w.run_round(store or self.store,'goal',event,'owner-'+event,root or self.project)
    def fresh(self):
        new=self.base/'runner-b'/'project';new.parent.mkdir(exist_ok=True)
        subprocess.run(['git','clone','-q',str(self.project),str(new)],check=True,capture_output=True)
        git(new,'remote','set-url','origin','https://github.com/fixture/project.git')
        return new

    def test_failed_acceptance_drives_real_repair_and_done_stops(self):
        self.register();first=self.tick();self.assertEqual(first['status'],'READY',first)
        self.assertFalse(first['passed']);self.assertEqual(first['worker']['returncode'],0)
        second=self.tick('two');self.assertEqual(second['status'],'DONE',second)
        self.assertTrue(second['passed']);self.assertFalse(second['actual_model_success_verified'])
        self.assertEqual((self.project/'calls.txt').read_text(),'2')
        self.assertEqual(self.tick('three')['reason'],'DONE')
        _,state=self.store.read()
        for key,value in self.original.items():self.assertEqual(state[key],value)
        self.assertIsNone(state['autonomy_tasks']['goal']['outbox'])

    def test_existing_continuity_entrypoint_dispatches_worker_and_acceptance(self):
        self.plan['backends'] = [self.backend('primary', 'good')]
        self.register()
        result = c.run_round(self.store, 'goal', 'dispatcher-event', 'dispatcher-owner', self.project)
        self.assertEqual(result['status'], 'DONE', result)
        self.assertTrue(result['passed'])
        self.assertFalse(result['actual_model_success_verified'])
        self.assertEqual((self.project/'calls.txt').read_text(), '1')
        self.assertEqual(c.run_round(self.store, 'goal', 'dispatcher-again', 'dispatcher-owner', self.project)['reason'], 'DONE')

    def test_cross_process_and_cross_directory_restore(self):
        self.register();self.tick('before')
        new=self.fresh();shutil.rmtree(self.project.parent)
        result=subprocess.run([sys.executable,str(Path(w.__file__)),'--repository',str(self.control),'tick','--task-id','goal','--project-root',str(new),'--event-id','after'],check=True,capture_output=True,text=True)
        report=json.loads(result.stdout);self.assertEqual(report['status'],'DONE',report)
        self.assertEqual((new/'calls.txt').read_text(),'2')
        self.assertEqual((new/'result.txt').read_text(),'good')

    def test_two_equivalent_acceptance_failures_trip_and_fallback(self):
        self.plan['backends']=[self.backend('bad','noop'),self.backend('independent','good')]
        self.register();self.assertEqual(self.tick('one')['status'],'READY');self.tick('two')
        third=self.tick('three');self.assertEqual(third['status'],'DONE',third)
        parent,state=self.store.read();routes=state['autonomy_tasks']['goal']['worker_supervisor']['routes']
        self.assertEqual(routes['bad']['attempts'],2);self.assertTrue(routes['bad']['tripped'])
        archive=json.loads(self.store.blob(parent,'autonomy/goal/circuits/bad.json'))
        self.assertEqual(archive['trip_reason'],'equivalent-error-twice')

    def test_three_distinct_errors_without_progress_switch_route(self):
        self.plan['backends']=[self.backend('varying','vary'),self.backend('fallback','good')]
        self.register()
        for i in range(3):self.assertEqual(self.tick('fail-'+str(i))['status'],'READY')
        _,state=self.store.read();stats=state['autonomy_tasks']['goal']['worker_supervisor']['routes']['varying']
        self.assertEqual(stats['attempts'],3);self.assertEqual(stats['no_progress'],3);self.assertTrue(stats['tripped'])
        self.assertEqual(self.tick('recover')['status'],'DONE')

    def test_repeated_acceptance_error_allows_three_round_progressive_repair(self):
        self.plan['backends']=[self.backend('primary','progressive')]
        self.register()
        first=self.tick('partial-one');second=self.tick('partial-two')
        for report in (first,second):
            self.assertEqual(report['status'],'READY',report)
            self.assertTrue(report['progress']['declared_files_changed'])
            self.assertFalse(report['progress']['verified_acceptance_progress'])
        _,state=self.store.read();worker=state['autonomy_tasks']['goal']['worker_supervisor']
        self.assertFalse(worker['routes']['primary']['tripped'])
        self.assertEqual(worker['last_failure']['diagnosis'],'acceptance-stalled-despite-file-activity')
        self.assertEqual(worker['last_failure']['remaining_unverified_rounds'],1)
        third=self.tick('finished');self.assertEqual(third['status'],'DONE',third)
        self.assertEqual((self.project/'calls.txt').read_text(),'3')

    def test_declared_file_churn_cannot_reset_verified_progress_budget(self):
        self.plan['backends']=[self.backend('churn','churn'),self.backend('fallback','good')]
        self.register()
        for number in range(3):
            report=self.tick('churn-'+str(number))
            self.assertTrue(report['progress']['declared_files_changed'])
            self.assertFalse(report['progress']['verified_acceptance_progress'])
            self.assertEqual(report['progress']['unverified_rounds'],number+1)
        parent,state=self.store.read();stats=state['autonomy_tasks']['goal']['worker_supervisor']['routes']['churn']
        self.assertTrue(stats['tripped'])
        self.assertEqual(stats['trip_reason'],'unverified-progress-budget-exhausted')
        archive=json.loads(self.store.blob(parent,'autonomy/goal/circuits/churn.json'))
        self.assertEqual(archive['no_progress'],3)
        self.assertEqual(self.tick('fallback')['status'],'DONE')

    def test_unrelated_file_activity_is_not_declared_or_verified_progress(self):
        self.plan['backends']=[self.backend('primary','noop')]
        self.register();first=self.tick('one');self.tick('two')
        self.assertTrue(first['progress']['workspace_files_changed'])  # calls.txt only
        self.assertFalse(first['progress']['declared_files_changed'])
        self.assertFalse(first['progress']['verified_acceptance_progress'])
        _,state=self.store.read()
        self.assertTrue(state['autonomy_tasks']['goal']['worker_supervisor']['routes']['primary']['tripped'])

    def test_independently_passing_milestones_extend_repair_budget(self):
        self.plan['backends']=[self.backend('primary','milestones')]
        self.plan['acceptance']=[[sys.executable,'-c',
            "from pathlib import Path; assert int(Path('result.txt').read_text()) >= "+str(number)] for number in range(1,5)]
        self.register()
        for number in range(1,5):
            report=self.tick('milestone-'+str(number))
            self.assertEqual(report['status'],'DONE' if number==4 else 'READY',report)
            self.assertTrue(report['progress']['verified_acceptance_progress'])
            self.assertEqual(report['progress']['passed_checks'],number)
            self.assertEqual(report['progress']['unverified_rounds'],0)

    def test_transient_probe_half_open_retry_and_exponential_cooldown(self):
        self.plan['backends']=[self.backend('primary','good')]
        probe=self.project/'probe.py'
        probe.write_text("""import json,sys
from pathlib import Path
r=json.load(sys.stdin)
p=Path('probe-count.txt');n=int(p.read_text())+1 if p.exists() else 1;p.write_text(str(n))
if n<3:
 print(json.dumps({'authentication_configuration_preserved':True,'failure':'codex-process-failed','stderr':'HTTP 503 temporarily unavailable','challenge':r['challenge'],'backend_id':r['backend_id']}));sys.exit(1)
print(json.dumps({'capability':'model-worker','challenge':r['challenge'],'backend_id':r['backend_id'],'model_response':'Explicit fixture'}))
""")
        self.plan['backends'][0]['probe_command']=[sys.executable,'probe.py']
        self.register();clock=[1000]
        def tick(event):return w.run_round(self.store,'goal',event,'owner',self.project,clock=lambda:clock[0])
        first=tick('outage');self.assertEqual(first['status'],'RETRY_WAIT',first)
        self.assertEqual(first['retry_at'],1060)
        _,state=self.store.read();task=state['autonomy_tasks']['goal'];stats=task['worker_supervisor']['routes']['primary']
        self.assertFalse(stats['tripped']);self.assertEqual(stats['capability_state'],'cooldown')
        self.assertEqual(stats['capability_probe']['failure_class'],'transient')
        self.assertEqual(task['next_wakeup_at'],1060);self.assertIsNone(task['outbox'])
        self.assertEqual(c.pending(self.store,clock=lambda:1059),[])
        self.assertEqual(c.pending(self.store,clock=lambda:1060),['goal'])
        clock[0]=1059
        early=tick('too-early');self.assertFalse(early['claimed']);self.assertEqual(early['reason'],'capability-cooldown')
        self.assertEqual((self.project/'probe-count.txt').read_text(),'1')
        self.assertFalse((self.project/'calls.txt').exists())
        clock[0]=1060
        second=tick('still-down');self.assertEqual(second['retry_at'],1180)
        self.assertEqual((self.project/'probe-count.txt').read_text(),'2')
        clock[0]=1180
        new=self.fresh();shutil.rmtree(self.project.parent)
        recovered=w.run_round(self.store,'goal','recovered','owner',new,clock=lambda:clock[0])
        self.assertEqual(recovered['status'],'DONE',recovered)
        self.assertEqual((new/'probe-count.txt').read_text(),'3')
        self.assertEqual((new/'calls.txt').read_text(),'1')
        _,state=self.store.read();stats=state['autonomy_tasks']['goal']['worker_supervisor']['routes']['primary']
        self.assertEqual(stats['capability_state'],'closed');self.assertNotIn('retry_at',stats)
        self.assertEqual(stats['probe_failures'],0)

    def test_direct_run_yields_after_transient_probe_instead_of_spinning(self):
        self.plan['backends'][0]['probe_command']=[sys.executable,'-c',
            "import sys;print('HTTP 429 rate limit',file=sys.stderr);sys.exit(1)"]
        self.register()
        result=w.run_until_terminal(self.store,'goal',self.project,max_rounds=50)
        self.assertEqual(result['status'],'RETRY_WAIT',result)
        self.assertTrue(result['claimed']);self.assertEqual(result['round'],1)
        self.assertGreater(result['retry_at'],c.utc())
        _,state=self.store.read();worker=state['autonomy_tasks']['goal']['worker_supervisor']
        self.assertEqual(worker['round'],1);self.assertEqual(worker['routes']['primary']['probe_failures'],1)
        self.assertFalse((self.project/'calls.txt').exists())

    def test_transient_probe_uses_available_independent_route_immediately(self):
        bad=self.backend('outage','noop')
        bad['probe_command']=[sys.executable,'-c',"import sys;print('connection reset by peer',file=sys.stderr);sys.exit(1)"]
        self.plan['backends']=[bad,self.backend('fallback','good')]
        self.register();report=self.tick()
        self.assertEqual(report['status'],'DONE',report)
        self.assertEqual(report['worker']['backend'],'fallback')
        _,state=self.store.read()
        self.assertFalse(state['autonomy_tasks']['goal']['worker_supervisor']['routes']['outage']['tripped'])

    def test_probe_failure_classification_does_not_retry_auth_or_protocol(self):
        for result,expected in [
            ((124,'','','timeout-process-group-terminated'),'transient'),
            ((1,'','HTTP 429 rate limit',None),'transient'),
            ((1,'','HTTP 401 authentication failed; retry later',None),'authentication'),
            ((127,'','command not found',None),'configuration'),
            ((0,'version 503','',''),'protocol'),
            ((1,'','unsupported feature',None),'unclassified')]:
            with self.subTest(result=result):self.assertEqual(w._probe_failure(result),expected)

    def test_real_adapter_receipt_metadata_does_not_override_failure_diagnostics(self):
        request={'kind':'capability','backend_id':'authentication-401','challenge':'authentication-403'}
        for cli_result,expected in [
            ((124,'','',True),'transient'),
            ((1,'','HTTP 429 rate limit',False),'transient'),
            ((1,'','HTTP 503 temporarily unavailable',False),'transient'),
            ((1,'','HTTP 401 authentication failed',False),'authentication'),
            ((1,'','invalid API key',False),'authentication'),
            ((0,'','',False),'unclassified')]:
            with self.subTest(cli_result=cli_result),patch.object(codex_backend,'invoke_cli',return_value=cli_result):
                receipt,exit_code=codex_backend.run(request,['codex'],timeout=10,cwd=self.project)
                self.assertTrue(receipt['authentication_configuration_preserved'])
                self.assertEqual(w._probe_failure((exit_code,json.dumps(receipt),'',None)),expected)

    def test_probe_diagnostics_ignore_structured_metadata_and_nonce(self):
        receipt={'authentication_configuration_preserved':True,'challenge':'401',
                 'proof':{'message':'HTTP 503 authentication'},'model_response':'timeout',
                 'failure':'model-nonce-proof-missing'}
        self.assertEqual(w._probe_failure((65,json.dumps(receipt),'',None)),'unclassified')
        receipt['message']='HTTP 429 too many requests'
        self.assertEqual(w._probe_failure((65,json.dumps(receipt),'',None)),'transient')

    def test_invalid_retry_and_progress_budgets_are_rejected(self):
        for value in (0,-1,True,float('inf'),float('nan')):
            plan=copy.deepcopy(self.plan);plan['backends'][0]['probe_retry_seconds']=value
            with self.subTest(value=value),self.assertRaises(ValueError):w.validate(plan)
        for value in (2,51,True,3.5):
            plan=copy.deepcopy(self.plan);plan['max_unverified_rounds']=value
            with self.subTest(value=value),self.assertRaises(ValueError):w.validate(plan)

    def test_duplicate_event_does_not_execute_worker(self):
        self.register();self.tick('same');again=self.tick('same')
        self.assertEqual(again['reason'],'duplicate-event');self.assertEqual((self.project/'calls.txt').read_text(),'1')

    def test_two_control_clones_observe_one_live_lease(self):
        self.plan['backends']=[self.backend('primary','sleep-good')];self.register()
        other=self.base/'other-control';subprocess.run(['git','clone','-q','--branch','automation/state',str(self.bare),str(other)],check=True,capture_output=True)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            a=pool.submit(self.tick,'first');b=pool.submit(self.tick,'second',None,c.Store(other));results=[a.result(),b.result()]
        self.assertEqual(sum(x.get('status')=='DONE' for x in results),1,results)
        self.assertEqual(sum(x.get('reason')=='live-lease' for x in results),1,results)
        self.assertEqual((self.project/'calls.txt').read_text(),'1')

    def test_completed_worker_result_resumes_acceptance_without_rerun(self):
        self.plan['backends']=[self.backend('primary','good')];self.register()
        with patch.object(w.e.Execution,'run',side_effect=RuntimeError('synthetic verifier infrastructure outage')):
            first=self.tick('interrupted')
        self.assertEqual(first['status'],'RETRY_WAIT',first)
        new=self.fresh();shutil.rmtree(self.project.parent)
        second=self.tick('resume',new)
        self.assertEqual(second['status'],'DONE',second)
        self.assertTrue(second['worker_result_resumed_without_reexecution'])
        self.assertEqual((new/'calls.txt').read_text(),'1')
        self.assertFalse(second['model_invocation_attempted'])
        _,state=self.store.read()
        self.assertEqual(state['autonomy_tasks']['goal']['worker_supervisor']['routes']['primary']['attempts'],1)

    def test_version_output_is_not_a_model_capability(self):
        self.plan['backends'][0]['probe_command']=[sys.executable,'--version'];self.register()
        result=self.tick();self.assertEqual(result['status'],'WAITING_CAPABILITY')
        self.assertFalse((self.project/'calls.txt').exists());self.assertFalse(result['actual_model_success_verified'])
        self.assertEqual(c.pending(self.store),[])
        process=subprocess.run([sys.executable,str(Path(w.__file__)),'--repository',str(self.control),'tick','--task-id','goal','--project-root',str(self.project),'--event-id','cli-unavailable'],capture_output=True,text=True)
        self.assertEqual(process.returncode,1)
        self.assertEqual(json.loads(process.stdout)['status'],'WAITING_CAPABILITY')

    def test_bad_project_identity_releases_lease_and_archives_failure(self):
        self.register();git(self.project,'remote','set-url','origin','https://github.com/fixture/wrong.git')
        result=self.tick();self.assertEqual(result['status'],'BLOCKED',result)
        parent,state=self.store.read();self.assertIsNone(state['autonomy_tasks']['goal']['lease'])
        proof=json.loads(self.store.blob(parent,'autonomy/goal/worker-runs/one/setup-failure.json'))
        self.assertEqual(proof['status'],'BLOCKED')

    def test_direct_run_advances_without_round_confirmation(self):
        self.register();result=w.run_until_terminal(self.store,'goal',self.project,max_rounds=4)
        self.assertEqual(result['status'],'DONE',result);self.assertEqual((self.project/'calls.txt').read_text(),'2')

    def test_init_control_without_source_history_and_preserve_existing(self):
        bare=self.base/'new-private.git';subprocess.run(['git','init','--bare','-q',str(bare)],check=True)
        checkout=self.base/'new-private';subprocess.run(['git','clone','-q',str(bare),str(checkout)],check=True,capture_output=True)
        store=c.Store(checkout)
        result=w.init_control(store,'fixture/toolbox',self.plan['source_sha'])
        self.assertTrue(result['initialized'])
        parent,state=store.read();self.assertTrue(state['continuous_execution_enabled'])
        again=w.init_control(store,'fixture/different','a'*40,'other')
        self.assertFalse(again['initialized']);self.assertTrue(again['existing_state_preserved'])
        other_parent,unchanged=store.read();self.assertEqual(parent,other_parent);self.assertEqual(state,unchanged)

    def test_initial_source_sha_drift_is_not_silently_accepted(self):
        self.register();(self.project/'extra.txt').write_text('changed')
        git(self.project,'add','extra.txt');git(self.project,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','new head')
        result=self.tick();self.assertEqual(result['status'],'BLOCKED',result)
        self.assertFalse((self.project/'calls.txt').exists())

    def test_capability_cache_rechecks_changed_executable_script(self):
        self.plan['backends']=[self.backend('primary','good')]
        self.plan['backends'][0]['probe_command']=[sys.executable,'probe.py']
        probe=self.project/'probe.py';probe.write_text("print('version only')")
        self.register();first=self.tick('bad-probe');self.assertEqual(first['status'],'WAITING_CAPABILITY',first)
        probe.write_text("import json,sys;r=json.load(sys.stdin);print(json.dumps({'capability':'model-worker','challenge':r['challenge'],'backend_id':r['backend_id'],'model_response':'Explicit fixture'}))")
        second=self.tick('changed-probe');self.assertEqual(second['status'],'DONE',second)
        _,state=self.store.read();stats=state['autonomy_tasks']['goal']['worker_supervisor']['routes']['primary']
        self.assertEqual(len(stats['history']),1);self.assertTrue(stats['history'][0]['tripped'])

    def test_changed_done_inputs_revalidate_before_calling_worker(self):
        self.plan['backends']=[self.backend('primary','good')];self.register()
        self.assertEqual(self.tick('complete')['status'],'DONE')
        (self.project/'result.txt').write_text('regressed')
        changed=self.tick('changed');self.assertEqual(changed['status'],'READY',changed)
        self.assertFalse(changed['model_invocation_attempted']);self.assertEqual((self.project/'calls.txt').read_text(),'1')
        fixed=self.tick('repair');self.assertEqual(fixed['status'],'DONE',fixed)
        self.assertEqual((self.project/'calls.txt').read_text(),'2')

    def test_internal_symlink_cannot_alias_excluded_file(self):
        self.plan['backends']=[self.backend('primary','good')]
        (self.project/'.env').write_text('SYNTHETIC_DO_NOT_ARCHIVE=plain_fixture_password')
        (self.project/'settings.py').symlink_to('.env')
        self.register();result=self.tick();self.assertEqual(result['status'],'RETRY_WAIT',result)
        self.assertFalse((self.project/'calls.txt').exists())
        parent,state=self.store.read()
        material=self.store.blob(parent,'STATE.json')
        self.assertNotIn(b'plain_fixture_password',material)

    def test_malformed_probe_json_switches_independent_route(self):
        bad=self.backend('bad','noop');bad['probe_command']=[sys.executable,'-c',"print('[]')"]
        self.plan['backends']=[bad,self.backend('good','good')];self.register()
        result=self.tick();self.assertEqual(result['status'],'DONE',result)
        _,state=self.store.read();stats=state['autonomy_tasks']['goal']['worker_supervisor']['routes']['bad']
        self.assertTrue(stats['tripped']);self.assertEqual(stats['attempts'],0)

    def test_repeated_verifier_infrastructure_failure_trips_and_stops(self):
        self.plan['backends']=[self.backend('primary','good')];self.register()
        with patch.object(w.e.Execution,'run',side_effect=OSError('synthetic persistent verifier filesystem failure')):
            first=self.tick('failure-one');second=self.tick('failure-two')
        self.assertEqual(first['status'],'RETRY_WAIT',first)
        self.assertEqual(second['status'],'BLOCKED',second)
        self.assertTrue(second['infrastructure_circuit_tripped'])
        self.assertEqual(self.tick('not-again')['reason'],'BLOCKED')
        self.assertEqual((self.project/'calls.txt').read_text(),'1')
        parent,state=self.store.read()
        circuit=json.loads(self.store.blob(parent,'autonomy/goal/circuits/infrastructure.json'))
        self.assertEqual(circuit['attempts'],2);self.assertTrue(circuit['pending_result_preserved'])

    def test_fixture_push_and_fetch_do_not_launch_background_housekeeping(self):
        # Client overrides must still lose to process-local isolation; the
        # receive-pack side separately uses the fixture's bare-repo settings.
        git(self.control, 'config', '--local', 'gc.auto', '1')
        git(self.control, 'config', '--local', 'maintenance.auto', 'true')
        git(self.control, 'config', '--local', 'receive.autogc', 'true')
        trace = self.base / 'git-process-trace.jsonl'
        self.plan['backends'] = [self.backend('primary', 'good')]
        with patch.dict(os.environ, {'GIT_TRACE2_EVENT': str(trace)}):
            self.register()
            self.assertEqual(self.tick('isolated-git')['status'], 'DONE')
        children = [event['argv'] for line in trace.read_text().splitlines()
                    if (event := json.loads(line)).get('event') == 'child_start']
        self.assertTrue(any('receive-pack' in ' '.join(argv) for argv in children))
        self.assertFalse([argv for argv in children if 'gc' in argv or 'maintenance' in argv], children)

    def test_different_goals_cannot_write_the_same_workspace_concurrently(self):
        self.plan['backends']=[self.backend('primary','sleep-good')];self.register()
        w.register_worker(self.store,'other-goal',self.plan)
        other=self.base/'other-control';subprocess.run(['git','clone','-q','--branch','automation/state',str(self.bare),str(other)],check=True,capture_output=True)
        started=threading.Event();original=w._process
        def observed(engine,argv,request,timeout):
            if request['kind']=='work':started.set()
            return original(engine,argv,request,timeout)
        with patch.object(w,'_process',side_effect=observed),concurrent.futures.ThreadPoolExecutor(2) as pool:
            first=pool.submit(self.tick,'write-one')
            self.assertTrue(started.wait(5))
            second=pool.submit(w.run_round,c.Store(other),'other-goal','write-two','other-owner',self.project)
            blocked=second.result();finished=first.result()
        self.assertEqual(blocked['reason'],'workspace-busy',blocked)
        self.assertEqual(finished['status'],'DONE',finished)
        self.assertEqual((self.project/'calls.txt').read_text(),'1')

    def test_automatic_runner_clone_and_checkpoint_resume(self):
        base=self.base/'autonomy-runs';base.mkdir()
        with tempfile.TemporaryDirectory(prefix='worker-auto-',dir=base) as directory:
            identifier=Path(directory).name
            w.register_worker(self.store,identifier,self.plan)
            original=self.store.git;clones=[]
            def local_fixture_clone(*args,**kwargs):
                if args and args[0]=='clone':
                    clones.append(args[-2])
                    return subprocess.check_output(['git','clone','--no-hardlinks',str(self.project),args[-1]],stderr=subprocess.DEVNULL)
                return original(*args,**kwargs)
            with patch.dict('os.environ', {'CLOUDKIT_RUNS_ROOT': str(base)}), patch.object(self.store,'git',side_effect=local_fixture_clone):
                result=w.run_until_terminal(self.store,identifier,None,max_rounds=4)
            root=Path(directory)/'project'
            self.assertEqual(result['status'],'DONE',result)
            self.assertEqual(clones,['https://github.com/fixture/project.git'])
            self.assertEqual((root/'calls.txt').read_text(),'2')
            self.assertEqual(git(root,'remote','get-url','origin'),'https://github.com/fixture/project.git')

    def test_dispatcher_runs_one_worker_boundary_and_wakes_remaining_task(self):
        self.plan['backends']=[self.backend('fixture','good')]
        w.register_worker(self.store,'first-worker',self.plan)
        w.register_worker(self.store,'second-worker',self.plan)
        runs=self.base/'main-runner';calls=[];fixture_project=self.project
        original=c.Store.git
        def fixture_clone(store,*args,**kwargs):
            if args and args[0]=='clone':
                self.assertEqual(args[-2],'https://github.com/fixture/project.git')
                return subprocess.check_output(['git','clone','--no-hardlinks',str(fixture_project),args[-1]],stderr=subprocess.DEVNULL)
            return original(store,*args,**kwargs)
        class DispatchTransportFixture:
            def __init__(self,repository):self.repository=repository
            def dispatch(self,ref,task_id,event_id):
                calls.append({'repository':self.repository,'ref':ref,'task_id':task_id,'event_id':event_id})
                return {'accepted':True}
        argv=['continuity.py','--repository',str(self.control),'--github-repository','fixture/control',
              'tick','--event-id','one-runner','--owner','runner-owner']
        with patch.dict('os.environ',{'CLOUDKIT_RUNS_ROOT':str(runs)}),patch.object(c.Store,'git',fixture_clone),\
             patch.object(c,'GitHub',DispatchTransportFixture),patch.object(sys,'argv',argv),patch('sys.stdout',new_callable=io.StringIO) as output:
            code=c.main()
        self.assertEqual(code,0)
        report=json.loads(output.getvalue())
        self.assertEqual(report['scope'],'worker-and-command-continuation')
        self.assertFalse(report['model_called'])
        actual=[item for item in report['results'] if item.get('claimed')]
        self.assertEqual(len(actual),1)
        self.assertEqual(actual[0]['status'],'DONE')
        self.assertFalse(actual[0]['actual_model_success_verified'])
        self.assertEqual((runs/'first-worker/project/calls.txt').read_text(),'1')
        self.assertFalse((runs/'second-worker/project').exists())
        _,state=self.store.read();tasks=state['autonomy_tasks']
        self.assertEqual(tasks['first-worker']['status'],'DONE')
        self.assertEqual(tasks['second-worker']['status'],'READY')
        self.assertEqual(tasks['second-worker']['outbox']['status'],'SENT')
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['event_id'],tasks['second-worker']['outbox']['event_id'])


if __name__=='__main__':unittest.main(verbosity=2)
