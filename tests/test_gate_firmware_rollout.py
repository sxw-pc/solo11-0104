import json
import sqlite3
from pathlib import Path

import pytest

from gateflow.__main__ import initialize
from gateflow.gate_firmware import (
    CrashSimulated,
    CONTRACTS,
    evidence_report,
    invoke,
    operation_status,
    register,
    run_rollout,
    state,
)
from gateflow.ingest import ingest
from gateflow.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'


def make_db(tmp_path, name='firmware.db', case='FIRM-1'):
    db = tmp_path / name
    initialize(db, FIX / 'gate-firmware-config.json')
    ingest(FIX / 'gate-firmware-events.jsonl', db)
    register(db, case, FIX / 'gate-firmware-case.json')
    return db


def versions(db, devices=('D1', 'D2', 'D3')):
    return [state(db, d)['version'] for d in devices]


def steps(summary):
    return {(s['device'], s['phase']): s for s in summary['steps']}


def crash_on(device, phase, when):
    def hook(stage, info):
        if stage == when and info['device'] == device and info['phase'] == phase:
            raise CrashSimulated(f'{stage}:{device}:{phase}')
    return hook


# ---------------------------------------------------------------------------
# Contracts and frozen evidence
# ---------------------------------------------------------------------------

def test_both_contracts_are_required_and_authorized(tmp_path):
    db = tmp_path / 'firmware.db'
    initialize(db, FIX / 'gate-firmware-config.json')
    ingest(FIX / 'gate-firmware-events.jsonl', db)
    doc = json.loads((FIX / 'gate-firmware-case.json').read_text(encoding='utf-8'))
    doc['contracts'] = [c for c in doc['contracts'] if c['id'] != 'zone_isolated']
    bad = tmp_path / 'case.json'
    bad.write_text(json.dumps(doc), encoding='utf-8')
    with pytest.raises(ValueError, match='gate_firmware_contract_unauthorized'):
        register(db, 'FIRM-1', bad)


def test_evidence_binds_real_event_and_source(tmp_path):
    db = make_db(tmp_path)
    conn = connect(db)
    rows = {r['device']: dict(r) for r in conn.execute('SELECT * FROM gate_firmware_evidence')}
    conn.close()
    assert set(rows) == {'D1', 'D2', 'D3'}
    assert rows['D1']['gate'] == 'G1' and rows['D2']['gate'] == 'G1'
    assert rows['D3']['gate'] == 'G2'
    assert rows['D1']['frozen_version'] == 'v1'
    assert rows['D1']['event_sha256'] and rows['D1']['source_sha256']


def test_register_is_idempotent_but_conflicting_fixture_is_rejected(tmp_path):
    db = make_db(tmp_path)
    assert register(db, 'FIRM-1', FIX / 'gate-firmware-case.json')['devices'] == 3
    doc = json.loads((FIX / 'gate-firmware-case.json').read_text(encoding='utf-8'))
    doc['target_version'] = 'v9'
    other = tmp_path / 'other.json'
    other.write_text(json.dumps(doc), encoding='utf-8')
    with pytest.raises(ValueError, match='gate_firmware_case_conflict'):
        register(db, 'FIRM-1', other)


def test_gate_events_and_zone_decisions_are_not_rewritten(tmp_path):
    """Firmware rollout must not touch the original events or occupancy decisions."""
    db = tmp_path / 'firmware.db'
    initialize(db, FIX / 'gate-firmware-config.json')
    ingest(FIX / 'gate-firmware-events.jsonl', db)
    from gateflow.rules import evaluate
    evaluate(db)
    before = connect(db)
    events = [dict(r) for r in before.execute('SELECT * FROM event ORDER BY id')]
    decisions = [dict(r) for r in before.execute('SELECT * FROM decision ORDER BY zone')]
    before.close()
    register(db, 'FIRM-1', FIX / 'gate-firmware-case.json')
    run_rollout(db, 'FIRM-1', 'atomic_all_gates')
    after = connect(db)
    assert [dict(r) for r in after.execute('SELECT * FROM event ORDER BY id')] == events
    assert [dict(r) for r in after.execute('SELECT * FROM decision ORDER BY zone')] == decisions
    after.close()


