"""Gate collector firmware enrolment, endpoint simulator and offline rollout coordinator.

The endpoint simulator (``invoke``) models one real collector.  The coordinator
(``run_rollout``) is resumable: intent is persisted before every dispatch, endpoint
answers and live device state are recorded separately, and a restart reconciles the
two instead of guessing.  Planned versions are never reported as installed; only the
live row in ``gate_firmware_device`` is evidence of an actual version.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .store import connect

CONTRACTS = ('atomic_all_gates', 'zone_isolated')
TERMINAL_STEP_STATUSES = ('confirmed', 'rejected', 'manual', 'skipped')
MAX_ATTEMPTS = 3


class CrashSimulated(RuntimeError):
    """Raised by a harness to model a coordinator process death."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _decode(value, default=None):
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _operation_id(case_id, contract_id, device, phase):
    """Stable per plan, device and phase: a rerun always names the same operation."""
    return hashlib.sha256(f'{case_id}|{contract_id}|{device}|{phase}'.encode('utf-8')).hexdigest()


def _contract_map(doc):
    contracts = {}
    for item in doc.get('contracts') or []:
        if isinstance(item, dict) and item.get('id'):
            contracts[item['id']] = {'id': item['id'], 'authorized_by': item.get('authorized_by')}
    for contract_id in CONTRACTS:
        contract = contracts.get(contract_id)
        if not contract or not contract.get('authorized_by'):
            raise ValueError('gate_firmware_contract_unauthorized')
    return contracts


# ---------------------------------------------------------------------------
# Frozen case, evidence and live device state
# ---------------------------------------------------------------------------

def register(db_path, case_id, fixture):
    path = Path(fixture).resolve()
    raw = path.read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('devices') or not doc.get('target_version'):
        raise ValueError('bad_gate_firmware_case')
    contracts = _contract_map(doc)
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        existing = db.execute('SELECT digest FROM gate_firmware_case WHERE id=?', (case_id,)).fetchone()
        if existing:
            if existing['digest'] != digest:
                raise ValueError('gate_firmware_case_conflict')
            return {'case_id': case_id, 'digest': digest, 'devices': len(doc['devices'])}
        with db:
            observed = []
            for device in doc['devices']:
                for key in ('id', 'gate', 'cell', 'current_version'):
                    if not device.get(key):
                        raise ValueError('bad_gate_firmware_case')
                if not db.execute('SELECT 1 FROM gate_map WHERE gate=?', (device['gate'],)).fetchone():
                    raise ValueError('firmware_gate_missing')
                event = db.execute(
                    'SELECT e.id,e.device,e.gate,e.sha256,s.sha256 AS source_sha256 '
                    'FROM event e JOIN source s ON s.id=e.source_id '
                    'WHERE e.device=? AND e.gate=? ORDER BY e.id LIMIT 1',
                    (device['id'], device['gate']),
                ).fetchone()
                if not event:
                    raise ValueError('firmware_device_evidence_missing')
                observed.append(dict(event))
                old = db.execute(
                    'SELECT gate,cell,version,revision FROM gate_firmware_device WHERE id=?',
                    (device['id'],),
                ).fetchone()
                expected = {
                    'gate': device['gate'],
                    'cell': device['cell'],
                    'version': device['current_version'],
                    'revision': 0,
                }
                if old and dict(old) != expected:
                    raise ValueError('firmware_device_conflict')
                db.execute(
                    'INSERT OR IGNORE INTO gate_firmware_device VALUES (?,?,?,?,0)',
                    (device['id'], device['gate'], device['cell'], device['current_version']),
                )
                db.execute(
                    'INSERT OR IGNORE INTO gate_firmware_evidence '
                    '(case_id,device,gate,cell,event_id,event_sha256,source_sha256,'
                    'frozen_version,frozen_revision,target_version) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (case_id, device['id'], device['gate'], device['cell'],
                     event['id'], event['sha256'], event['source_sha256'],
                     device['current_version'], 0, doc['target_version']),
                )
            payload = _json({
                'fixture': str(path),
                'case': doc,
                'observed': observed,
                'contracts': contracts,
            })
            db.execute('INSERT INTO gate_firmware_case VALUES (?,?,?)', (case_id, payload, digest))
    finally:
        db.close()
    return {'case_id': case_id, 'digest': digest, 'devices': len(doc['devices'])}


def state(db_path, device):
    db = connect(db_path)
    try:
        row = db.execute(
            'SELECT id,gate,cell,version,revision FROM gate_firmware_device WHERE id=?',
            (device,),
        ).fetchone()
        if not row:
            raise ValueError('unknown_gate_firmware_device')
        return dict(row)
    finally:
        db.close()


