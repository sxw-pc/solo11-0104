import json
import csv
from pathlib import Path
import pytest
from gateflow.__main__ import initialize, climate_demo
from gateflow.climate_control import (
    register, plan, plan_all, execute, apply_feedback, report,
    CONTRACTS, STATUS_FEASIBLE, STATUS_REVIEW, STATUS_INFEASIBLE, STATUS_UNRECOVERABLE,
)
from gateflow.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'
BASE = json.loads((FIX / 'climate-case.json').read_text(encoding='utf-8'))
SENSORS = (
    'zone,at,temp_c,sensor\n'
    'A,2026-01-01T08:00:00+00:00,20.0,TA\n'
    'B,2026-01-01T08:00:00+00:00,20.0,TB\n'
)


def build_case(tmp_path, case=None, sensors=SENSORS):
    directory = tmp_path / 'case'
    directory.mkdir()
    (directory / 'sensors.csv').write_text(sensors, encoding='utf-8')
    doc = json.loads(json.dumps(case if case is not None else BASE))
    doc['sensors_file'] = 'sensors.csv'
    (directory / 'case.json').write_text(json.dumps(doc), encoding='utf-8')
    db = tmp_path / 'climate.db'
    initialize(db, FIX / 'climate-config.json')
    register(db, 'C', directory / 'case.json')
    return db


def fixture_db(tmp_path):
    db = tmp_path / 'climate.db'
    initialize(db, FIX / 'climate-config.json')
    register(db, 'CLIMATE-1', FIX / 'climate-case.json')
    return db


def rows_by_slot(payload):
    return {row['slot']: row for row in payload['slots']}


# ---------------------------------------------------------------------------
# the two frozen contracts
# ---------------------------------------------------------------------------

def test_min_energy_contract_cools_a_in_slot_zero(tmp_path):
    plan_payload = plan(fixture_db(tmp_path), 'CLIMATE-1', 'min_energy')
    rows = rows_by_slot(plan_payload)
    assert plan_payload['status'] == STATUS_FEASIBLE
    assert plan_payload['energy_cents'] == 100
    assert plan_payload['total_exposure'] == 6
    assert plan_payload['max_zone_exposure'] == 4
    assert plan_payload['zone_exposure'] == {'A': 2, 'B': 4}
    assert rows[0]['action']['zones'] == ['A']
    assert rows[1]['action']['type'] == 'none'
    assert plan_payload['commands'] == [
        {'action': 'start_cooling', 'zone': 'A', 'slot': 0, 'case_id': 'CLIMATE-1', 'contract': 'min_energy'}
    ]


def test_min_max_exposure_contract_cools_a_then_b(tmp_path):
    plan_payload = plan(fixture_db(tmp_path), 'CLIMATE-1', 'min_max_zone_exposure')
    rows = rows_by_slot(plan_payload)
    assert plan_payload['energy_cents'] == 1100
    assert plan_payload['total_exposure'] == 4
    assert plan_payload['max_zone_exposure'] == 2
    assert plan_payload['zone_exposure'] == {'A': 2, 'B': 2}
    assert rows[0]['action']['zones'] == ['A']
    assert rows[1]['action']['zones'] == ['B']
    assert [(c['slot'], c['zone']) for c in plan_payload['commands']] == [(0, 'A'), (1, 'B')]


def test_both_contracts_respect_total_budget(tmp_path):
    db = fixture_db(tmp_path)
    for contract in CONTRACTS:
        payload = plan(db, 'CLIMATE-1', contract)
        assert payload['budget_ok'] is True
        assert payload['total_exposure'] <= payload['total_exposure_budget']
        assert payload['conservation']['exposure_sum_equals_final_cumulative'] is True
        assert payload['conservation']['temperature_path_consistent'] is True
        assert payload['conservation']['shared_device_respected'] is True


def test_contract_changes_actions_and_cost(tmp_path):
    db = fixture_db(tmp_path)
    energy = plan(db, 'CLIMATE-1', 'min_energy')
    exposure = plan(db, 'CLIMATE-1', 'min_max_zone_exposure')
    assert energy['commands'] != exposure['commands']
    assert energy['max_zone_exposure'] == 4 and exposure['max_zone_exposure'] == 2
    assert energy['energy_cents'] < exposure['energy_cents']