# ---------------------------------------------------------------------------
# Endpoint semantics
# ---------------------------------------------------------------------------

def test_ack_lost_commits_real_version_and_replays_same_operation(tmp_path):
    db = make_db(tmp_path)
    assert invoke(db, 'FIRM-1', 'D2', 'install', 'v2', 'OP-D2', 0) == {'status': 'ack_lost'}
    assert state(db, 'D2')['version'] == 'v2'
    # Same id: replayed, not re-applied; D2 must not gain another revision.
    replay = invoke(db, 'FIRM-1', 'D2', 'install', 'v2', 'OP-D2', 0)
    assert replay['status'] == 'replayed'
    assert state(db, 'D2')['revision'] == 1
    # A new id would be a blind retry: it really applies a second time.
    assert invoke(db, 'FIRM-1', 'D2', 'install', 'v2', 'OP-D2-NEW', 1)['status'] in ('applied', 'ack_lost')
    assert state(db, 'D2')['revision'] == 2


def test_operation_id_reuse_for_different_device_is_conflict(tmp_path):
    db = make_db(tmp_path)
    invoke(db, 'FIRM-1', 'D1', 'install', 'v2', 'SHARED', 0)
    assert invoke(db, 'FIRM-1', 'D2', 'install', 'v2', 'SHARED', 0)['status'] == 'operation_id_conflict'
    assert state(db, 'D2')['version'] == 'v1'


def test_stale_revision_is_refused(tmp_path):
    db = make_db(tmp_path)
    assert invoke(db, 'FIRM-1', 'D1', 'install', 'v2', 'OP-1', 0)['status'] == 'applied'
    result = invoke(db, 'FIRM-1', 'D1', 'install', 'v2', 'OP-2', 0)
    assert result['status'] == 'revision_conflict'
    assert result['current']['revision'] == 1


def test_rejected_install_records_endpoint_answer_without_version_change(tmp_path):
    db = make_db(tmp_path)
    assert invoke(db, 'FIRM-1', 'D3', 'install', 'v2', 'OP-D3', 0)['status'] == 'rejected'
    assert state(db, 'D3') == {'id': 'D3', 'gate': 'G2', 'cell': 'CELL-2', 'version': 'v1', 'revision': 0}
    assert operation_status(db, 'OP-D3')['result'] == json.dumps(
        {'revision': 0, 'status': 'rejected', 'version': 'v1'}, sort_keys=True, separators=(',', ':'))


def test_unreachable_endpoint_never_commits(tmp_path):
    db = tmp_path / 'firmware.db'
    initialize(db, FIX / 'gate-firmware-config.json')
    ingest(FIX / 'gate-firmware-events.jsonl', db)
    doc = json.loads((FIX / 'gate-firmware-case.json').read_text(encoding='utf-8'))
    doc['devices'][0]['failure'] = 'unreachable'
    fixture = tmp_path / 'unreachable.json'
    fixture.write_text(json.dumps(doc), encoding='utf-8')
    register(db, 'FIRM-1', fixture)
    assert invoke(db, 'FIRM-1', 'D1', 'install', 'v2', 'OP', 0)['status'] == 'unreachable'
    assert state(db, 'D1')['version'] == 'v1'
    assert operation_status(db, 'OP') is None