def operation_status(db_path, operation_id):
    """The endpoint's own ledger for an operation, or None when nothing committed."""
    db = connect(db_path)
    try:
        row = db.execute(
            'SELECT id,device,command,result FROM gate_firmware_operation WHERE id=?',
            (operation_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def invoke(db_path, case_id, device, command, version, operation_id, expected_revision):
    """One collector endpoint call.

    Returns the endpoint's answer.  ``ack_lost`` means the endpoint committed the
    version but the acknowledgement never reached the caller; the operation ledger
    is the only safe way to find out what happened.
    """
    if command not in ('install', 'rollback'):
        raise ValueError('bad_gate_firmware_command')
    db = connect(db_path)
    try:
        with db:
            prior = db.execute(
                'SELECT device,command,result FROM gate_firmware_operation WHERE id=?',
                (operation_id,),
            ).fetchone()
            if prior:
                if prior['device'] != device or prior['command'] != command:
                    return {'status': 'operation_id_conflict', 'record': json.loads(prior['result'])}
                return {'status': 'replayed', 'record': json.loads(prior['result'])}
            row = db.execute(
                'SELECT version,revision FROM gate_firmware_device WHERE id=?', (device,)
            ).fetchone()
            if not row:
                raise ValueError('unknown_gate_firmware_device')
            if row['revision'] != expected_revision:
                return {'status': 'revision_conflict', 'current': dict(row)}
            case = db.execute('SELECT payload FROM gate_firmware_case WHERE id=?', (case_id,)).fetchone()
            if not case:
                raise ValueError('unknown_gate_firmware_case')
            case_doc = json.loads(case['payload'])['case']
            spec = next((x for x in case_doc['devices'] if x['id'] == device), None)
            if not spec:
                raise ValueError('device_not_in_case')
            if command == 'install':
                if version != case_doc['target_version']:
                    raise ValueError('firmware_version_mismatch')
                failure = spec.get('failure')
            else:
                if version != spec['current_version']:
                    raise ValueError('firmware_version_mismatch')
                failure = spec.get('rollback_failure')
            if failure == 'reject':
                outcome = {'status': 'rejected', 'version': row['version'], 'revision': row['revision']}
                db.execute(
                    'INSERT INTO gate_firmware_operation VALUES (?,?,?,?)',
                    (operation_id, device, command, _json(outcome)),
                )
                return outcome
            if failure in ('unreachable', 'lost'):
                return {'status': failure, 'version': row['version'], 'revision': row['revision']}
            db.execute(
                'UPDATE gate_firmware_device SET version=?,revision=revision+1 WHERE id=?',
                (version, device),
            )
            outcome = {'status': 'applied', 'version': version, 'revision': row['revision'] + 1}
            db.execute(
                'INSERT INTO gate_firmware_operation VALUES (?,?,?,?)',
                (operation_id, device, command, _json(outcome)),
            )
            if failure == 'commit_then_ack_lost':
                return {'status': 'ack_lost'}
            return outcome
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Coordinator persistence helpers
# ---------------------------------------------------------------------------

def _device_row(db, device):
    row = db.execute(
        'SELECT id,gate,cell,version,revision FROM gate_firmware_device WHERE id=?',
        (device,),
    ).fetchone()
    if not row:
        raise ValueError('unknown_gate_firmware_device')
    return dict(row)


def _case_payload(db, case_id):
    row = db.execute('SELECT payload,digest FROM gate_firmware_case WHERE id=?', (case_id,)).fetchone()
    if not row:
        raise ValueError('unknown_gate_firmware_case')
    return json.loads(row['payload']), row['digest']


def _evidence_rows(db, case_id):
    return {
        row['device']: dict(row)
        for row in db.execute(
            'SELECT * FROM gate_firmware_evidence WHERE case_id=? ORDER BY device', (case_id,)
        )
    }


def _run_row(db, case_id, contract_id):
    return db.execute(
        'SELECT * FROM gate_firmware_run WHERE case_id=? AND contract_id=?',
        (case_id, contract_id),
    ).fetchone()


def _update_run(db, run_id, **fields):
    fields['updated_at'] = _now()
    assignments = ','.join(f'{name}=?' for name in fields)
    with db:
        db.execute(
            f'UPDATE gate_firmware_run SET {assignments} WHERE id=?',
            (*fields.values(), run_id),
        )


def _step_row(db, run_id, phase, device):
    row = db.execute(
        'SELECT * FROM gate_firmware_step WHERE run_id=? AND phase=? AND device=?',
        (run_id, phase, device),
    ).fetchone()
    return dict(row) if row else None


def _ensure_step(db, run_id, phase, device, ordinal, command, prior, target,
                 operation_id, expected_revision):
    row = _step_row(db, run_id, phase, device)
    if row:
        return row
    with db:
        db.execute(
            'INSERT OR IGNORE INTO gate_firmware_step '
            '(run_id,device,phase,ordinal,command,operation_id,prior_version,target_version,'
            'expected_revision,status,created_at,updated_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (run_id, device, phase, ordinal, command, operation_id, prior, target,
             expected_revision, 'pending', _now(), _now()),
        )
    return _step_row(db, run_id, phase, device)


def _update_step(db, run_id, phase, device, **fields):
    fields['updated_at'] = _now()
    assignments = ','.join(f'{name}=?' for name in fields)
    with db:
        db.execute(
            f'UPDATE gate_firmware_step SET {assignments} WHERE run_id=? AND phase=? AND device=?',
            (*fields.values(), run_id, phase, device),
        )
    return _step_row(db, run_id, phase, device)


def _manual(db, run_id, device, reason, live, detail=None):
    with db:
        db.execute(
            'INSERT OR IGNORE INTO gate_firmware_manual '
            '(run_id,device,reason,observed_version,observed_revision,detail,created_at) '
            'VALUES (?,?,?,?,?,?,?)',
            (run_id, device, reason,
             (live or {}).get('version'), (live or {}).get('revision'),
             _json(detail or {}), _now()),
        )


def _observe(db, run_id, device, phase, operation_id, live, source, note=''):
    with db:
        db.execute(
            'INSERT INTO gate_firmware_observation '
            '(run_id,device,phase,operation_id,version,revision,source,note,created_at) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (run_id, device, phase, operation_id, live['version'], live['revision'],
             source, note, _now()),
        )