# ---------------------------------------------------------------------------
# exact optimum witness
# ---------------------------------------------------------------------------

def test_optimality_witness_matches_brute_force(tmp_path):
    db = fixture_db(tmp_path)
    for contract in CONTRACTS:
        payload = plan(db, 'CLIMATE-1', contract)
        opt = payload['optimality']
        assert opt['exact'] is True
        assert opt['brute_force_checked'] is True
        assert opt['brute_force_matches'] is True
        assert opt['brute_force_goal_vector'] == opt['goal_vector']
        assert opt['no_better']


def test_global_optimum_beats_greedy(tmp_path):
    """Cooling the hottest zone per slot is not optimal for max exposure."""
    payload = plan(fixture_db(tmp_path), 'CLIMATE-1', 'min_max_zone_exposure')
    assert payload['max_zone_exposure'] == 2
    # A greedy "cool the currently hottest zone" would leave one zone at 4.
    assert payload['zone_exposure'] == {'A': 2, 'B': 2}


def test_tie_break_is_stable(tmp_path):
    db = fixture_db(tmp_path)
    first = plan(db, 'CLIMATE-1', 'min_energy')
    second = plan(db, 'CLIMATE-1', 'min_energy')
    assert first['digest'] == second['digest']
    assert first['plan_id'] == second['plan_id']


def test_plan_all_covers_both_contracts(tmp_path):
    payloads = plan_all(fixture_db(tmp_path), 'CLIMATE-1')
    assert set(payloads) == set(CONTRACTS)


# ---------------------------------------------------------------------------
# shared device and feasibility
# ---------------------------------------------------------------------------

def test_shared_device_serves_one_zone_per_slot(tmp_path):
    case = json.loads(json.dumps(BASE))
    case['shared_cooling_capacity'] = 2  # still a single shared unit
    payload = plan(build_case(tmp_path, case), 'C', 'min_max_zone_exposure')
    for row in payload['slots']:
        assert row['device']['used'] <= 1
        assert len(row['action']['zones']) <= 1


def test_both_zones_never_cooled_in_same_slot(tmp_path):
    payload = plan(fixture_db(tmp_path), 'CLIMATE-1', 'min_energy')
    for row in payload['slots']:
        assert not (set(row['action']['zones']) >= {'A', 'B'})


def test_no_feasible_plan_reports_no_commands(tmp_path):
    case = json.loads(json.dumps(BASE))
    case['total_exposure_budget'] = 1
    payload = plan(build_case(tmp_path, case), 'C', 'min_energy')
    assert payload['status'] == STATUS_INFEASIBLE
    assert payload['feasible'] is False
    assert payload['commands'] == []
    assert any(issue['code'] == 'no_feasible_plan' for issue in payload['issues'])
    assert all(row['action']['type'] == 'blocked' for row in payload['slots'])


def test_budget_exactly_met_is_feasible(tmp_path):
    """The min_energy plan uses all 6 units: exactly at capacity is allowed."""
    payload = plan(fixture_db(tmp_path), 'CLIMATE-1', 'min_energy')
    assert payload['total_exposure'] == 6
    assert payload['total_exposure_budget'] == 6
    assert payload['status'] == STATUS_FEASIBLE


def test_device_with_extra_capacity_still_solves(tmp_path):
    case = json.loads(json.dumps(BASE))
    case['shared_cooling_capacity'] = 3
    payload = plan(build_case(tmp_path, case), 'C', 'min_energy')
    assert payload['status'] == STATUS_FEASIBLE


# ---------------------------------------------------------------------------
# evidence validation: one defect blocks the whole plan
# ---------------------------------------------------------------------------

def _assert_blocked(payload, code):
    assert payload['status'] == STATUS_REVIEW
    assert payload['feasible'] is False
    assert payload['commands'] == []
    assert any(issue['code'] == code for issue in payload['issues'])


def test_missing_heat_entry_blocks_plan(tmp_path):
    case = json.loads(json.dumps(BASE))
    case['zones'][0]['heat_c_per_slot'] = [3]
    payload = plan(build_case(tmp_path, case), 'C', 'min_energy')
    _assert_blocked(payload, 'heat_missing')