# ---------------------------------------------------------------------------
# Contract outcomes on the real endpoint facts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('contract,expected', [
    ('atomic_all_gates', ['v1', 'v1', 'v1']),
    ('zone_isolated', ['v2', 'v2', 'v1']),
])
def test_fixture_contracts_produce_real_installed_versions(tmp_path, contract, expected):
    db = make_db(tmp_path)
    summary = run_rollout(db, 'FIRM-1', contract)
    assert versions(db) == expected
    by_step = steps(summary)
    # D2 acknowledged nothing but really committed v2: coordinator confirms it.
    assert by_step[('D2', 'install')]['observed_version'] == 'v2'
    assert by_step[('D2', 'install')]['status'] == 'confirmed'
    # D3 was refused and stays at v1; no fake "installed".
    assert by_step[('D3', 'install')]['status'] == 'rejected'
    assert by_step[('D3', 'install')]['observed_version'] == 'v1'
    assert by_step[('D3', 'install')]['endpoint_response'] is not None


def test_atomic_contract_compensates_installed_devices(tmp_path):
    db = make_db(tmp_path)
    summary = run_rollout(db, 'FIRM-1', 'atomic_all_gates')
    by_step = steps(summary)
    assert summary['outcome'] == 'rolled_back'
    assert by_step[('D1', 'install')]['observed_version'] == 'v2'
    assert by_step[('D1', 'rollback')]['status'] == 'confirmed'
    assert by_step[('D2', 'install')]['observed_version'] == 'v2'
    assert by_step[('D2', 'rollback')]['status'] == 'confirmed'
    assert by_step[('D3', 'rollback')]['status'] == 'skipped'
    # Every rollback carried its own expected revision matching the install outcome.
    assert by_step[('D2', 'rollback')]['expected_revision'] == by_step[('D2', 'install')]['observed_revision']
    assert summary['manual'] == []


def test_zone_contract_keeps_g1_upgrade_and_g2_untouched(tmp_path):
    db = make_db(tmp_path)
    summary = run_rollout(db, 'FIRM-1', 'zone_isolated')
    by_step = steps(summary)
    assert summary['outcome'] == 'partial'
    assert {('D1', 'rollback'), ('D2', 'rollback')}.isdisjoint(by_step)
    assert state(db, 'D1')['version'] == state(db, 'D2')['version'] == 'v2'
    assert state(db, 'D3')['version'] == 'v1'
    unit = summary['detail']['units']['G1/CELL-1']
    assert unit['status'] == 'upgraded'


def test_same_endpoint_facts_yield_different_real_versions(tmp_path):
    """Both contracts are legal; the facts are identical, the real versions are not."""
    atomic = make_db(tmp_path, 'atomic.db')
    zoned = make_db(tmp_path, 'zoned.db')
    run_rollout(atomic, 'FIRM-1', 'atomic_all_gates')
    run_rollout(zoned, 'FIRM-1', 'zone_isolated')
    assert versions(atomic) == ['v1', 'v1', 'v1']
    assert versions(zoned) == ['v2', 'v2', 'v1']
    # Same frozen endpoint facts in both runs: D1 applies, D2 commits then loses
    # its ack, D3 refuses. Only the compensation differs, so the real end state does.
    def facts(conn):
        return sorted((r['device'], r['command'], json.loads(r['result'])['status'])
                      for r in conn.execute('SELECT device,command,result FROM gate_firmware_operation'))
    conn_a, conn_z = connect(atomic), connect(zoned)
    facts_a, facts_z = facts(conn_a), facts(conn_z)
    conn_a.close(); conn_z.close()
    for ledger in (facts_a, facts_z):
        assert ('D2', 'install', 'applied') in ledger
        assert ('D3', 'install', 'rejected') in ledger
        assert ('D3', 'rollback', 'applied') not in ledger
    assert versions(atomic) != versions(zoned)