def _ledger(db, operation_id):
    row = db.execute(
        'SELECT id,device,command,result FROM gate_firmware_operation WHERE id=?',
        (operation_id,),
    ).fetchone()
    return dict(row) if row else None


def _mark_failure(db, run_id, device, phase, status, live, reason, detail=None):
    _update_step(
        db, run_id, phase, device,
        status=status,
        observed_version=live.get('version'),
        observed_revision=live.get('revision'),
        status_recheck=_json({'live': live, 'reason': reason}),
        detail=_json(detail or {'reason': reason}),
    )
    if status in ('manual', 'revision_conflict', 'unreachable'):
        _manual(db, run_id, device, reason, live, detail)


def _step_modified(step):
    """A device counts as modified by this run only if the install really applied."""
    if not step or step['phase'] != 'install' or step['status'] != 'confirmed':
        return False
    detail = _decode(step['detail'], {})
    if detail.get('modified_this_run') is False:
        return False
    return bool(
        step['observed_version'] == step['target_version']
        and step['observed_revision'] is not None
        and step['expected_revision'] is not None
        and step['observed_revision'] == step['expected_revision'] + 1
    )


# ---------------------------------------------------------------------------
# Step reconciliation and driving
# ---------------------------------------------------------------------------

def _reconcile(db, run_id, device, phase, command, source, destination):
    """Resolve a persisted step from the endpoint ledger and the live device.

    Returns ``done`` (step terminal), ``ready`` (safe to dispatch the persisted
    intent) or ``blocked`` (needs a human).  Never invents a version.
    """
    step = _step_row(db, run_id, phase, device)
    if step['status'] in TERMINAL_STEP_STATUSES:
        return 'done'
    live = _device_row(db, device)
    operation_id = step['operation_id']
    expected = step['expected_revision']
    entry = _ledger(db, operation_id) if operation_id else None
    if entry:
        record = _decode(entry['result'], {})
        if entry['device'] != device:
            _mark_failure(db, run_id, device, phase, 'manual', live,
                          'operation_id_conflict', {'ledger': entry, 'live': live})
            return 'blocked'
        if record.get('status') == 'rejected':
            _update_step(
                db, run_id, phase, device, status='rejected',
                endpoint_response=_json(record),
                status_recheck=_json({'ledger': record, 'live': live}),
                observed_version=live['version'], observed_revision=live['revision'],
                detail=_json({'endpoint': 'rejected'}),
            )
            _observe(db, run_id, device, phase, operation_id, live, 'operation_ledger', 'rejected')
            return 'done'
        if record.get('status') == 'applied':
            if live['version'] != record['version'] or live['revision'] < record['revision']:
                _mark_failure(db, run_id, device, phase, 'manual', live,
                              'ledger_state_mismatch', {'ledger': record, 'live': live})
                return 'blocked'
            _update_step(
                db, run_id, phase, device, status='confirmed',
                endpoint_response=step['endpoint_response'] or _json({'status': 'reconciled'}),
                status_recheck=_json(record),
                observed_version=live['version'], observed_revision=live['revision'],
                detail=_json({'confirmed_by': 'operation_ledger',
                              'modified_this_run': live['version'] != step['prior_version']}),
            )
            _observe(db, run_id, device, phase, operation_id, live, 'operation_ledger')
            return 'done'
        _mark_failure(db, run_id, device, phase, 'manual', live,
                      'unexpected_ledger_record', {'ledger': record})
        return 'blocked'
    # No ledger entry: the endpoint never committed this operation.
    status = step['status']
    applied_now = (
        live['version'] == destination
        and live['revision'] == (expected or 0) + 1
    )
    if applied_now and status in ('ack_lost', 'intent'):
        if command != 'install':
            # A compensation must be proven by the endpoint ledger, never inferred
            # from a live version another maintainer could equally have produced.
            _mark_failure(db, run_id, device, phase, 'manual', live,
                          'rollback_without_operation_ledger', {'live': live})
            return 'blocked'
        _update_step(
            db, run_id, phase, device, status='confirmed',
            status_recheck=_json({'status': 'live_state_confirmed', 'live': live}),
            observed_version=live['version'], observed_revision=live['revision'],
            detail=_json({'confirmed_by': 'live_device_state', 'modified_this_run': True}),
        )
        _observe(db, run_id, device, phase, operation_id, live, 'live_device_state',
                 'ack lost without ledger')
        return 'done'
    if status == 'ack_lost':
        _mark_failure(db, run_id, device, phase, 'manual', live,
                      'ack_lost_unconfirmed', {'live': live})
        return 'blocked'
    if status == 'unreachable':
        if live['version'] == source and live['revision'] == expected:
            return 'ready'
        _mark_failure(db, run_id, device, phase, 'manual', live,
                      'device_changed_while_unreachable', {'live': live})
        return 'blocked'
    if live['version'] == source and live['revision'] == expected:
        return 'ready'
    if live['version'] == destination and live['revision'] == (expected or 0) + 1:
        # The live device already moved to the destination after our persisted intent
        # but with no ledger entry: either a crash mid-dispatch or an out-of-band
        # change.  Only an install may claim it; a rollback must stay manual.
        if command != 'install':
            _mark_failure(db, run_id, device, phase, 'manual', live,
                          'rollback_without_operation_ledger', {'live': live})
            return 'blocked'
        _update_step(
            db, run_id, phase, device, status='confirmed',
            status_recheck=_json({'status': 'live_state_confirmed', 'live': live}),
            observed_version=live['version'], observed_revision=live['revision'],
            detail=_json({'confirmed_by': 'live_device_state', 'modified_this_run': True}),
        )
        _observe(db, run_id, device, phase, operation_id, live, 'live_device_state',
                 'commit observed without ledger entry')
        return 'done'
    if live['version'] == destination and live['revision'] == expected:
        # Already at destination before dispatch (e.g. another plan moved it).
        _update_step(
            db, run_id, phase, device, status='confirmed',
            status_recheck=_json({'status': 'already_at_destination', 'live': live}),
            observed_version=live['version'], observed_revision=live['revision'],
            detail=_json({'already_at_destination': True, 'modified_this_run': False}),
        )
        _observe(db, run_id, device, phase, operation_id, live, 'live_device_state',
                 'already at destination')
        return 'done'
    _mark_failure(db, run_id, device, phase, 'manual', live,
                  'revision_changed_before_dispatch',
                  {'live': live, 'source': source, 'destination': destination})
    return 'blocked'