def test_nonphysical_cooling_blocks_plan(tmp_path):
    for bad in (0, -2, 'abc', None):
        case = json.loads(json.dumps(BASE))
        case['cooling_c_per_action'] = bad
        tmp = tmp_path / f'bad-{bad}'
        tmp.mkdir()
        payload = plan(build_case(tmp, case), 'C', 'min_energy')
        _assert_blocked(payload, 'cooling_nonphysical')


def test_missing_sensor_reading_blocks_plan(tmp_path):
    sensors = 'zone,at,temp_c,sensor\nA,2026-01-01T08:00:00+00:00,20.0,TA\n'
    payload = plan(build_case(tmp_path, sensors=sensors), 'C', 'min_energy')
    _assert_blocked(payload, 'reading_missing')


def test_partial_slot_readings_block_plan(tmp_path):
    sensors = SENSORS + 'A,2026-01-01T09:00:00+00:00,21.0,TA\n'
    payload = plan(build_case(tmp_path, sensors=sensors), 'C', 'min_energy')
    _assert_blocked(payload, 'reading_missing')


def test_sensor_identity_conflict_blocks_plan(tmp_path):
    sensors = (
        'zone,at,temp_c,sensor\n'
        'A,2026-01-01T08:00:00+00:00,20.0,TA\n'
        'B,2026-01-01T08:00:00+00:00,20.0,TA\n'
    )
    payload = plan(build_case(tmp_path, sensors=sensors), 'C', 'min_energy')
    _assert_blocked(payload, 'sensor_identity_conflict')


def test_timezone_misalignment_blocks_plan(tmp_path):
    sensors = (
        'zone,at,temp_c,sensor\n'
        'A,2026-01-01T08:00:00,20.0,TA\n'
        'B,2026-01-01T08:00:00+00:00,20.0,TB\n'
    )
    payload = plan(build_case(tmp_path, sensors=sensors), 'C', 'min_energy')
    assert payload['status'] == STATUS_REVIEW
    assert payload['commands'] == []
    assert any(issue['code'] in ('timezone_error', 'timezone_misaligned') for issue in payload['issues'])


def test_unknown_zone_reading_blocks_plan(tmp_path):
    sensors = SENSORS + 'Z,2026-01-01T08:00:00+00:00,20.0,TZ\n'
    payload = plan(build_case(tmp_path, sensors=sensors), 'C', 'min_energy')
    _assert_blocked(payload, 'reading_invalid')


def test_zero_capacity_blocks_plan(tmp_path):
    case = json.loads(json.dumps(BASE))
    case['shared_cooling_capacity'] = 0
    payload = plan(build_case(tmp_path, case), 'C', 'min_energy')
    _assert_blocked(payload, 'capacity_insufficient')


# ---------------------------------------------------------------------------
# execution lifecycle
# ---------------------------------------------------------------------------

def test_execute_records_commands_and_is_idempotent(tmp_path):
    db = fixture_db(tmp_path)
    plan(db, 'CLIMATE-1', 'min_max_zone_exposure')
    first = execute(db, 'CLIMATE-1', 'min_max_zone_exposure')
    assert [(c['slot'], c['zone']) for c in first['commands']] == [(0, 'A'), (1, 'B')]
    second = execute(db, 'CLIMATE-1', 'min_max_zone_exposure')
    assert second['commands'] == []
    conn = connect(db)
    try:
        assert conn.execute('SELECT COUNT(*) FROM climate_execution').fetchone()[0] == 2
    finally:
        conn.close()


def test_execute_rejects_blocked_plan(tmp_path):
    case = json.loads(json.dumps(BASE))
    case['total_exposure_budget'] = 1
    db = build_case(tmp_path, case)
    plan(db, 'C', 'min_energy')
    with pytest.raises(ValueError, match='plan_not_feasible'):
        execute(db, 'C', 'min_energy')


