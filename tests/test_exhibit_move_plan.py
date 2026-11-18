import json
import sys
from pathlib import Path
import pytest
from gateflow.__main__ import initialize, main
from gateflow.exhibit_move import load_catalog, register, plan, verify
from gateflow.ingest import ingest
from gateflow.rules import evaluate
from gateflow.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'
SHIFT = ('2026-01-01T08:00:00+00:00', '2026-01-01T08:45:00+00:00')
REPO_ARCS = [
    {'from': 'N0', 'to': 'N1', 'minutes': 1}, {'from': 'N1', 'to': 'N0', 'minutes': 1},
    {'from': 'N1', 'to': 'N2', 'minutes': 1}, {'from': 'N2', 'to': 'N1', 'minutes': 1},
    {'from': 'N2', 'to': 'N11', 'minutes': 9}, {'from': 'N11', 'to': 'N2', 'minutes': 9},
    {'from': 'N11', 'to': 'N12', 'minutes': 1}, {'from': 'N12', 'to': 'N11', 'minutes': 1}]


def ex(eid, cur, tgt, zone, units=1):
    return {'id': eid, 'name': eid, 'current_node': cur, 'target_node': tgt,
            'target_zone': zone, 'crate_units': units}


def crew(cid, cap=1, clearance=('A', 'B')):
    return {'id': cid, 'cart_capacity': cap, 'clearance': list(clearance)}


def mv(e, dl):
    return {'exhibit': e, 'deadline_minutes': dl}


def case_doc(crews, moves, arcs, shift=SHIFT, depot='N0'):
    return {'depot': depot, 'shift_start': shift[0], 'shift_end': shift[1],
            'crews': crews, 'moves': moves, 'arcs': arcs,
            'contracts': ['min_walk', 'min_max_lateness']}


def arc(frm, to, minutes, closed=None):
    a = {'from': frm, 'to': to, 'minutes': minutes}
    if closed:
        a['closed'] = closed
    return a


def _write(tmp_path, name, doc):
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding='utf-8')
    return p


def _config(tmp_path, zones=('A', 'B')):
    return _write(tmp_path, 'config.json', {
        'batch': [{'id': 'day-1', 'start': '2026-01-01T00:00:00+00:00', 'end': '2026-01-02T00:00:00+00:00'}],
        'zone': [{'id': z, 'capacity': 9} for z in zones],
        'gate_map': [], 'ticket': []})


def setup_case(tmp_path, exhibits, doc, zones=('A', 'B')):
    db = tmp_path / 'case.db'
    initialize(db, _config(tmp_path, zones))
    load_catalog(db, _write(tmp_path, 'catalog.json', {'exhibits': exhibits}))
    register(db, 'M-1', _write(tmp_path, 'case.json', doc))
    return db


def run_plan(db, tmp_path):
    out = tmp_path / 'plan.json'
    summary = plan(db, 'M-1', out)
    return summary, json.loads(out.read_text(encoding='utf-8'))


def actions(stops):
    return [(s['action'], s.get('exhibit'), s.get('at_minute')) for s in stops if s['action'] != 'travel']