# ---------------------------------------------------------------------------
# Resume, crash and restart
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('contract', CONTRACTS)
def test_restart_after_commit_then_ack_lost_confirms_before_acting(tmp_path, contract):
    db = make_db(tmp_path)
    with pytest.raises(CrashSimulated):
        run_rollout(db, 'FIRM-1', contract,
                    crash_hook=crash_on('D2', 'install', 'after_dispatch'))
    assert state(db, 'D2')['version'] == 'v2'
    summary = run_rollout(db, 'FIRM-1', contract)
    by_step = steps(summary)
    assert by_step[('D2', 'install')]['status'] == 'confirmed'
    assert by_step[('D2', 'install')]['attempt'] == 1
    assert by_step[('D2', 'install')]['observed_revision'] == 1
    # Resumed run did not re-install D2 (confirming only reads the ledger).
    after_install_revision = 1 if contract == 'zone_isolated' else 2  # atomic also rolls back
    assert state(db, 'D2')['revision'] == after_install_revision
    assert summary['outcome'] == ('rolled_back' if contract == 'atomic_all_gates' else 'partial')


def test_restart_after_commit_before_any_record_still_recovers(tmp_path):
    """Crash after the endpoint committed but before the answer was stored anywhere."""
    db = make_db(tmp_path)
    with pytest.raises(CrashSimulated):
        run_rollout(db, 'FIRM-1', 'atomic_all_gates',
                    crash_hook=crash_on('D1', 'install', 'after_intent'))
    live = state(db, 'D1')
    assert live['version'] == 'v1'  # intent persisted, nothing dispatched yet
    summary = run_rollout(db, 'FIRM-1', 'atomic_all_gates')
    assert steps(summary)[('D1', 'install')]['observed_version'] == 'v2'
    assert versions(db) == ['v1', 'v1', 'v1']


def test_restart_mid_compensation_finishes_obligation_without_reinstall(tmp_path):
    """Crash right after D2's rollback committed: restart must confirm, not redo."""
    db = make_db(tmp_path)
    with pytest.raises(CrashSimulated):
        run_rollout(db, 'FIRM-1', 'atomic_all_gates',
                    crash_hook=crash_on('D2', 'rollback', 'after_dispatch'))
    assert state(db, 'D2')['version'] == 'v1'
    assert state(db, 'D1')['version'] == 'v2'  # D1 compensation still pending
    summary = run_rollout(db, 'FIRM-1', 'atomic_all_gates')
    by_step = steps(summary)
    assert by_step[('D1', 'rollback')]['status'] == 'confirmed'
    assert by_step[('D2', 'rollback')]['status'] == 'confirmed'
    assert by_step[('D2', 'rollback')]['attempt'] == 1
    assert versions(db) == ['v1', 'v1', 'v1']
    assert summary['outcome'] == 'rolled_back'
    conn = connect(db)
    ops = [dict(r) for r in conn.execute('SELECT * FROM gate_firmware_operation')]
    conn.close()
    d2_ops = [o for o in ops if o['device'] == 'D2']
    assert len(d2_ops) == 2  # one install (its lost ack was a delivery fault), one rollback
    assert [o['command'] for o in d2_ops] == ['install', 'rollback']


def test_clean_all_success_atomic_completes_without_rollback(tmp_path):
    def mutate(doc):
        for device in doc['devices']:
            device.pop('failure', None)
    db = _custom_case(tmp_path, mutate)
    summary = run_rollout(db, 'FIRM-1', 'atomic_all_gates')
    assert summary['outcome'] == 'completed'
    assert versions(db) == ['v2', 'v2', 'v2']
    assert not any(s['phase'] == 'rollback' for s in summary['steps'])


def test_clean_all_success_zone_reports_both_units_upgraded(tmp_path):
    def mutate(doc):
        for device in doc['devices']:
            device.pop('failure', None)
    db = _custom_case(tmp_path, mutate)
    summary = run_rollout(db, 'FIRM-1', 'zone_isolated')
    assert summary['outcome'] == 'completed'
    assert set(summary['detail']['units']) == {'G1/CELL-1', 'G2/CELL-2'}
    assert {u['status'] for u in summary['detail']['units'].values()} == {'upgraded'}
    assert versions(db) == ['v2', 'v2', 'v2']