def test_execute_named_slot_without_command_is_rejected(tmp_path):
    db = fixture_db(tmp_path)
    plan(db, 'CLIMATE-1', 'min_energy')
    with pytest.raises(ValueError, match='no_command'):
        execute(db, 'CLIMATE-1', 'min_energy', slots=[1])


def test_execution_marks_slot_rows(tmp_path):
    db = fixture_db(tmp_path)
    plan(db, 'CLIMATE-1', 'min_max_zone_exposure')
    execute(db, 'CLIMATE-1', 'min_max_zone_exposure')
    conn = connect(db)
    try:
        executed = conn.execute('SELECT COUNT(*) FROM climate_slot_plan WHERE executed=1').fetchone()[0]
        assert executed == 2
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# late feedback
# ---------------------------------------------------------------------------

def _feedback_file(tmp_path, rows):
    path = tmp_path / 'feedback.csv'
    path.write_text('zone,at,temp_c,sensor\n' + ''.join(rows), encoding='utf-8')
    return path


def test_late_feedback_corrects_remaining_start_and_keeps_executed(tmp_path):
    db = fixture_db(tmp_path)
    plan(db, 'CLIMATE-1', 'min_energy')
    execute(db, 'CLIMATE-1', 'min_energy', slots=[0])
    feedback = _feedback_file(
        tmp_path,
        'A,2026-01-01T09:00:00+00:00,22.0,TA\nB,2026-01-01T09:00:00+00:00,21.0,TB\n',
    )
    payload = apply_feedback(db, 'CLIMATE-1', 'min_energy', feedback)
    assert payload['version'] == 2
    rows = rows_by_slot(payload)
    assert rows[0]['executed'] is True
    assert rows[0]['action']['zones'] == ['A']           # executed command unchanged
    assert rows[1]['start_temps'] == {'A': 22, 'B': 21}  # corrected future start
    assert payload['deviation']['feedback_slot'] == 1
    assert payload['deviation']['delta_c'] == {'A': 1, 'B': -1}
    assert payload['deviation']['executed_actions_unchanged'] == [{'slot': 0, 'zone': 'A'}]


def test_feedback_does_not_rewrite_executed_command(tmp_path):
    db = fixture_db(tmp_path)
    plan(db, 'CLIMATE-1', 'min_max_zone_exposure')
    execute(db, 'CLIMATE-1', 'min_max_zone_exposure', slots=[0])
    feedback = _feedback_file(
        tmp_path,
        'A,2026-01-01T09:00:00+00:00,21.0,TA\nB,2026-01-01T09:00:00+00:00,21.0,TB\n',
    )
    payload = apply_feedback(db, 'CLIMATE-1', 'min_max_zone_exposure', feedback)
    rows = rows_by_slot(payload)
    assert rows[0]['action']['zones'] == ['A']
    assert rows[0]['executed'] is True
    conn = connect(db)
    try:
        commands = [dict(row) for row in conn.execute('SELECT slot,zone FROM climate_execution ORDER BY slot')]
    finally:
        conn.close()
    assert commands[0] == {'slot': 0, 'zone': 'A'}


def test_feedback_that_breaks_budget_is_unrecoverable(tmp_path):
    db = fixture_db(tmp_path)
    plan(db, 'CLIMATE-1', 'min_energy')
    execute(db, 'CLIMATE-1', 'min_energy', slots=[0])
    # A measured 30/30 at slot 1 means slot 0 already caused huge exposure
    feedback = _feedback_file(
        tmp_path,
        'A,2026-01-01T09:00:00+00:00,30.0,TA\nB,2026-01-01T09:00:00+00:00,30.0,TB\n',
    )
    payload = apply_feedback(db, 'CLIMATE-1', 'min_energy', feedback)
    assert payload['status'] == STATUS_UNRECOVERABLE
    assert payload['feasible'] is False
    assert payload['commands'] == []
    assert any(issue['code'] == 'exposure_budget_exceeded_unrecoverable' for issue in payload['issues'])
    assert payload['committed_exposure'] > payload['total_exposure_budget']