def test_fixed_example_both_contracts(tmp_path):
    db = tmp_path / 'm.db'
    initialize(db, FIX / 'exhibit-config.json')
    load_catalog(db, FIX / 'exhibit-catalog.json')
    register(db, 'M-1', FIX / 'exhibit-move-case.json')
    summary, rep = run_plan(db, tmp_path)
    assert summary['contracts']['min_walk']['objective'] == {'total_walk_minutes': 24, 'max_lateness_minutes': 4}
    assert summary['contracts']['min_max_lateness']['objective'] == {'total_walk_minutes': 44, 'max_lateness_minutes': 2}
    mw = rep['contracts']['min_walk']['routes'][0]
    assert actions(mw['stops']) == [('pickup', 'X-A', 1), ('deliver', 'X-A', 12),
                                    ('pickup', 'X-B', 13), ('deliver', 'X-B', 22)]
    nodes = [mw['stops'][0]['from']] + [s['to'] for s in mw['stops'] if s['action'] == 'travel']
    assert nodes == ['N0', 'N1', 'N12', 'N11', 'N2', 'N0']
    ml = rep['contracts']['min_max_lateness']['routes'][0]
    assert actions(ml['stops']) == [('pickup', 'X-B', 11), ('deliver', 'X-B', 20),
                                    ('pickup', 'X-A', 21), ('deliver', 'X-A', 32)]
    # 车载容量全程不超过 1：先取 A 途经 N11 时不能装 B
    for route in (mw, ml):
        for s in route['stops']:
            assert s['cart_crate_units'] <= 1
    leg = [s for s in mw['stops'] if s['action'] == 'travel' and s['from'] == 'N1'][0]
    assert 'N11' in leg['path'] and leg['cart_exhibits'] == ['X-A']
    # 两种合同运送相同展品
    assert rep['contracts']['min_walk']['exhibits_delivered'] == ['X-A', 'X-B']
    assert rep['contracts']['min_max_lateness']['exhibits_delivered'] == ['X-A', 'X-B']
    # 证据要素齐全
    assert rep['plan_only'] and rep['case_digest'] and rep['evidence_boundary']
    assert rep['search']['pareto_frontier'] == [
        {'total_walk_minutes': 24, 'max_lateness_minutes': 4},
        {'total_walk_minutes': 44, 'max_lateness_minutes': 2}]
    deliver = [s for s in mw['stops'] if s['action'] == 'deliver'][0]
    for key in ('node', 'at_minute', 'exhibit', 'target_zone', 'deadline_minute', 'lateness_minutes',
                    'cart_exhibits', 'cart_crate_units'):
        assert key in deliver
    travel = [s for s in mw['stops'] if s['action'] == 'travel'][0]
    for key in ('from', 'to', 'path', 'arcs', 'depart_minute', 'arrive_minute', 'walk_minutes'):
        assert key in travel
    assert (tmp_path / 'plan.md').exists()
    # 幂等重算：摘要一致、每合同一行
    assert plan(db, 'M-1') == summary
    c = connect(db)
    try:
        assert c.execute('SELECT COUNT(*) FROM exhibit_plan').fetchone()[0] == 2
    finally:
        c.close()
    assert verify(db, 'M-1')['ok']


def test_arc_closure_waits_for_window(tmp_path):
    exhibits = [ex('X-A', 'N1', 'N2', 'A')]
    arcs = [arc('N0', 'N1', 2, closed=[{'start': 0, 'end': 10}]),
            arc('N1', 'N2', 3), arc('N2', 'N0', 4), arc('N1', 'N0', 2)]
    doc = case_doc([crew('C1')], [mv('X-A', 100)], arcs,
                   shift=('2026-01-01T08:00:00+00:00', '2026-01-01T09:00:00+00:00'))
    db = setup_case(tmp_path, exhibits, doc)
    summary, rep = run_plan(db, tmp_path)
    assert summary['contracts']['min_walk']['objective'] == {'total_walk_minutes': 9, 'max_lateness_minutes': 0}
    stops = rep['contracts']['min_walk']['routes'][0]['stops']
    first = stops[0]
    assert first['from'] == 'N0' and first['to'] == 'N1'
    assert first['arcs'][0]['depart_minute'] == 10 and first['arcs'][0]['arrive_minute'] == 12
    assert first['wait_minutes'] == 10
    assert actions(stops) == [('pickup', 'X-A', 12), ('deliver', 'X-A', 15)]
    assert verify(db, 'M-1')['ok']


def test_arc_closure_forces_detour(tmp_path):
    exhibits = [ex('X-A', 'N1', 'N2', 'A')]
    arcs = [arc('N0', 'N1', 1), arc('N1', 'N2', 5, closed=[{'start': 0, 'end': 45}]),
            arc('N1', 'N3', 2), arc('N3', 'N2', 2), arc('N2', 'N0', 1)]
    doc = case_doc([crew('C1')], [mv('X-A', 100)], arcs)
    db = setup_case(tmp_path, exhibits, doc)
    summary, rep = run_plan(db, tmp_path)
    assert summary['contracts']['min_walk']['objective'] == {'total_walk_minutes': 6, 'max_lateness_minutes': 0}
    stops = rep['contracts']['min_walk']['routes'][0]['stops']
    detour = [s for s in stops if s['action'] == 'travel' and s['from'] == 'N1'][0]
    assert detour['path'] == ['N1', 'N3', 'N2']
    assert actions(stops) == [('pickup', 'X-A', 1), ('deliver', 'X-A', 5)]


