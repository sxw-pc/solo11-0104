import json
from pathlib import Path
from gateflow.__main__ import initialize
from gateflow.ingest import ingest
from gateflow.rules import evaluate
from gateflow.store import connect
from gateflow.report import report

FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def setup_overcap(tmp):
    db = tmp / 'overcap.db'
    initialize(db, FIX / 'overcap_config.json')
    ingest(FIX / 'overcap_events.jsonl', db)
    return db


def setup_base(tmp):
    db = tmp / 'x.db'
    initialize(db, FIX / 'config.json')
    return db


def add(tmp, db, rows, name='e.jsonl'):
    p = tmp / name
    p.write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf-8')
    ingest(p, db)


def ev(**changes):
    r = {'kind': 'enter', 'device': 'D', 'seq': 1, 'at': '2026-01-01T08:00:00+00:00', 'gate': 'G1', 'ticket': 'T1'}
    r.update(changes)
    return r


def episodes(db, zone='A'):
    c = connect(db)
    rows = [dict(r) for r in c.execute('SELECT * FROM over_capacity WHERE zone=? ORDER BY id', (zone))]
    c.close()
    return rows


def test_immediate_contract_records_transient_overcapacity(tmp_path):
    db = setup_overcap(tmp_path)
    assert evaluate(db, 'immediate')['A'] == '超限'
    eps = episodes(db)
    assert len(eps) == 2
    short, long = eps
    # 第一段：第三张票进入后 40 秒内退出，即时契约仍记为超限
    assert short['peak'] == 3 and short['duration_seconds'] == 40
    assert json.loads(short['tickets']) == ['T1', 'T2', 'T3']
    assert short['classification'] == '超限' and short['confidence'] == '可信'
    assert short['ended_at'] == '2026-01-01T08:02:40+00:00'
    # 第二段：跨区 move 引发的连续 6 分钟超容
    assert long['peak'] == 3 and long['duration_seconds'] == 360
    assert json.loads(long['tickets']) == ['T1', 'T2', 'T4']
    assert long['classification'] == '超限'
    # B 区从未超容
    assert episodes(db, 'B') == []


def test_sustained_contract_only_escalates_long_episode(tmp_path):
    db = setup_overcap(tmp_path)
    # 配置文件默认 sustained / 300 秒
    assert evaluate(db)['A'] == '需疏散'
    short, long = episodes(db)
    assert short['classification'] == '待观察'          # 40 秒：记录但不升级
    assert long['classification'] == '需疏散'          # 360 秒：升级为需疏散
    assert short['duration_seconds'] == 40 and long['duration_seconds'] == 360
    detail = json.loads(connect(db).execute('SELECT detail FROM decision WHERE zone="A"').fetchone()[0])
    assert detail['contract'] == 'sustained'
    assert [e['classification'] for e in detail['episodes']] == ['待观察', '需疏散']


def test_episode_evidence_is_traceable_to_events(tmp_path):
    db = setup_overcap(tmp_path)
    evaluate(db, 'immediate')
    c = connect(db)
    short, long = c.execute('SELECT * FROM over_capacity ORDER BY id').fetchall()
    trigger = c.execute('SELECT kind,ticket FROM event WHERE id=?', (short['trigger_event_id'],)).fetchone()
    clear = c.execute('SELECT kind,ticket FROM event WHERE id=?', (short['clear_event_id'],)).fetchone()
    assert tuple(trigger) == ('enter', 'T3')
    assert tuple(clear) == ('exit', 'T3')
    move_trigger = c.execute('SELECT kind,ticket FROM event WHERE id=?', (long['trigger_event_id'],)).fetchone()
    assert tuple(move_trigger) == ('move', 'T4')
    # 同一条完整心跳 fixture：证据均可信，且每个触发/解除事件都有 evidence 锚点
    assert c.execute('SELECT count(*) FROM evidence').fetchone()[0] == 10
    c.close()


def test_replay_is_deterministic_across_runs(tmp_path):
    db = setup_overcap(tmp_path)
    evaluate(db, 'sustained')
    first = [(r['started_at'], r['ended_at'], r['peak'], r['duration_seconds'], r['classification']) for r in episodes(db)]
    evaluate(db, 'sustained')
    second = [(r['started_at'], r['ended_at'], r['peak'], r['duration_seconds'], r['classification']) for r in episodes(db)]
    assert first == second and len(first) == 2