def test_feedback_missing_zone_blocks_new_plan(tmp_path):
    db = fixture_db(tmp_path)
    plan(db, 'CLIMATE-1', 'min_energy')
    execute(db, 'CLIMATE-1', 'min_energy', slots=[0])
    feedback = _feedback_file(tmp_path, 'A,2026-01-01T09:00:00+00:00,21.0,TA\n')
    payload = apply_feedback(db, 'CLIMATE-1', 'min_energy', feedback)
    assert payload['status'] == STATUS_REVIEW
    assert any(issue['code'] == 'reading_missing' for issue in payload['issues'])
    assert payload['commands'] == []


def test_feedback_repeat_is_stable(tmp_path):
    db = fixture_db(tmp_path)
    plan(db, 'CLIMATE-1', 'min_energy')
    feedback = _feedback_file(
        tmp_path,
        'A,2026-01-01T09:00:00+00:00,21.0,TA\nB,2026-01-01T09:00:00+00:00,21.0,TB\n',
    )
    first = apply_feedback(db, 'CLIMATE-1', 'min_energy', feedback)
    second = apply_feedback(db, 'CLIMATE-1', 'min_energy', feedback)
    assert first['digest'] == second['digest']
    assert first['plan_id'] == second['plan_id']


# ---------------------------------------------------------------------------
# evidence report and persistence
# ---------------------------------------------------------------------------

def test_report_contains_evidence_and_optimality(tmp_path):
    db = fixture_db(tmp_path)
    plan_all(db, 'CLIMATE-1')
    execute(db, 'CLIMATE-1', 'min_energy')
    path = report(db, 'CLIMATE-1', tmp_path / 'out' / 'climate.json')
    data = json.loads(path.read_text(encoding='utf-8'))
    assert data['case_id'] == 'CLIMATE-1'
    assert {p['contract'] for p in data['plans']} == set(CONTRACTS)
    for plan_report in data['plans']:
        assert plan_report['source']['sensor_sha256']
        assert plan_report['source']['case_digest']
        assert plan_report['optimality']['method'] == 'exact_dynamic_programming'
        for slot in plan_report['slots']:
            assert 'start_temps' in slot and 'end_temps' in slot
            assert 'source_readings' in slot
            assert 'energy_cents' in slot
    assert path.with_suffix('.md').exists()


def test_store_tables_hold_slots_and_issues(tmp_path):
    db = fixture_db(tmp_path)
    plan_all(db, 'CLIMATE-1')
    conn = connect(db)
    try:
        plans = conn.execute('SELECT COUNT(*) FROM climate_plan').fetchone()[0]
        slots = conn.execute('SELECT COUNT(*) FROM climate_slot_plan').fetchone()[0]
        pristine = conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        conn.close()
    assert plans == 2
    assert slots == 4
    assert pristine == []


def test_climate_demo_command(tmp_path):
    result = climate_demo(tmp_path)
    data = json.loads(Path(result['report']).read_text(encoding='utf-8'))
    assert data['plans']
    assert Path(result['db']).exists()


def test_register_is_idempotent_and_conflict_safe(tmp_path):
    db = fixture_db(tmp_path)
    again = register(db, 'CLIMATE-1', FIX / 'climate-case.json')
    assert again['readings'] == 2
    other = json.loads(json.dumps(BASE))
    other['zones'][0]['max_c'] = 99
    directory = tmp_path / 'other'
    directory.mkdir()
    (directory / 'climate-sensors.csv').write_text(SENSORS, encoding='utf-8')
    (directory / 'case.json').write_text(json.dumps(other), encoding='utf-8')
    with pytest.raises(ValueError, match='climate_case_conflict'):
        register(db, 'CLIMATE-1', directory / 'case.json')


# ---------------------------------------------------------------------------
# metric conservation
# ---------------------------------------------------------------------------

def test_exposure_is_sum_over_slots_not_peak_reading(tmp_path):
    """Exposure counts every slot's over-limit amount, not the highest reading."""
    case = json.loads(json.dumps(BASE))
    case['total_exposure_budget'] = 100
    payload = plan(build_case(tmp_path, case), 'C', 'min_energy')
    rows = rows_by_slot(payload)
    total = sum(sum(row['exposure'].values()) for row in payload['slots'])
    assert total == payload['total_exposure']
    # with no cooling at all A ends at 26 / 29 and B at 24 / 26:
    # over-limit = A(4+7) + B(3+5) = 19, far above the peak-reading gap
    assert any(row['slot_exposure'] > 0 for row in rows.values())
    assert payload['conservation']['total_exposure'] == payload['total_exposure']