def test_rollback_unreachable_keeps_compensation_obligation(tmp_path):
    def mutate(doc):
        doc['devices'][0]['rollback_failure'] = 'unreachable'
    db = _custom_case(tmp_path, mutate)
    summary = run_rollout(db, 'FIRM-1', 'atomic_all_gates', max_attempts=2)
    by_step = steps(summary)
    assert by_step[('D1', 'rollback')]['status'] == 'unreachable'
    assert state(db, 'D1')['version'] == 'v2'  # really still upgraded, not lied about
    assert summary['outcome'] == 'manual_required'
    assert any(m['device'] == 'D1' and m['reason'] == 'rollback_unreachable'
               for m in summary['manual'])
    # The obligation never disappears on rerun.
    again = run_rollout(db, 'FIRM-1', 'atomic_all_gates', max_attempts=2)
    assert any(m['device'] == 'D1' for m in again['manual'])
    assert state(db, 'D1')['version'] == 'v2'


def test_rollback_only_targets_versions_this_run_changed(tmp_path):
    """D1 was already at target by someone else: we must not roll it back."""
    db = make_db(tmp_path)
    conn = connect(db)
    conn.execute("UPDATE gate_firmware_device SET version='v2',revision=5 WHERE id='D1'")
    conn.commit(); conn.close()
    summary = run_rollout(db, 'FIRM-1', 'atomic_all_gates')
    by_step = steps(summary)
    assert by_step[('D1', 'install')]['observed_version'] == 'v2'
    assert by_step[('D1', 'rollback')]['status'] == 'skipped'
    assert state(db, 'D1')['version'] == 'v2' and state(db, 'D1')['revision'] == 5
    assert state(db, 'D2')['version'] == 'v1'
    assert summary['outcome'] == 'manual_required'  # pre-existing drift cannot be "rolled back"
    assert any(m['device'] == 'D1' for m in summary['manual'])


def test_operation_id_collision_inside_plan_stops_device(tmp_path):
    """If our stable op id is already owned by another device, stop that device."""
    from gateflow.gate_firmware import _operation_id
    db = make_db(tmp_path)
    op = _operation_id('FIRM-1', 'atomic_all_gates', 'D1', 'install')
    conn = connect(db)
    conn.execute('INSERT INTO gate_firmware_operation VALUES (?,?,?,?)',
                 (op, 'D9', 'install', json.dumps({'status': 'applied', 'version': 'v2', 'revision': 1})))
    conn.commit(); conn.close()
    summary = run_rollout(db, 'FIRM-1', 'atomic_all_gates')
    by_step = steps(summary)
    assert by_step[('D1', 'install')]['status'] == 'manual'
    assert state(db, 'D1')['version'] == 'v1'
    assert any(m['device'] == 'D1' and m['reason'] == 'operation_id_conflict'
               for m in summary['manual'])
    assert summary['outcome'] == 'manual_required'


def test_rollback_never_claims_commit_without_ledger(tmp_path):
    """A rollback is a compensation: it must be proven by the endpoint ledger."""
    from gateflow.gate_firmware import _operation_id
    db = make_db(tmp_path)
    with pytest.raises(CrashSimulated):
        run_rollout(db, 'FIRM-1', 'atomic_all_gates',
                    crash_hook=crash_on('D2', 'rollback', 'after_dispatch'))
    # Simulate the endpoint losing its own ledger entry while the live commit stands.
    conn = connect(db)
    op = _operation_id('FIRM-1', 'atomic_all_gates', 'D2', 'rollback')
    conn.execute('DELETE FROM gate_firmware_operation WHERE id=?', (op,))
    conn.commit(); conn.close()
    summary = run_rollout(db, 'FIRM-1', 'atomic_all_gates')
    by_step = steps(summary)
    assert state(db, 'D2')['version'] == 'v1'
    assert by_step[('D2', 'rollback')]['status'] == 'manual'
    assert any(m['device'] == 'D2' and m['reason'] == 'rollback_without_operation_ledger'
               for m in summary['manual'])
    assert summary['outcome'] == 'manual_required'
    # Rerun never re-dispatches this rollback under a fresh identity.
    again = run_rollout(db, 'FIRM-1', 'atomic_all_gates')
    assert steps(again)[('D2', 'rollback')]['status'] == 'manual'