def test_one_way_arcs_not_reversed(tmp_path):
    exhibits = [ex('X-A', 'N1', 'N0', 'A')]
    arcs = [arc('N0', 'N1', 1), arc('N1', 'N2', 1), arc('N2', 'N0', 1)]
    doc = case_doc([crew('C1')], [mv('X-A', 100)], arcs,
                   shift=('2026-01-01T08:00:00+00:00', '2026-01-01T08:30:00+00:00'))
    db = setup_case(tmp_path, exhibits, doc)
    summary, rep = run_plan(db, tmp_path)
    # 若把有向边当无向边会得出 2 分钟；正确答案是绕行 3 分钟
    assert summary['contracts']['min_walk']['objective'] == {'total_walk_minutes': 3, 'max_lateness_minutes': 0}
    stops = rep['contracts']['min_walk']['routes'][0]['stops']
    directed = {(a['from'], a['to']) for a in arcs}
    for s in stops:
        if s['action'] == 'travel':
            for leg in s['arcs']:
                assert (leg['from'], leg['to']) in directed
    assert actions(stops) == [('pickup', 'X-A', 1), ('deliver', 'X-A', 3)]


def test_two_crews_clearance_split(tmp_path):
    exhibits = [ex('X-A', 'N1', 'N12', 'A'), ex('X-B', 'N11', 'N2', 'B')]
    doc = case_doc([crew('C1', clearance=('A',)), crew('C2', clearance=('B',))],
                   [mv('X-A', 35), mv('X-B', 18)], REPO_ARCS)
    db = setup_case(tmp_path, exhibits, doc)
    summary, rep = run_plan(db, tmp_path)
    for c in ('min_walk', 'min_max_lateness'):
        assert summary['contracts'][c]['objective'] == {'total_walk_minutes': 46, 'max_lateness_minutes': 2}
        routes = rep['contracts'][c]['routes']
        assert routes[0]['crew'] == 'C1' and routes[0]['delivered'] == ['X-A']
        assert routes[1]['crew'] == 'C2' and routes[1]['delivered'] == ['X-B']
        assert actions(routes[0]['stops']) == [('pickup', 'X-A', 1), ('deliver', 'X-A', 12)]
        assert actions(routes[1]['stops']) == [('pickup', 'X-B', 11), ('deliver', 'X-B', 20)]
    assert rep['search']['assignments_enumerated'] == 4
    assert rep['search']['assignments_feasible'] == 1
    assert verify(db, 'M-1')['ok']


def test_second_crew_optional(tmp_path):
    exhibits = [ex('X-A', 'N1', 'N2', 'A'), ex('X-B', 'N2', 'N1', 'A')]
    arcs = [arc('N0', 'N1', 1), arc('N1', 'N0', 1), arc('N1', 'N2', 1),
            arc('N2', 'N1', 1), arc('N0', 'N2', 2), arc('N2', 'N0', 2)]
    doc = case_doc([crew('C1'), crew('C2')], [mv('X-A', 100), mv('X-B', 100)], arcs,
                   shift=('2026-01-01T08:00:00+00:00', '2026-01-01T09:00:00+00:00'))
    db = setup_case(tmp_path, exhibits, doc)
    summary, rep = run_plan(db, tmp_path)
    # 一组人员顺手往返即可，min_walk 不应为了用满两组人员而多走路
    assert summary['contracts']['min_walk']['objective'] == {'total_walk_minutes': 4, 'max_lateness_minutes': 0}
    routes = rep['contracts']['min_walk']['routes']
    assert sum(1 for r in routes if not r['stops']) == 1
    assert sorted(d for r in routes for d in r['delivered']) == ['X-A', 'X-B']