def _drive_step(db, db_path, run_id, case_id, device, phase, command, source, destination,
                crash_hook, max_attempts):
    for _ in range(max_attempts + 2):
        step = _step_row(db, run_id, phase, device)
        if step['status'] in TERMINAL_STEP_STATUSES:
            return step
        outcome = _reconcile(db, run_id, device, phase, command, source, destination)
        if outcome != 'ready':
            return _step_row(db, run_id, phase, device)
        step = _step_row(db, run_id, phase, device)
        if step['attempt'] >= max_attempts and step['status'] != 'intent':
            # 'intent' can be a crash before any dispatch reached the endpoint; the
            # stable operation id makes one reconciliation-and-dispatch safe.
            live = _device_row(db, device)
            _mark_failure(db, run_id, device, phase, 'unreachable', live,
                          'endpoint_unreachable', {'attempts': step['attempt']})
            return _step_row(db, run_id, phase, device)
        _update_step(db, run_id, phase, device, status='intent', attempt=step['attempt'] + 1)
        if crash_hook:
            crash_hook('after_intent', {'device': device, 'phase': phase,
                                        'operation_id': step['operation_id']})
        response = invoke(db_path, case_id, device, command, destination,
                          step['operation_id'], step['expected_revision'])
        if crash_hook:
            crash_hook('after_dispatch', {'device': device, 'phase': phase,
                                          'operation_id': step['operation_id'],
                                          'response': response})
        status = response.get('status')
        if status == 'ack_lost':
            # Do not retry under a new id: reconcile via the operation ledger.
            _update_step(db, run_id, phase, device, status='ack_lost',
                         endpoint_response=_json(response))
            continue
        if status in ('unreachable', 'lost'):
            live = _device_row(db, device)
            _update_step(
                db, run_id, phase, device, status='unreachable',
                endpoint_response=_json(response),
                observed_version=live['version'], observed_revision=live['revision'],
                detail=_json({'reason': 'endpoint_unreachable', 'attempt': step['attempt'] + 1}),
            )
            if step['attempt'] + 1 >= max_attempts:
                _manual(db, run_id, device, 'endpoint_unreachable', live,
                        {'attempts': step['attempt'] + 1})
            continue
        if status in ('rejected', 'operation_id_conflict', 'revision_conflict'):
            live = _device_row(db, device)
            reason = {
                'rejected': 'endpoint_rejected',
                'operation_id_conflict': 'operation_id_conflict',
                'revision_conflict': 'revision_conflict',
            }[status]
            _update_step(db, run_id, phase, device, endpoint_response=_json(response),
                         status_recheck=_json({'endpoint': response, 'live': live}))
            _mark_failure(
                db, run_id, device, phase,
                'rejected' if status == 'rejected' else 'manual',
                live, reason, {'endpoint': response},
            )
            _observe(db, run_id, device, phase, step['operation_id'], live,
                     'endpoint_response', reason)
            return _step_row(db, run_id, phase, device)
        if status == 'replayed':
            record = response.get('record') or {}
            if record.get('status') != 'applied':
                _mark_failure(db, run_id, device, phase, 'manual', _device_row(db, device),
                              'replayed_non_applied_operation', {'endpoint': response})
                return _step_row(db, run_id, phase, device)
            _update_step(db, run_id, phase, device, endpoint_response=_json(response))
            continue
        if status == 'applied':
            live = _device_row(db, device)
            if live['version'] != destination or live['revision'] != response.get('revision'):
                _update_step(db, run_id, phase, device, endpoint_response=_json(response))
                _mark_failure(
                    db, run_id, device, phase, 'manual', live,
                    'applied_state_mismatch', {'endpoint': response, 'live': live},
                )
                return _step_row(db, run_id, phase, device)
            _update_step(
                db, run_id, phase, device, status='confirmed',
                endpoint_response=_json(response),
                status_recheck=_json({'endpoint': response, 'live': live}),
                observed_version=live['version'], observed_revision=live['revision'],
                detail=_json({'confirmed_by': 'endpoint_response',
                              'modified_this_run': live['version'] != step['prior_version']}),
            )
            _observe(db, run_id, device, phase, step['operation_id'], live,
                     'endpoint_response')
            return _step_row(db, run_id, phase, device)
        _mark_failure(db, run_id, device, phase, 'manual', _device_row(db, device),
                      'unexpected_endpoint_response', {'endpoint': response})
        return _step_row(db, run_id, phase, device)
    return _step_row(db, run_id, phase, device)