def test_manual_obligation_survives_rerun(tmp_path):
    db = make_db(tmp_path)
    with pytest.raises(CrashSimulated):
        run_rollout(db, 'FIRM-1', 'atomic_all_gates',
                    crash_hook=crash_on('D3', 'install', 'after_intent'))
    conn = connect(db)
    conn.execute("UPDATE gate_firmware_device SET version='v3',revision=revision+1 WHERE id='D1'")
    conn.commit(); conn.close()
    first = run_rollout(db, 'FIRM-1', 'atomic_all_gates')
    assert first['outcome'] == 'manual_required'
    second = run_rollout(db, 'FIRM-1', 'atomic_all_gates')
    assert second['outcome'] == 'manual_required'
    assert [(m['device'], m['reason']) for m in second['manual']] == \
           [(m['device'], m['reason']) for m in first['manual']]
    assert state(db, 'D1')['version'] == 'v3'  # still untouched


@pytest.mark.parametrize('contract', CONTRACTS)
def test_rerun_same_case_is_idempotent(tmp_path, contract):
    db = make_db(tmp_path)
    first = run_rollout(db, 'FIRM-1', contract)
    conn = connect(db)
    ops_before = [dict(r) for r in conn.execute('SELECT * FROM gate_firmware_operation ORDER BY id')]
    revisions_before = {d: state(db, d)['revision'] for d in ('D1', 'D2', 'D3')}
    conn.close()
    second = run_rollout(db, 'FIRM-1', contract)
    conn = connect(db)
    ops_after = [dict(r) for r in conn.execute('SELECT * FROM gate_firmware_operation ORDER BY id')]
    conn.close()
    assert first['ended_at'] == second['ended_at']
    assert ops_before == ops_after
    assert revisions_before == {d: state(db, d)['revision'] for d in ('D1', 'D2', 'D3')}
    assert second['outcome'] == first['outcome']


def test_repeated_operation_id_used_by_second_plan_is_localised(tmp_path):
    db = make_db(tmp_path)
    conn = connect(db)
    # Pretend an earlier plan already committed D3 under this plan's operation id.
    conn.execute(
        'INSERT INTO gate_firmware_operation VALUES (?,?,?,?)',
        ('fixed-op', 'D1', 'install', json.dumps({'status': 'applied', 'version': 'v2', 'revision': 1}, sort_keys=True)),
    )
    conn.commit(); conn.close()
    # Direct endpoint call with a different device under the same id is refused.
    assert invoke(db, 'FIRM-1', 'D2', 'install', 'v2', 'fixed-op', 0)['status'] == 'operation_id_conflict'
    assert state(db, 'D2')['version'] == 'v1'


# ---------------------------------------------------------------------------
# Failure handling that must stay manual
# ---------------------------------------------------------------------------

def _custom_case(tmp_path, mutate):
    doc = json.loads((FIX / 'gate-firmware-case.json').read_text(encoding='utf-8'))
    mutate(doc)
    fixture = tmp_path / 'case.json'
    fixture.write_text(json.dumps(doc), encoding='utf-8')
    db = tmp_path / 'firmware.db'
    initialize(db, FIX / 'gate-firmware-config.json')
    ingest(FIX / 'gate-firmware-events.jsonl', db)
    register(db, 'FIRM-1', fixture)
    return db