def test_cancel_recomputes_and_removes_episode(tmp_path):
    db = setup_base(tmp_path)
    c = connect(db)
    c.execute("INSERT OR IGNORE INTO ticket VALUES ('T3','child')")
    c.commit()
    c.close()
    add(tmp_path, db, [
        ev(seq=1, ticket='T1'),
        ev(seq=2, at='2026-01-01T08:01:00+00:00', ticket='T2'),
        ev(seq=3, at='2026-01-01T08:02:00+00:00', ticket='T3'),
        ev(seq=4, kind='cancel', at='2026-01-01T08:03:00+00:00', ref_device='D', ref_seq=3),
        ev(seq=5, kind='heartbeat', gate=None, at='2026-01-01T08:05:00+00:00'),
    ])
    evaluate(db)
    assert episodes(db) == []
    assert connect(db).execute('SELECT max(count) FROM occupancy WHERE zone="A"').fetchone()[0] == 2
    # 重复撤销不产生时长，只登记 cancel_invalid
    add(tmp_path, db, [ev(seq=6, kind='cancel', at='2026-01-01T09:00:00+00:00', ref_device='D', ref_seq=3)], name='again.jsonl')
    evaluate(db)
    c = connect(db)
    assert c.execute("SELECT count(*) FROM issue WHERE code='cancel_invalid'").fetchone()[0] == 1
    assert c.execute('SELECT count(*) FROM over_capacity').fetchone()[0] == 0
    c.close()


def test_cross_zone_move_keeps_single_continuous_episode(tmp_path):
    db = setup_overcap(tmp_path)
    evaluate(db, 'immediate')
    eps = episodes(db)
    assert len(eps) == 2
    long = eps[1]
    c = connect(db)
    trigger = c.execute('SELECT kind FROM event WHERE id=?', (long['trigger_event_id'],)).fetchone()[0]
    assert trigger == 'move'
    # move 只让 B 区减为 0，B 区从未超容
    assert c.execute('SELECT max(count) FROM occupancy WHERE zone="B"').fetchone()[0] == 1
    assert c.execute('SELECT count(*) FROM over_capacity WHERE zone="B"').fetchone()[0] == 0
    c.close()


def test_map_version_switch_resolves_gate_by_event_time(tmp_path):
    db = setup_base(tmp_path)
    c = connect(db)
    c.execute('UPDATE zone SET capacity=1 WHERE id="A"')
    c.execute("DELETE FROM gate_map WHERE gate='G1'")
    c.execute("INSERT INTO gate_map VALUES ('G1','A','2025-01-01T00:00:00+00:00','2026-01-01T09:00:00+00:00')")
    c.execute("INSERT INTO gate_map VALUES ('G1','B','2026-01-01T09:00:00+00:00','2027-01-01T00:00:00+00:00')")
    c.commit()
    c.close()
    add(tmp_path, db, [
        ev(seq=1, at='2026-01-01T08:00:00+00:00', ticket='T1'),
        ev(seq=2, at='2026-01-01T08:01:00+00:00', ticket='T2'),
        ev(seq=3, kind='move', at='2026-01-01T09:05:00+00:00', ticket='T2'),
        ev(seq=4, kind='heartbeat', gate=None, at='2026-01-01T08:05:00+00:00'),
        ev(seq=5, kind='heartbeat', gate=None, at='2026-01-01T09:10:00+00:00'),
    ])
    evaluate(db, 'immediate')
    eps = episodes(db)
    assert len(eps) == 1 and eps[0]['zone'] == 'A'
    # 换版后 09:05 的 G1 事件关联 B 区，不在 A 区产生新超容
    assert connect(db).execute('SELECT count FROM occupancy WHERE zone="B" ORDER BY id DESC LIMIT 1').fetchone()[0] == 1
    assert episodes(db, 'B') == []


def test_sequence_gap_must_not_report_safety(tmp_path):
    db = setup_base(tmp_path)
    c = connect(db)
    c.execute("INSERT OR IGNORE INTO ticket VALUES ('T3','child')")
    c.commit()
    c.close()
    add(tmp_path, db, [
        ev(seq=1, ticket='T1'),
        ev(seq=2, at='2026-01-01T08:01:00+00:00', ticket='T2'),
        ev(seq=4, at='2026-01-01T08:02:00+00:00', ticket='T3'),
        ev(seq=5, kind='heartbeat', gate=None, at='2026-01-01T08:05:00+00:00'),
    ])
    for contract in ('immediate', 'sustained'):
        evaluate(db, contract)
        c = connect(db)
        assert c.execute("SELECT count(*) FROM issue WHERE code='sequence_gap'").fetchone()[0] >= 1
        row = c.execute('SELECT confidence,classification FROM over_capacity').fetchone()
        assert row['confidence'] == '未知' and row['classification'] == '证据不足'
        assert c.execute('SELECT status FROM decision WHERE zone="A"').fetchone()[0] == '证据不足'
        c.close()