def _install_device(db, db_path, run_id, case_id, contract_id, device, ordinal,
                    target, crash_hook, max_attempts):
    evidence = _evidence_rows(db, case_id).get(device)
    if not evidence:
        raise ValueError('firmware_device_evidence_missing')
    existing = _step_row(db, run_id, 'install', device)
    if existing and existing['status'] not in TERMINAL_STEP_STATUSES:
        # A crash left this step open: reconcile it before doing anything else.
        return _drive_step(db, db_path, run_id, case_id, device, 'install', 'install',
                           evidence['frozen_version'], target, crash_hook, max_attempts)
    live = _device_row(db, device)
    if live['version'] == target:
        step = _ensure_step(
            db, run_id, 'install', device, ordinal, 'install',
            evidence['frozen_version'], target,
            _operation_id(case_id, contract_id, device, 'install'), live['revision'],
        )
        if step['status'] not in TERMINAL_STEP_STATUSES:
            _update_step(
                db, run_id, 'install', device, status='confirmed',
                observed_version=live['version'], observed_revision=live['revision'],
                status_recheck=_json({'status': 'already_at_target', 'live': live}),
                detail=_json({'already_at_target': True,
                              'modified_this_run': live['revision'] == step['expected_revision'] + 1}),
            )
            _observe(db, run_id, device, 'install', step['operation_id'], live,
                     'live_device_state', 'already at target')
        return _step_row(db, run_id, 'install', device)
    _ensure_step(
        db, run_id, 'install', device, ordinal, 'install',
        evidence['frozen_version'], target,
        _operation_id(case_id, contract_id, device, 'install'),
        live['revision'],
    )
    return _drive_step(db, db_path, run_id, case_id, device, 'install', 'install',
                       evidence['frozen_version'], target, crash_hook, max_attempts)


def _rollback_device(db, db_path, run_id, case_id, contract_id, device, ordinal,
                     target, crash_hook, max_attempts):
    """Roll a device back to its frozen version, only if this run installed it."""
    existing = _step_row(db, run_id, 'rollback', device)
    if existing and existing['status'] in TERMINAL_STEP_STATUSES:
        return existing

    install = _step_row(db, run_id, 'install', device)
    evidence = _evidence_rows(db, case_id).get(device)
    if not evidence:
        raise ValueError('firmware_device_evidence_missing')
    prior = evidence['frozen_version']
    if not _step_modified(install):
        step = _ensure_step(
            db, run_id, 'rollback', device, ordinal, 'rollback',
            install['target_version'] if install else target, prior,
            _operation_id(case_id, contract_id, device, 'rollback'),
            install['observed_revision'] if install else 0,
        )
        if step['status'] not in TERMINAL_STEP_STATUSES:
            live = _device_row(db, device)
            _update_step(
                db, run_id, 'rollback', device, status='skipped',
                observed_version=live['version'], observed_revision=live['revision'],
                detail=_json({'reason': 'not_modified_this_run'}),
            )
            _observe(db, run_id, device, 'rollback', step['operation_id'], live,
                     'live_device_state', 'not modified this run')
        return _step_row(db, run_id, 'rollback', device)
    operation_id = _operation_id(case_id, contract_id, device, 'rollback')
    live = _device_row(db, device)
    if (live['version'] != install['target_version']
            or live['revision'] != install['observed_revision']):
        # Another maintainer changed the device after this run installed it.
        # Only an already persisted rollback operation may explain the drift.
        if not _ledger(db, operation_id):
            unexplained_commit = (
                live['version'] == prior
                and live['revision'] == install['observed_revision'] + 1
            )
            _ensure_step(
                db, run_id, 'rollback', device, ordinal, 'rollback',
                install['target_version'], prior, operation_id, live['revision'],
            )
            _mark_failure(
                db, run_id, device, 'rollback', 'manual', live,
                'rollback_without_operation_ledger' if unexplained_commit
                else 'concurrent_change_before_rollback',
                {'install': install, 'live': live},
            )
            return _step_row(db, run_id, 'rollback', device)
        _ensure_step(
            db, run_id, 'rollback', device, ordinal, 'rollback',
            install['target_version'], prior, operation_id, install['observed_revision'],
        )
        return _drive_step(db, db_path, run_id, case_id, device, 'rollback', 'rollback',
                           install['target_version'], prior, crash_hook, max_attempts)
    _ensure_step(
        db, run_id, 'rollback', device, ordinal, 'rollback',
        install['target_version'], prior, operation_id, install['observed_revision'],
    )
    step = _drive_step(db, db_path, run_id, case_id, device, 'rollback', 'rollback',
                       install['target_version'], prior, crash_hook, max_attempts)
    if step['status'] not in ('confirmed', 'skipped'):
        # The compensation obligation was not met; a human must own it.
        _manual(db, run_id, device, f"rollback_{step['status']}", _device_row(db, device),
                {'step': {k: step[k] for k in ('status', 'operation_id', 'observed_version',
                                               'observed_revision', 'expected_revision')}})
    return step