def test_half_upgraded_unit_never_declared_complete(tmp_path):
    """D2 refuses; D1 must not stay alone on v2 inside unit G1/CELL-1."""
    def mutate(doc):
        doc['devices'][1] = {k: v for k, v in doc['devices'][1].items() if k != 'failure'}
        doc['devices'][1]['failure'] = 'reject'
    db = _custom_case(tmp_path, mutate)
    summary = run_rollout(db, 'FIRM-1', 'zone_isolated')
    assert state(db, 'D1')['version'] == 'v1'
    assert state(db, 'D2')['version'] == 'v1'
    assert summary['detail']['units']['G1/CELL-1']['status'] == 'rolled_back'
    assert summary['outcome'] == 'rolled_back'  # no unit was left half-upgraded or claimed done
    assert summary['detail']['units']['G1/CELL-1']['devices'] == {'D1': 'v1', 'D2': 'v1'}
    assert steps(summary)[('D1', 'rollback')]['status'] == 'confirmed'


def test_concurrent_change_between_rollbacks_is_manual(tmp_path):
    """Another maintainer bumps D1 after install; rollback must stop, not overwrite."""
    db = make_db(tmp_path)
    with pytest.raises(CrashSimulated):
        run_rollout(db, 'FIRM-1', 'atomic_all_gates',
                    crash_hook=crash_on('D3', 'install', 'after_intent'))
    conn = connect(db)
    conn.execute("UPDATE gate_firmware_device SET version='v3',revision=revision+1 WHERE id='D1'")
    conn.commit(); conn.close()
    summary = run_rollout(db, 'FIRM-1', 'atomic_all_gates')
    by_step = steps(summary)
    assert by_step[('D1', 'rollback')]['status'] == 'manual'
    assert state(db, 'D1')['version'] == 'v3'
    assert any(m['device'] == 'D1' and m['reason'] == 'concurrent_change_before_rollback'
               for m in summary['manual'])
    assert summary['outcome'] == 'manual_required'
    # D2 was modified only by this run and is still rolled back; D1 is left to a human.
    assert state(db, 'D2')['version'] == 'v1'


def test_unreachable_device_ends_manual_and_never_silently_skipped(tmp_path):
    def mutate(doc):
        doc['devices'][0]['failure'] = 'unreachable'
    db = _custom_case(tmp_path, mutate)
    summary = run_rollout(db, 'FIRM-1', 'atomic_all_gates', max_attempts=2)
    by_step = steps(summary)
    assert by_step[('D1', 'install')]['status'] == 'unreachable'
    assert by_step[('D1', 'install')]['attempt'] == 2
    assert state(db, 'D1')['version'] == 'v1'
    assert summary['outcome'] == 'manual_required'
    assert any(m['device'] == 'D1' and m['reason'] == 'endpoint_unreachable' for m in summary['manual'])
    # Every device still has an explicit step: none omitted.
    assert {s['device'] for s in summary['steps']} == {'D1', 'D2', 'D3'}


def test_rollback_reject_stays_manual_without_fake_restore(tmp_path):
    def mutate(doc):
        doc['devices'][1]['rollback_failure'] = 'reject'
    db = _custom_case(tmp_path, mutate)
    summary = run_rollout(db, 'FIRM-1', 'atomic_all_gates')
    by_step = steps(summary)
    assert by_step[('D2', 'rollback')]['status'] == 'rejected'
    assert state(db, 'D2')['version'] == 'v2'
    assert summary['outcome'] == 'manual_required'
    assert any(m['device'] == 'D2' for m in summary['manual'])


# ---------------------------------------------------------------------------
# Evidence report
# ---------------------------------------------------------------------------