def test_missing_measurement_is_never_treated_as_zero(tmp_path):
    sensors = 'zone,at,temp_c,sensor\nA,2026-01-01T08:00:00+00:00,20.0,TA\n'
    payload = plan(build_case(tmp_path, sensors=sensors), 'C', 'min_energy')
    assert payload['status'] == STATUS_REVIEW
    assert payload['total_exposure'] is None
    assert payload['commands'] == []


# ---------------------------------------------------------------------------
# explicit edge cases named by the brief
# ---------------------------------------------------------------------------

def test_double_zone_request_in_one_slot_is_infeasible(tmp_path):
    """Both zones need cooling in the same only slot; the shared unit cannot."""
    case = json.loads(json.dumps(BASE))
    case['zones'][0]['max_c'] = 21
    case['zones'][1]['max_c'] = 20
    case['slots'] = [{'id': 0, 'start': '2026-01-01T08:00:00+00:00', 'energy_cents': 100}]
    for zone in case['zones']:
        zone['heat_c_per_slot'] = [3]
    case['total_exposure_budget'] = 0
    payload = plan(build_case(tmp_path, case), 'C', 'min_energy')
    assert payload['status'] == STATUS_INFEASIBLE
    assert payload['commands'] == []


def test_device_exactly_full_is_still_executable(tmp_path):
    case = json.loads(json.dumps(BASE))
    case['total_exposure_budget'] = 100
    db = build_case(tmp_path, case)
    payload = plan(db, 'C', 'min_max_zone_exposure')
    # both slots use the single shared unit exactly once
    assert all(row['device']['used'] == 1 for row in payload['slots'])
    assert all(row['device']['used'] == row['device']['capacity'] for row in payload['slots'])
    executed = execute(db, 'C', 'min_max_zone_exposure')
    assert len(executed['commands']) == 2


def test_plan_distinguishes_executed_from_pending(tmp_path):
    db = fixture_db(tmp_path)
    payload = plan(db, 'CLIMATE-1', 'min_max_zone_exposure')
    execute(db, 'CLIMATE-1', 'min_max_zone_exposure', slots=[0])
    replanned = plan(db, 'CLIMATE-1', 'min_max_zone_exposure')
    rows = rows_by_slot(replanned)
    assert rows[0]['executed'] is True
    assert rows[1]['executed'] is False
    pending = [(c['slot'], c['zone']) for c in replanned['commands']]
    assert pending == [(1, 'B')]
    assert replanned['executed_actions'][0]['slot'] == 0


def test_replan_after_execution_does_not_change_executed_action(tmp_path):
    db = fixture_db(tmp_path)
    first = plan(db, 'CLIMATE-1', 'min_max_zone_exposure')
    assert rows_by_slot(first)[0]['action']['zones'] == ['A']
    execute(db, 'CLIMATE-1', 'min_max_zone_exposure', slots=[0])
    replanned = plan(db, 'CLIMATE-1', 'min_max_zone_exposure')
    assert rows_by_slot(replanned)[0]['action']['zones'] == ['A']
    assert replanned['source']['input_digest'] != first['source']['input_digest']


def test_energy_and_exposure_metrics_are_conserved(tmp_path):
    db = fixture_db(tmp_path)
    for contract in CONTRACTS:
        payload = plan(db, 'CLIMATE-1', contract)
        conservation = payload['conservation']
        assert conservation['energy_equals_sum_of_slot_costs'] is True
        assert conservation['energy_cents'] == sum(row['energy_used_cents'] for row in payload['slots'])
        assert conservation['exposure_sum_equals_final_cumulative'] is True
        assert conservation['total_exposure_matches_plan'] is True
        assert conservation['total_exposure'] == payload['total_exposure']
        assert conservation['temperature_path_consistent'] is True