# ---------------------------------------------------------------------------
# Rollout
# ---------------------------------------------------------------------------

def _plan_order(doc, devices):
    ids = [d['id'] for d in devices]
    order = [d for d in doc.get('order', []) if d in ids]
    order += [d for d in ids if d not in order]
    return order


def _units(devices, evidence):
    """Unit = (gate, cell); zone_isolated keeps a unit's collectors consistent."""
    units = {}
    for device in devices:
        ev = evidence.get(device['id']) or {}
        key = (ev.get('gate') or device['gate'], ev.get('cell') or device['cell'])
        units.setdefault(key, {'gate': key[0], 'cell': key[1], 'devices': []})['devices'].append(device['id'])
    return units


def _unit_reachable(units):
    gates_for_cell = {}
    for (gate, cell) in units:
        gates_for_cell.setdefault(cell, set()).add(gate)
    if any(len(gates) > 1 for gates in gates_for_cell.values()):
        raise ValueError('firmware_cell_spans_gates')


def _run_summary(db, case_id, contract_id):
    run = _run_row(db, case_id, contract_id)
    if not run:
        raise ValueError('unknown_gate_firmware_run')
    return {
        'case_id': case_id,
        'contract_id': contract_id,
        'status': run['status'],
        'outcome': run['outcome'],
        'authorized_by': run['contract_authorized_by'],
        'detail': _decode(run['detail'], {}),
        'started_at': run['started_at'],
        'updated_at': run['updated_at'],
        'ended_at': run['ended_at'],
        'devices': [dict(row) for row in db.execute(
            'SELECT id,gate,cell,version,revision FROM gate_firmware_device '
            'WHERE id IN (SELECT device FROM gate_firmware_evidence WHERE case_id=?) ORDER BY id',
            (case_id,))],
        'steps': [dict(row) for row in db.execute(
            'SELECT * FROM gate_firmware_step WHERE run_id=? ORDER BY ordinal,phase,id', (run['id'],))],
        'manual': [dict(row) for row in db.execute(
            'SELECT * FROM gate_firmware_manual WHERE run_id=? ORDER BY id', (run['id'],))],
        'observations': [dict(row) for row in db.execute(
            'SELECT * FROM gate_firmware_observation WHERE run_id=? ORDER BY id', (run['id'],))],
    }