def test_capacity_forces_multiple_trips(tmp_path):
    exhibits = [ex('E1', 'N1', 'N2', 'A'), ex('E2', 'N1', 'N2', 'A')]
    arcs = [arc('N0', 'N1', 1), arc('N1', 'N0', 1), arc('N1', 'N2', 2),
            arc('N2', 'N1', 2), arc('N0', 'N2', 3), arc('N2', 'N0', 3)]
    shift = ('2026-01-01T08:00:00+00:00', '2026-01-01T10:00:00+00:00')
    db = setup_case(tmp_path, exhibits, case_doc([crew('C1', cap=1)], [mv('E1', 100), mv('E2', 100)], arcs, shift=shift))
    summary, rep = run_plan(db, tmp_path)
    assert summary['contracts']['min_walk']['objective'] == {'total_walk_minutes': 10, 'max_lateness_minutes': 0}
    stops = rep['contracts']['min_walk']['routes'][0]['stops']
    assert all(s['cart_crate_units'] <= 1 for s in stops)
    assert sorted(x[1] for x in actions(stops) if x[0] == 'deliver') == ['E1', 'E2']
    db2_dir = tmp_path / 'cap2'
    db2_dir.mkdir()
    db2 = setup_case(db2_dir, exhibits,
                     case_doc([crew('C1', cap=2)], [mv('E1', 100), mv('E2', 100)], arcs, shift=shift))
    summary2, _ = run_plan(db2, db2_dir)
    assert summary2['contracts']['min_walk']['objective'] == {'total_walk_minutes': 6, 'max_lateness_minutes': 0}


def test_crate_units_exceed_cart_is_infeasible(tmp_path):
    exhibits = [ex('X-A', 'N1', 'N2', 'A', units=2)]
    arcs = [arc('N0', 'N1', 1), arc('N1', 'N2', 1), arc('N2', 'N0', 1)]
    db = setup_case(tmp_path, exhibits, case_doc([crew('C1', cap=1)], [mv('X-A', 100)], arcs))
    summary, rep = run_plan(db, tmp_path)
    assert summary['contracts']['min_walk']['status'] == 'infeasible'
    assert summary['contracts']['min_walk']['objective'] is None
    reasons = rep['infeasible']['reasons']
    assert [r['code'] for r in reasons] == ['capacity_exceeded']
    assert rep['infeasible']['exhibits_undelivered'] == ['X-A']


def test_clearance_missing_is_infeasible(tmp_path):
    exhibits = [ex('X-B', 'N1', 'N2', 'B')]
    arcs = [arc('N0', 'N1', 1), arc('N1', 'N2', 1), arc('N2', 'N0', 1)]
    db = setup_case(tmp_path, exhibits, case_doc([crew('C1', clearance=('A',))], [mv('X-B', 100)], arcs))
    summary, rep = run_plan(db, tmp_path)
    assert summary['contracts']['min_max_lateness']['status'] == 'infeasible'
    assert [r['code'] for r in rep['infeasible']['reasons']] == ['clearance_missing']
    assert rep['infeasible']['exhibits_undelivered'] == ['X-B']


def test_no_feasible_shift_reports_all_undelivered(tmp_path):
    exhibits = [ex('X-A', 'N1', 'N12', 'A'), ex('X-B', 'N11', 'N2', 'B')]
    doc = case_doc([crew('C1')], [mv('X-A', 35), mv('X-B', 18)], REPO_ARCS,
                   shift=('2026-01-01T08:00:00+00:00', '2026-01-01T08:05:00+00:00'))
    db = setup_case(tmp_path, exhibits, doc)
    summary, rep = run_plan(db, tmp_path)
    for c in ('min_walk', 'min_max_lateness'):
        assert summary['contracts'][c]['status'] == 'infeasible'
    assert [r['code'] for r in rep['infeasible']['reasons']] == ['shift_exceeded']
    assert rep['infeasible']['exhibits_undelivered'] == ['X-A', 'X-B']
    assert verify(db, 'M-1')['ok']