def test_measurement_without_execution_still_counts_exposure(tmp_path):
    """An elapsed slot measured late caused exposure even with no command."""
    db = fixture_db(tmp_path)
    plan(db, 'CLIMATE-1', 'min_energy')
    feedback = _feedback_file(
        tmp_path,
        'A,2026-01-01T09:00:00+00:00,22.0,TA\nB,2026-01-01T09:00:00+00:00,21.0,TB\n',
    )
    payload = apply_feedback(db, 'CLIMATE-1', 'min_energy', feedback)
    assert payload['feasible'] is True
    rows = rows_by_slot(payload)
    # slot 0 was not executed and is now past; no command may be added to it
    assert rows[0]['action']['type'] == 'none'
    assert rows[1]['start_temps'] == {'A': 22, 'B': 21}
    assert payload['total_exposure'] == 5
    assert payload['commands'] == []


def test_measurement_without_execution_can_break_budget(tmp_path):
    db = fixture_db(tmp_path)
    plan(db, 'CLIMATE-1', 'min_energy')
    feedback = _feedback_file(
        tmp_path,
        'A,2026-01-01T09:00:00+00:00,30.0,TA\nB,2026-01-01T09:00:00+00:00,30.0,TB\n',
    )
    payload = apply_feedback(db, 'CLIMATE-1', 'min_energy', feedback)
    assert payload['status'] == STATUS_UNRECOVERABLE
    assert payload['commands'] == []
    assert payload['committed_exposure'] > payload['total_exposure_budget']


def test_plan_after_feedback_is_stable(tmp_path):
    db = fixture_db(tmp_path)
    plan(db, 'CLIMATE-1', 'min_energy')
    feedback = _feedback_file(
        tmp_path,
        'A,2026-01-01T09:00:00+00:00,22.0,TA\nB,2026-01-01T09:00:00+00:00,21.0,TB\n',
    )
    after_feedback = apply_feedback(db, 'CLIMATE-1', 'min_energy', feedback)
    again = plan(db, 'CLIMATE-1', 'min_energy')
    assert again['plan_id'] == after_feedback['plan_id']


def test_extra_cooling_capacity_is_still_single_zone(tmp_path):
    """A nonphysical shared capacity never lets A and B cool together."""
    case = json.loads(json.dumps(BASE))
    case['shared_cooling_capacity'] = 2
    payload = plan(build_case(tmp_path, case), 'C', 'min_energy')
    for row in payload['slots']:
        assert row['device']['used'] <= 1


def test_fractional_heat_cooling_are_exact(tmp_path):
    case = json.loads(json.dumps(BASE))
    case['cooling_c_per_action'] = 1.5
    case['zones'][0]['heat_c_per_slot'] = [2.5, 2.5]
    case['zones'][1]['heat_c_per_slot'] = [1.5, 1.5]
    payload = plan(build_case(tmp_path, case), 'C', 'min_max_zone_exposure')
    assert payload['status'] == STATUS_FEASIBLE
    assert payload['conservation']['exposure_sum_equals_final_cumulative'] is True
    assert payload['conservation']['temperature_path_consistent'] is True