def run_rollout(db_path, case_id, contract_id, crash_hook=None, max_attempts=MAX_ATTEMPTS):
    """Run or resume one authorized recovery contract for one frozen case."""
    if contract_id not in CONTRACTS:
        raise ValueError('unknown_gate_firmware_contract')
    db = connect(db_path)
    try:
        payload, digest = _case_payload(db, case_id)
        contract = (payload.get('contracts') or {}).get(contract_id)
        if not contract or not contract.get('authorized_by'):
            raise ValueError('gate_firmware_contract_unauthorized')
        doc = payload['case']
        devices = {d['id']: d for d in doc['devices']}
        evidence = _evidence_rows(db, case_id)
        if set(evidence) != set(devices):
            raise ValueError('firmware_device_evidence_missing')
        units = _units(list(devices.values()), evidence)
        _unit_reachable(units)
        target = doc['target_version']
        order = _plan_order(doc, list(devices.values()))
        run = _run_row(db, case_id, contract_id)
        terminal = run and run['ended_at'] is not None
        if not run:
            with db:
                db.execute(
                    'INSERT INTO gate_firmware_run '
                    '(case_id,contract_id,contract_authorized_by,status,frozen_digest,detail,'
                    'started_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                    (case_id, contract_id, contract['authorized_by'], 'running', digest,
                     _json({'target_version': target, 'order': order,
                            'units': {f'{g}/{c}': u for (g, c), u in units.items()}}),
                     _now(), _now()),
                )
            run = _run_row(db, case_id, contract_id)
        run_id = run['id']
        if terminal:
            return _run_summary(db, case_id, contract_id)
        _update_run(db, run_id, status='running')

        def unit_of(device_id):
            for key, unit in units.items():
                if device_id in unit['devices']:
                    return key
            return None

        ordinals = {device_id: index + 1 for index, device_id in enumerate(order)}
        failures = []           # (device, reason)
        blocked_units = set()
        for device_id in order:
            spec = devices[device_id]
            unit = unit_of(device_id)
            if contract_id == 'zone_isolated' and unit in blocked_units:
                step = _ensure_step(
                    db, run_id, 'install', device_id, ordinals[device_id], 'install',
                    spec['current_version'], target,
                    _operation_id(case_id, contract_id, device_id, 'install'),
                    _device_row(db, device_id)['revision'],
                )
                if step['status'] not in TERMINAL_STEP_STATUSES:
                    live = _device_row(db, device_id)
                    _update_step(
                        db, run_id, 'install', device_id, status='skipped',
                        observed_version=live['version'], observed_revision=live['revision'],
                        detail=_json({'reason': 'unit_blocked'}),
                    )
                    _observe(db, run_id, device_id, 'install', step['operation_id'], live,
                             'live_device_state', 'unit blocked by sibling failure')
                continue
            step = _install_device(
                db, db_path, run_id, case_id, contract_id, device_id,
                ordinals[device_id], target, crash_hook, max_attempts,
            )
            if step['status'] == 'confirmed':
                continue
            failures.append((device_id, step['status']))
            if contract_id == 'atomic_all_gates':
                break
            blocked_units.add(unit)

        # Every device in the plan gets an explicit step, even when the plan aborts.
        for device_id in order:
            if _step_row(db, run_id, 'install', device_id):
                continue
            live = _device_row(db, device_id)
            step = _ensure_step(
                db, run_id, 'install', device_id, ordinals[device_id], 'install',
                evidence[device_id]['frozen_version'], target,
                _operation_id(case_id, contract_id, device_id, 'install'),
                _device_row(db, device_id)['revision'],
            )
            if step['status'] not in TERMINAL_STEP_STATUSES:
                _update_step(
                    db, run_id, 'install', device_id, status='skipped',
                    observed_version=live['version'], observed_revision=live['revision'],
                    detail=_json({'reason': 'atomic_aborted'}),
                )
                _observe(db, run_id, device_id, 'install', step['operation_id'], live,
                         'live_device_state', 'atomic plan aborted before dispatch')

        if contract_id == 'atomic_all_gates':
            if not failures and all(
                _step_row(db, run_id, 'install', d)['status'] == 'confirmed' for d in order
            ):
                live_ok = all(_device_row(db, d)['version'] == target for d in order)
                if live_ok:
                    _update_run(
                        db, run_id, status='completed', outcome='completed',
                        ended_at=_now(),
                        detail=_json({'target_version': target, 'order': order,
                                      'units': {f'{g}/{c}': u['devices'] for (g, c), u in units.items()}}),
                    )
                else:
                    _manual(db, run_id, order[0], 'atomic_live_version_mismatch',
                            _device_row(db, order[0]),
                            {'expected': target,
                             'final_versions': {d: _device_row(db, d)['version'] for d in order}})
                    _update_run(db, run_id, status='failed', outcome='manual_required',
                                ended_at=_now(), detail=_json({'reason': 'live_version_mismatch'}))
                return _run_summary(db, case_id, contract_id)
            compensation = []
            modified = {d for d in order if _step_modified(_step_row(db, run_id, 'install', d))}
            for device_id in reversed(order):
                if device_id not in modified:
                    unchanged = _step_row(db, run_id, 'rollback', device_id)
                    if unchanged:
                        continue
                    live = _device_row(db, device_id)
                    _ensure_step(
                        db, run_id, 'rollback', device_id, ordinals[device_id], 'rollback',
                        evidence[device_id]['frozen_version'], evidence[device_id]['frozen_version'],
                        _operation_id(case_id, contract_id, device_id, 'rollback'),
                        live['revision'],
                    )
                    _update_step(
                        db, run_id, 'rollback', device_id, status='skipped',
                        observed_version=live['version'], observed_revision=live['revision'],
                        detail=_json({'reason': 'not_modified_this_run'}),
                    )
                    _observe(db, run_id, device_id, 'rollback', None, live,
                             'live_device_state', 'not modified this run')
                    continue
                step = _rollback_device(
                    db, db_path, run_id, case_id, contract_id, device_id,
                    ordinals[device_id], target, crash_hook, max_attempts,
                )
                compensation.append((device_id, step['status']))
            manual = db.execute(
                'SELECT count(*) FROM gate_firmware_manual WHERE run_id=?', (run_id,)
            ).fetchone()[0]
            all_restored = all(status in ('confirmed', 'skipped') for _, status in compensation)
            live_all_prior = all(
                _device_row(db, d)['version'] == evidence[d]['frozen_version'] for d in order
            )
            if all_restored and live_all_prior and not manual:
                outcome, status = 'rolled_back', 'completed'
            else:
                outcome, status = 'manual_required', 'failed'
                if not manual:
                    _manual(db, run_id, order[0], 'compensation_incomplete',
                            _device_row(db, order[0]), {'compensation': compensation})
            _update_run(
                db, run_id, status=status, outcome=outcome, ended_at=_now(),
                detail=_json({
                    'target_version': target,
                    'order': order,
                    'failures': failures,
                    'compensation': compensation,
                    'final_versions': {d: _device_row(db, d)['version'] for d in order},
                }),
            )
            return _run_summary(db, case_id, contract_id)

        # zone_isolated: each unit must end internally consistent.
        unit_results = {}
        for key, unit in units.items():
            members = unit['devices']
            steps = {d: _step_row(db, run_id, 'install', d) for d in members}
            confirmed = [d for d in members if steps[d] and steps[d]['status'] == 'confirmed']
            modified = [d for d in members if _step_modified(steps[d])]
            if len(confirmed) == len(members):
                versions = {_device_row(db, d)['version'] for d in members}
                unit_results[f'{key[0]}/{key[1]}'] = {
                    'status': 'upgraded' if versions == {target} else 'inconsistent',
                    'devices': {d: _device_row(db, d)['version'] for d in members},
                }
                continue
            # A unit that did not fully upgrade must not stay half-upgraded.
            rolled = []
            for device_id in reversed(modified):
                step = _rollback_device(
                    db, db_path, run_id, case_id, contract_id, device_id,
                    ordinals[device_id], target, crash_hook, max_attempts,
                )
                rolled.append((device_id, step['status']))
            versions = {d: _device_row(db, d)['version'] for d in members}
            same = len(set(versions.values())) == 1
            at_prior = set(versions.values()) == {evidence[members[0]]['frozen_version']}
            rollback_ok = all(s in ('confirmed', 'skipped') for _, s in rolled)
            if same and at_prior and rollback_ok:
                unit_status = 'rolled_back' if modified else 'unchanged'
            else:
                # Never claim a unit is uniform when it is not, or when the frozen
                # version is not what is actually installed.
                unit_status = 'inconsistent'
                _manual(db, run_id, members[0], 'unit_inconsistent_after_failure',
                        _device_row(db, members[0]),
                        {'versions': versions, 'rollback': rolled})
            unit_results[f'{key[0]}/{key[1]}'] = {
                'status': unit_status,
                'devices': versions,
                'rollback': rolled,
                'failures': [d for d in members if steps[d] and steps[d]['status'] in ('rejected', 'manual')],
            }
        manual = db.execute(
            'SELECT count(*) FROM gate_firmware_manual WHERE run_id=?', (run_id,)
        ).fetchone()[0]
        statuses = {u['status'] for u in unit_results.values()}
        if manual or 'inconsistent' in statuses:
            outcome, status = 'manual_required', 'failed'
        elif statuses == {'upgraded'}:
            outcome, status = 'completed', 'completed'
        elif 'upgraded' in statuses:
            outcome, status = 'partial', 'completed'
        else:
            outcome, status = 'rolled_back', 'completed'
        _update_run(
            db, run_id, status=status, outcome=outcome, ended_at=_now(),
            detail=_json({
                'target_version': target,
                'order': order,
                'failures': failures,
                'units': unit_results,
                'final_versions': {d: _device_row(db, d)['version'] for d in order},
            }),
        )
        return _run_summary(db, case_id, contract_id)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Version evidence report