def test_inventory_location_drives_route(tmp_path):
    exhibits = [ex('X-A', 'N5', 'N12', 'A')]
    arcs = [arc('N0', 'N5', 2), arc('N5', 'N0', 2), arc('N5', 'N12', 4), arc('N12', 'N0', 6)]
    doc = case_doc([crew('C1')], [mv('X-A', 100)], arcs,
                   shift=('2026-01-01T08:00:00+00:00', '2026-01-01T09:00:00+00:00'))
    db = setup_case(tmp_path, exhibits, doc)
    summary, rep = run_plan(db, tmp_path)
    assert summary['contracts']['min_walk']['objective'] == {'total_walk_minutes': 12, 'max_lateness_minutes': 0}
    stops = rep['contracts']['min_walk']['routes'][0]['stops']
    assert actions(stops) == [('pickup', 'X-A', 2), ('deliver', 'X-A', 6)]
    assert stops[1]['node'] == 'N5'


def test_inventory_change_after_freeze_rejected(tmp_path):
    db = tmp_path / 'm.db'
    initialize(db, FIX / 'exhibit-config.json')
    load_catalog(db, FIX / 'exhibit-catalog.json')
    register(db, 'M-1', FIX / 'exhibit-move-case.json')
    c = connect(db)
    c.execute("UPDATE exhibit SET current_node='N9' WHERE id='X-A'")
    c.commit(); c.close()
    with pytest.raises(ValueError, match='inventory_changed'):
        plan(db, 'M-1')
    with pytest.raises(ValueError, match='inventory_changed'):
        verify(db, 'M-1')


def test_duplicate_move_rejected(tmp_path):
    exhibits = [ex('X-A', 'N1', 'N2', 'A')]
    arcs = [arc('N0', 'N1', 1), arc('N1', 'N2', 1), arc('N2', 'N0', 1)]
    doc = case_doc([crew('C1')], [mv('X-A', 35), mv('X-A', 20)], arcs)
    db = tmp_path / 'd.db'
    initialize(db, _config(tmp_path))
    load_catalog(db, _write(tmp_path, 'catalog.json', {'exhibits': exhibits}))
    with pytest.raises(ValueError, match='duplicate_exhibit'):
        register(db, 'M-1', _write(tmp_path, 'case.json', doc))


def test_case_too_large_rejected(tmp_path):
    exhibits = [ex(f'E{i}', 'N1', 'N2', 'A') for i in range(9)]
    arcs = [arc('N0', 'N1', 1), arc('N1', 'N2', 1), arc('N2', 'N0', 1)]
    doc = case_doc([crew('C1', cap=9)], [mv(f'E{i}', 100) for i in range(9)], arcs)
    db = tmp_path / 'd.db'
    initialize(db, _config(tmp_path))
    load_catalog(db, _write(tmp_path, 'catalog.json', {'exhibits': exhibits}))
    with pytest.raises(ValueError, match='case_too_large'):
        register(db, 'M-1', _write(tmp_path, 'case.json', doc))


def test_plan_requires_registered_case(tmp_path):
    db = tmp_path / 'd.db'
    initialize(db, _config(tmp_path))
    with pytest.raises(ValueError, match='move_case_missing'):
        plan(db, 'M-1')
    with pytest.raises(ValueError, match='move_case_missing'):
        verify(db, 'M-1')


def test_plan_conflict_on_tampered_case(tmp_path):
    db = tmp_path / 'm.db'
    initialize(db, FIX / 'exhibit-config.json')
    load_catalog(db, FIX / 'exhibit-catalog.json')
    register(db, 'M-1', FIX / 'exhibit-move-case.json')
    plan(db, 'M-1')
    c = connect(db)
    payload = json.loads(c.execute('SELECT payload FROM exhibit_move_case WHERE id=?', ('M-1',)).fetchone()[0])
    payload['case']['moves'][0]['deadline_minutes'] = 5
    c.execute('UPDATE exhibit_move_case SET payload=? WHERE id=?', (json.dumps(payload), 'M-1'))
    c.commit(); c.close()
    with pytest.raises(ValueError, match='plan_conflict'):
        plan(db, 'M-1')


def test_verify_detects_tampered_plan(tmp_path):
    db = tmp_path / 'm.db'
    initialize(db, FIX / 'exhibit-config.json')
    load_catalog(db, FIX / 'exhibit-catalog.json')
    register(db, 'M-1', FIX / 'exhibit-move-case.json')
    plan(db, 'M-1')
    assert verify(db, 'M-1')['ok']
    c = connect(db)
    c.execute("UPDATE exhibit_plan SET digest='bogus' WHERE contract='min_walk'")
    c.commit(); c.close()
    got = verify(db, 'M-1')
    assert not got['ok'] and not got['contracts']['min_walk']['digest_match']