def test_heartbeat_gap_must_not_report_safety(tmp_path):
    db = setup_base(tmp_path)
    c = connect(db)
    c.execute("INSERT OR IGNORE INTO ticket VALUES ('T3','child')")
    c.commit()
    c.close()
    add(tmp_path, db, [
        ev(seq=1, ticket='T1'),
        ev(seq=2, at='2026-01-01T08:01:00+00:00', ticket='T2'),
        ev(seq=3, at='2026-01-01T08:02:00+00:00', ticket='T3'),
    ])
    evaluate(db, 'sustained')
    c = connect(db)
    assert c.execute("SELECT count(*) FROM issue WHERE code='heartbeat_gap'").fetchone()[0] == 1
    row = c.execute('SELECT classification FROM over_capacity').fetchone()
    assert row[0] == '证据不足'
    assert c.execute('SELECT status FROM decision WHERE zone="A"').fetchone()[0] == '证据不足'
    c.close()


def test_same_second_events_have_zero_phantom_duration(tmp_path):
    db = setup_base(tmp_path)
    c = connect(db)
    c.execute("INSERT OR IGNORE INTO ticket VALUES ('T3','child')")
    c.commit()
    c.close()
    t0 = '2026-01-01T08:00:00+00:00'
    add(tmp_path, db, [
        ev(seq=1, at=t0, ticket='T1'),
        ev(seq=2, at=t0, ticket='T2'),
        ev(seq=3, at=t0, ticket='T3'),
        ev(seq=4, kind='exit', at=t0, ticket='T3'),
        ev(seq=5, kind='heartbeat', gate=None, at='2026-01-01T08:01:00+00:00'),
    ])
    evaluate(db, 'sustained')
    eps = episodes(db)
    assert len(eps) == 1
    assert eps[0]['duration_seconds'] == 0 and eps[0]['classification'] == '待观察'
    evaluate(db, 'immediate')
    assert episodes(db)[0]['classification'] == '超限'


def test_sustain_threshold_boundary(tmp_path):
    db = setup_overcap(tmp_path)
    evaluate(db, 'sustained', 360)
    assert [e['classification'] for e in episodes(db)] == ['待观察', '需疏散']
    evaluate(db, 'sustained', 361)
    assert [e['classification'] for e in episodes(db)] == ['待观察', '待观察']
    assert connect(db).execute('SELECT status FROM decision WHERE zone="A"').fetchone()[0] == '待观察'


def test_report_lists_episode_peaks_durations_and_tickets(tmp_path):
    db = setup_overcap(tmp_path)
    evaluate(db)
    out = report(db, tmp_path / 'r.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    assert len(data['over_capacity']) == 2
    peaks = {(r['peak'], r['duration_seconds']) for r in data['over_capacity']}
    assert peaks == {(3, 40), (3, 360)}
    md = out.with_suffix('.md').read_text(encoding='utf-8')
    assert '超容区间' in md and 'T4' in md and '需疏散' in md


def test_invalid_contract_rejected(tmp_path):
    import pytest
    db = setup_base(tmp_path)
    with pytest.raises(ValueError):
        evaluate(db, 'pager')


def test_malformed_cancel_and_ticketless_event_rejected_at_ingest(tmp_path):
    db = setup_base(tmp_path)
    add(tmp_path, db, [
        ev(seq=1, ticket='T1'),
        ev(seq=2, kind='cancel', at='2026-01-01T08:01:00+00:00'),
        ev(seq=3, at='2026-01-01T08:02:00+00:00', ticket=None),
    ])
    c = connect(db)
    # 只有合法的 seq=1 入事件落库，两条畸形记录进 parse_error
    assert [r[0] for r in c.execute('SELECT seq FROM event ORDER BY seq')] == [1]
    assert c.execute("SELECT count(*) FROM issue WHERE code='parse_error'").fetchone()[0] == 2
    c.close()
    evaluate(db, 'immediate')
    c = connect(db)
    assert c.execute('SELECT count(*) FROM over_capacity').fetchone()[0] == 0
    c.close()


def test_sustained_setting_is_picked_up_from_config(tmp_path):
    db = setup_overcap(tmp_path)
    # 不传参数：overcap_config.json 预置 sustained / 300
    res = evaluate(db)
    assert res['A'] == '需疏散'
    short, long = episodes(db)
    assert short['classification'] == '待观察' and long['classification'] == '需疏散'