# ---------------------------------------------------------------------------

def evidence_report(db_path, case_id, contract_id, out):
    """Write a JSON report tying every claimed version to endpoint/live evidence."""
    if contract_id not in CONTRACTS:
        raise ValueError('unknown_gate_firmware_contract')
    db = connect(db_path)
    try:
        summary = _run_summary(db, case_id, contract_id)
        payload, digest = _case_payload(db, case_id)
        doc = payload['case']
        evidence = _evidence_rows(db, case_id)
        run_id = _run_row(db, case_id, contract_id)['id']
        devices = []
        for spec in doc['devices']:
            device_id = spec['id']
            ev = evidence.get(device_id)
            if not ev:
                raise ValueError('firmware_device_evidence_missing')
            live = _device_row(db, device_id)
            steps = {}
            for phase in ('install', 'rollback'):
                step = _step_row(db, run_id, phase, device_id)
                if not step:
                    continue
                steps[phase] = {
                    'command': step['command'],
                    'operation_id': step['operation_id'],
                    'intent_status': 'recorded' if step['attempt'] else 'not_dispatched',
                    'attempt': step['attempt'],
                    'expected_revision': step['expected_revision'],
                    'prior_version': step['prior_version'],
                    'target_version': step['target_version'],
                    'status': step['status'],
                    'endpoint_response': _decode(step['endpoint_response']),
                    'status_recheck': _decode(step['status_recheck']),
                    'observed_version': step['observed_version'],
                    'observed_revision': step['observed_revision'],
                    'modified_this_run': _step_modified(step),
                }
            devices.append({
                'device': device_id,
                'gate': ev['gate'],
                'cell': ev['cell'],
                'event_id': ev['event_id'],
                'event_sha256': ev['event_sha256'],
                'source_sha256': ev['source_sha256'],
                'frozen_version': ev['frozen_version'],
                'frozen_revision': ev['frozen_revision'],
                'target_version': ev['target_version'],
                'actual_version': live['version'],
                'actual_revision': live['revision'],
                'reached_target': live['version'] == ev['target_version'],
                'steps': steps,
                'manual': [dict(row) for row in db.execute(
                    'SELECT * FROM gate_firmware_manual WHERE run_id=? AND device=? ORDER BY id',
                    (run_id, device_id))],
            })
        report = {
            'case_id': case_id,
            'case_digest': digest,
            'contract_id': contract_id,
            'contract_authorized_by': summary['authorized_by'],
            'run_outcome': summary['outcome'],
            'run_status': summary['status'],
            'target_version': doc['target_version'],
            'devices': devices,
            'final_versions': {d['device']: d['actual_version'] for d in devices},
            'gates': {
                gate: {
                    'physical_versions': {
                        d['device']: d['actual_version'] for d in devices if d['gate'] == gate
                    },
                    'sources': {
                        d['device']: {
                            'event_id': d['event_id'],
                            'event_sha256': d['event_sha256'],
                            'source_sha256': d['source_sha256'],
                        }
                        for d in devices if d['gate'] == gate
                    },
                }
                for gate in sorted({d['gate'] for d in devices})
            },
            'manual': summary['manual'],
            'observations': summary['observations'],
            'note': '实际版本只取自 gate_firmware_device；计划版本不作为安装证据。',
        }
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return p
    finally:
        db.close()