def test_four_zone_eight_slot_case_solves(tmp_path):
    tmp = tmp_path
    directory = tmp / 'big'
    directory.mkdir()
    zones = ['A', 'B', 'C', 'D']
    case = {
        'sensors_file': 'sensors.csv',
        'slots': [{'id': i, 'start': f'2026-01-01T{8 + i:02d}:00:00+00:00', 'energy_cents': 1 + i}
                  for i in range(8)],
        'zones': [{'id': z, 'max_c': 34, 'heat_c_per_slot': [3] * 8} for z in zones],
        'cooling_c_per_action': 2,
        'shared_cooling_capacity': 1,
        'total_exposure_budget': 500,
        'contracts': list(CONTRACTS),
    }
    (directory / 'sensors.csv').write_text(
        'zone,at,temp_c,sensor\n' + ''.join(
            f'{z},2026-01-01T08:00:00+00:00,20.0,T{z}\n' for z in zones),
        encoding='utf-8',
    )
    (directory / 'case.json').write_text(json.dumps(case), encoding='utf-8')
    config = {
        'batch': [],
        'zone': [{'id': z, 'capacity': 2} for z in zones],
        'gate_map': [],
        'ticket': [],
    }
    (tmp / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    db = tmp / 'big.db'
    initialize(db, tmp / 'config.json')
    register(db, 'BIG', directory / 'case.json')
    payload = plan(db, 'BIG', 'min_max_zone_exposure')
    assert payload['status'] == STATUS_FEASIBLE
    assert len(payload['slots']) == 8
    assert payload['optimality']['exact'] is True
    for row in payload['slots']:
        assert row['device']['used'] <= 1


def test_feedback_with_conflicting_values_blocks_plan(tmp_path):
    db = fixture_db(tmp_path)
    plan(db, 'CLIMATE-1', 'min_energy')
    execute(db, 'CLIMATE-1', 'min_energy', slots=[0])
    feedback = _feedback_file(
        tmp_path,
        'A,2026-01-01T09:00:00+00:00,21.0,TA\n'
        'A,2026-01-01T09:00:00+00:00,23.0,TA\n'
        'B,2026-01-01T09:00:00+00:00,21.0,TB\n',
    )
    payload = apply_feedback(db, 'CLIMATE-1', 'min_energy', feedback)
    assert payload['status'] == STATUS_REVIEW
    assert payload['commands'] == []
    assert any(issue['code'] == 'feedback_conflict' for issue in payload['issues'])


def test_execute_is_all_or_nothing(tmp_path):
    db = fixture_db(tmp_path)
    plan(db, 'CLIMATE-1', 'min_energy')
    with pytest.raises(ValueError, match='no_command'):
        execute(db, 'CLIMATE-1', 'min_energy', slots=[0, 1])
    conn = connect(db)
    try:
        assert conn.execute('SELECT COUNT(*) FROM climate_execution').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM climate_slot_plan WHERE executed=1').fetchone()[0] == 0
    finally:
        conn.close()


def test_execute_unknown_slot_is_rejected(tmp_path):
    db = fixture_db(tmp_path)
    plan(db, 'CLIMATE-1', 'min_energy')
    with pytest.raises(ValueError, match='slot_missing'):
        execute(db, 'CLIMATE-1', 'min_energy', slots=[99])


def test_report_records_blocked_plan_without_commands(tmp_path):
    case = json.loads(json.dumps(BASE))
    case['zones'][0]['heat_c_per_slot'] = [3]
    db = build_case(tmp_path, case)
    payload = plan(db, 'C', 'min_energy')
    assert payload['status'] == STATUS_REVIEW
    path = report(db, 'C', tmp_path / 'blocked.json')
    data = json.loads(path.read_text(encoding='utf-8'))
    stored = data['plans'][0]
    assert stored['status'] == STATUS_REVIEW
    assert stored['commands'] == []
    assert any(issue['code'] == 'heat_missing' for issue in stored['issues'])


def test_unknown_contract_is_rejected(tmp_path):
    db = fixture_db(tmp_path)
    with pytest.raises(ValueError, match='unknown_contract'):
        plan(db, 'CLIMATE-1', 'min_cost_only')


def test_timezone_offset_misalignment_blocks_plan(tmp_path):
    sensors = (
        'zone,at,temp_c,sensor\n'
        'A,2026-01-01T08:00:00+00:00,20.0,TA\n'
        'B,2026-01-01T08:00:00+00:00,20.0,TB\n'
        'A,2026-01-01T09:00:00+05:00,21.0,TA\n'
    )
    payload = plan(build_case(tmp_path, sensors=sensors), 'C', 'min_energy')
    _assert_blocked(payload, 'timezone_misaligned')


def test_slot_start_must_be_offset_aware(tmp_path):
    case = json.loads(json.dumps(BASE))
    case['slots'][0]['start'] = '2026-01-01T08:00:00'
    payload = plan(build_case(tmp_path, case), 'C', 'min_energy')
    _assert_blocked(payload, 'timezone_error')


def test_slot_starts_must_increase(tmp_path):
    case = json.loads(json.dumps(BASE))
    case['slots'][1]['start'] = case['slots'][0]['start']
    payload = plan(build_case(tmp_path, case), 'C', 'min_energy')
    _assert_blocked(payload, 'slot_invalid')
