#!/usr/bin/env python3
"""Reconcile due Git outboxes with RQ; no parallel scheduler or task database."""
import json
import os
from pathlib import Path
import time
import uuid
from cloudkit.runtime import company, continuity, queue_bridge


def reconcile_companies(store, reconcile_goal=company.reconcile):
    """Advance only admitted company goals; one invalid goal never stops peers."""
    _, state = store.read()
    results = []
    for identifier, goal in state.get('coordination', {}).get('goals', {}).items():
        if goal.get('status') == 'DONE' or not goal.get('review'):
            continue
        if any(task.get('dependencies') for task in goal['plan']['tasks']) and not goal.get('workspace'):
            results.append({'goal_id': identifier, 'status': 'WAITING_WORKSPACE'})
            continue
        try:
            result = reconcile_goal(store, identifier, goal.get('workspace'))
            results.append({'goal_id': identifier, 'status': result['status']})
        except (ValueError, RuntimeError, OSError) as error:
            results.append({'goal_id': identifier, 'status': 'BLOCKED', 'error_type': type(error).__name__})
    return results


def reconcile(store, enqueue=queue_bridge.enqueue, clock=continuity.utc):
    deliveries = []
    for identifier in continuity.pending(store, clock):
        def due_outbox(state):
            item = state['autonomy_tasks'][identifier]
            if (item['status'] not in {'READY', 'RETRY_WAIT', 'ACTIVE'}
                    or (item.get('next_wakeup_at') or 0) > clock()
                    or (item.get('lease') or {}).get('expires_at', 0) > clock()):
                return None, False
            if item.get('outbox'):
                return None, True
            item['outbox'] = {'event_id': uuid.uuid4().hex, 'status': 'PENDING'}
            return {}, True
        if store.update(due_outbox, 'Reconcile due Git intent with queue delivery'):
            result = enqueue(str(store.repository), identifier,
                             queue_name=os.environ.get('CLOUDKIT_QUEUE', 'cloudkit'),
                             github_repository=os.environ.get('CLOUDKIT_CONTROL_REPOSITORY'))
            if result.get('error_type'):
                raise RuntimeError('Queue reconciliation failed: ' + result['error_type'])
            deliveries.append(result)
    return deliveries


if __name__ == '__main__':
    from runtime import ready, store
    ready()
    failures = 0
    while True:
        try:
            current = store()
            company_results = reconcile_companies(current)
            if any(item['status'] in {'BLOCKED', 'WAITING_WORKSPACE'} for item in company_results):
                print(json.dumps({'company_goals': company_results}), flush=True)
            results = reconcile(current)
            Path('/data/dispatcher-heartbeat').write_text(str(time.time()))
            if any(item.get('enqueued') for item in results):
                print(json.dumps({'delivered': sum(bool(item.get('enqueued')) for item in results)}), flush=True)
            failures = 0
        except Exception as error:
            failures += 1
            print(json.dumps({'passed': False, 'error_type': type(error).__name__, 'attempt': failures}), flush=True)
            if failures >= 3:
                raise SystemExit(1)
        time.sleep(60)