def test_evidence_report_traces_gate_sources_and_actual_versions(tmp_path):
    db = make_db(tmp_path)
    run_rollout(db, 'FIRM-1', 'atomic_all_gates')
    out = evidence_report(db, 'FIRM-1', 'atomic_all_gates', tmp_path / 'report.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    assert data['contract_authorized_by'] == 'VENUE-OPS'
    assert data['final_versions'] == {'D1': 'v1', 'D2': 'v1', 'D3': 'v1'}
    assert data['target_version'] == 'v2'
    assert set(data['gates']) == {'G1', 'G2'}
    assert data['gates']['G1']['physical_versions'] == {'D1': 'v1', 'D2': 'v1'}
    assert data['gates']['G2']['physical_versions'] == {'D3': 'v1'}
    for gate in data['gates'].values():
        for src in gate['sources'].values():
            assert src['event_id'] and src['event_sha256'] and src['source_sha256']
    d2 = next(d for d in data['devices'] if d['device'] == 'D2')
    assert d2['steps']['install']['status'] == 'confirmed'
    assert d2['steps']['install']['observed_version'] == 'v2'
    assert d2['actual_version'] == 'v1'
    assert d2['steps']['rollback']['operation_id']


def test_reports_for_the_two_contracts_differ_only_in_real_outcome(tmp_path):
    atomic = make_db(tmp_path, 'a.db')
    zoned = make_db(tmp_path, 'z.db')
    run_rollout(atomic, 'FIRM-1', 'atomic_all_gates')
    run_rollout(zoned, 'FIRM-1', 'zone_isolated')
    a = json.loads(evidence_report(atomic, 'FIRM-1', 'atomic_all_gates', tmp_path / 'a.json').read_text(encoding='utf-8'))
    z = json.loads(evidence_report(zoned, 'FIRM-1', 'zone_isolated', tmp_path / 'z.json').read_text(encoding='utf-8'))
    assert a['final_versions'] != z['final_versions']
    assert {d['device']: d['event_sha256'] for d in a['devices']} == {
        d['device']: d['event_sha256'] for d in z['devices']}


def test_report_rejects_unknown_contract(tmp_path):
    db = make_db(tmp_path)
    with pytest.raises(ValueError, match='unknown_gate_firmware_contract'):
        evidence_report(db, 'FIRM-1', 'nope', tmp_path / 'x.json')
    with pytest.raises(ValueError, match='unknown_gate_firmware_contract'):
        run_rollout(db, 'FIRM-1', 'nope')


def test_run_unknown_case_and_device_fail_loudly(tmp_path):
    db = make_db(tmp_path)
    with pytest.raises(ValueError, match='unknown_gate_firmware_case'):
        run_rollout(db, 'MISSING', 'atomic_all_gates')
    with pytest.raises(ValueError, match='unknown_gate_firmware_device'):
        state(db, 'NOPE')


# ---------------------------------------------------------------------------
# Migration + SQLite integrity
# ---------------------------------------------------------------------------

def test_old_database_is_migrated_in_place(tmp_path):
    db = tmp_path / 'old.db'
    old = sqlite3.connect(db)
    old.executescript("""
    CREATE TABLE gate_firmware_device(id TEXT PRIMARY KEY,gate TEXT NOT NULL,cell TEXT NOT NULL,version TEXT NOT NULL,revision INTEGER NOT NULL);
    CREATE TABLE gate_firmware_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL);
    CREATE TABLE gate_firmware_operation(id TEXT PRIMARY KEY,device TEXT NOT NULL,command TEXT NOT NULL,result TEXT NOT NULL);
    CREATE TABLE evidence(id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, source_sha256 TEXT NOT NULL, payload_sha256 TEXT NOT NULL, role TEXT NOT NULL, UNIQUE(event_id,role));
    """)
    old.execute("INSERT INTO gate_firmware_device VALUES ('D1','G1','CELL-1','v1',0)")
    old.commit(); old.close()
    conn = connect(db)
    columns = {r[1] for r in conn.execute('PRAGMA table_info(gate_firmware_step)')}
    assert {'operation_id', 'expected_revision', 'status_recheck', 'observed_version'} <= columns
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert conn.execute("SELECT version FROM gate_firmware_device WHERE id='D1'").fetchone()[0] == 'v1'
    conn.close()


def test_full_flow_leaves_foreign_keys_intact(tmp_path):
    db = make_db(tmp_path)
    run_rollout(db, 'FIRM-1', 'atomic_all_gates')
    conn = connect(db)
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert conn.execute('SELECT count(*) FROM gate_firmware_run').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM evidence').fetchone()[0] == 3
    conn.close()