def test_plan_preserves_gate_pipeline(tmp_path):
    db = tmp_path / 'g.db'
    initialize(db, FIX / 'config.json')
    ingest(FIX / 'events.jsonl', db)
    evaluate(db)
    c = connect(db)
    before = {t: c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
              for t in ('event', 'occupancy', 'decision', 'issue', 'evidence')}
    decisions = [dict(r) for r in c.execute('SELECT * FROM decision ORDER BY zone')]
    c.close()
    load_catalog(db, FIX / 'exhibit-catalog.json')
    register(db, 'M-1', FIX / 'exhibit-move-case.json')
    _, rep = run_plan(db, tmp_path)
    c = connect(db)
    after = {t: c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
             for t in ('event', 'occupancy', 'decision', 'issue', 'evidence')}
    assert before == after
    assert decisions == [dict(r) for r in c.execute('SELECT * FROM decision ORDER BY zone')]
    # 计划不伪称实物交接：库存位置不被改写
    assert c.execute("SELECT current_node FROM exhibit WHERE id='X-A'").fetchone()[0] == 'N1'
    c.close()
    assert rep['plan_only'] is True


def test_eight_exhibits_two_crews(tmp_path):
    exhibits = ([ex(f'A{i}', 'N1', 'N2' if i % 2 else 'N3', 'A') for i in range(4)] +
                [ex(f'B{i}', 'N1', 'N2' if i % 2 else 'N3', 'B') for i in range(4)])
    arcs = [arc('N0', 'N1', 1), arc('N1', 'N0', 1), arc('N1', 'N2', 2), arc('N2', 'N1', 2),
            arc('N1', 'N3', 2), arc('N3', 'N1', 1), arc('N2', 'N0', 3), arc('N3', 'N0', 3)]
    doc = case_doc([crew('C1', cap=2, clearance=('A',)), crew('C2', cap=2, clearance=('B',))],
                   [mv(e['id'], 200) for e in exhibits], arcs,
                   shift=('2026-01-01T08:00:00+00:00', '2026-01-01T14:00:00+00:00'))
    db = setup_case(tmp_path, exhibits, doc)
    summary, rep = run_plan(db, tmp_path)
    assert summary['contracts']['min_walk']['objective'] == {'total_walk_minutes': 18, 'max_lateness_minutes': 0}
    assert rep['search']['assignments_enumerated'] == 256
    assert rep['search']['assignments_feasible'] == 1
    routes = rep['contracts']['min_walk']['routes']
    assert sorted(d for r in routes for d in r['delivered']) == sorted(e['id'] for e in exhibits)
    assert sum(r['total_walk_minutes'] for r in routes) == 18
    assert verify(db, 'M-1')['ok']


def test_cli_exhibit_flow(tmp_path, monkeypatch, capsys):
    db = tmp_path / 'c.db'

    def run(*args):
        monkeypatch.setattr(sys, 'argv', ['gateflow', *args])
        main()
        return capsys.readouterr().out

    run('init', '--db', str(db), '--config', str(FIX / 'exhibit-config.json'))
    assert json.loads(run('exhibit-catalog', '--db', str(db), '--fixture', str(FIX / 'exhibit-catalog.json'))) == {'exhibits': 2}
    assert json.loads(run('exhibit-register', '--db', str(db), '--case', 'M-1',
                          '--fixture', str(FIX / 'exhibit-move-case.json')))['moves'] == 2
    got = json.loads(run('exhibit-plan', '--db', str(db), '--case', 'M-1', '--out', str(tmp_path / 'r.json')))
    assert got['contracts']['min_walk']['objective'] == {'total_walk_minutes': 24, 'max_lateness_minutes': 4}
    assert got['contracts']['min_max_lateness']['objective'] == {'total_walk_minutes': 44, 'max_lateness_minutes': 2}
    assert json.loads((tmp_path / 'r.json').read_text(encoding='utf-8'))['kind'] == 'exhibit_move_plan'
    assert json.loads(run('exhibit-verify', '--db', str(db), '--case', 'M-1'))['ok']
