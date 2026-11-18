"""布展物资采购订单：精确优化、起订额、容量、逾期、待核阻断与订单修订测试。"""
import csv
import json
import sqlite3
from pathlib import Path

import pytest

from gateflow.__main__ import initialize
from gateflow.store import connect
from gateflow.venue_procurement import register, plan_purchases, readiness_report

FIX = Path(__file__).resolve().parents[1] / 'fixtures'
QUOTE_HEADER = ('vendor', 'sku', 'unit_cents', 'available_qty', 'arrival_day', 'min_order_cents')


def write_case(root, case_id, requirements, quote_rows, planning_day='2026-01-02',
               quotes_name='quotes.csv', case_name='case.json', extra=None):
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    with (root / quotes_name).open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f); w.writerow(QUOTE_HEADER); w.writerows(quote_rows)
    doc = {'zone_requirements': requirements, 'quotes_file': quotes_name,
           'contracts': ['min_total_cost', 'min_max_lateness'], 'planning_day': planning_day}
    if extra: doc.update(extra)
    path = root / case_name
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    return path


def setup_db(tmp_path, zones=(('A', 5), ('B', 5), ('C', 5)), extra_zones=()):
    cfg = tmp_path / 'cfg.json'
    all_zones = list(zones) + list(extra_zones)
    cfg.write_text(json.dumps({'batch': [], 'zone': [{'id': z, 'capacity': c} for z, c in all_zones],
                               'gate_map': [], 'ticket': []}), encoding='utf-8')
    db = tmp_path / 'venue.db'
    initialize(db, cfg)
    return db


def contracts_by_objective(payload):
    return {c['objective']: c for c in payload['contracts']}


def load_report(db, case_id='BUY', out_name='readiness.json'):
    out = db.parent / out_name
    readiness_report(db, case_id, out)
    return json.loads(out.read_text(encoding='utf-8'))


def lines_index(report_contract):
    return {(x['vendor'], x['sku']): x for x in report_contract['lines']}


# ---------- 冻结主案例：两份合法合同的取舍 ----------

def test_fixture_case_two_optimal_contracts(tmp_path):
    db = setup_db(tmp_path)
    register(db, 'BUY', FIX / 'venue-buy-case.json')
    payload = plan_purchases(db, 'BUY')
    assert payload['status'] == '可签发'
    by = contracts_by_objective(payload)

    cheap = by['min_total_cost']
    assert cheap['total_cents'] == 12000 and cheap['max_lateness'] == 2
    fast = by['min_max_lateness']
    assert fast['total_cents'] == 17000 and fast['max_lateness'] == 0

    report = load_report(db)
    rc = {c['objective']: c for c in report['contracts']}

    cheap_lines = lines_index(rc['min_total_cost'])
    assert cheap_lines[('S', 'DISPLAY-CASE')]['qty'] == 2
    assert cheap_lines[('S', 'LABEL-SET')]['qty'] == 1
    s_total = next(v for v in rc['min_total_cost']['vendor_totals'] if v['vendor'] == 'S')
    assert s_total['total_cents'] == 12000 and s_total['min_order_cents'] == 12000
    assert s_total['moq_satisfied'] == 1 and s_total['promised_arrival_day'] == 6
    req_a = next(r for r in rc['min_total_cost']['requirements'] if r['zone'] == 'A')
    assert req_a['effective_arrival_day'] == 6 and req_a['lateness'] == 2
    req_b = next(r for r in rc['min_total_cost']['requirements'] if r['zone'] == 'B')
    assert req_b['lateness'] == 0

    fast_lines = lines_index(rc['min_max_lateness'])
    assert fast_lines[('F', 'DISPLAY-CASE')]['qty'] == 2
    assert fast_lines[('F', 'LABEL-SET')]['qty'] == 1
    assert all(r['lateness'] == 0 for r in rc['min_max_lateness']['requirements'])
    # 报价原文 SHA 与案例摘要哈希冻结进报告
    assert report['quote_sha256'] and len(report['quote_sha256']) == 64
    assert report['case_digest'] and report['status'] == '可签发'
    assert (db.parent / 'readiness.md').exists()


def test_moq_is_cross_line_not_per_sku_line(tmp_path):
    """展柜买 F、标识单买 S：S 仅 2000 分低于 12000 起订额，该混合方案必须被拒绝。"""
    db = setup_db(tmp_path)
    register(db, 'BUY', FIX / 'venue-buy-case.json')
    plan_purchases(db, 'BUY')
    report = load_report(db)
    proof = report['optimality_proof']
    assert proof['rejected']['moq_not_satisfied'] >= 1
    for order in proof['all_feasible_orders']:
        vendors = {x['vendor'] for x in order['lines']}
        if vendors == {'S', 'F'}:  # 任何跨供应商拆分都必须仍满足各自起订额
            totals = {}
            for x in order['lines']:
                totals[x['vendor']] = totals.get(x['vendor'], 0) + x['line_cents']
            assert totals['S'] >= 12000
    # 仅有 S 全包与 F 全包两个可行完整订单
    assert proof['feasible_count'] == 2
    assert proof['pareto_frontier'] == [
        {'total_cents': 12000, 'max_lateness': 2},
        {'total_cents': 17000, 'max_lateness': 0},
    ]


def test_optimality_proof_is_exhaustive_and_replayable(tmp_path):
    db = setup_db(tmp_path)
    register(db, 'BUY', FIX / 'venue-buy-case.json')
    payload = plan_purchases(db, 'BUY')
    proof = payload['proof']
    assert proof['solver'] == 'exhaustive_integer_enumeration'
    assert proof['bounds'] == {'max_skus': 8, 'max_vendors': 3}
    assert proof['leaves'] == 6  # DISPLAY-CASE 3 种拆法 × LABEL-SET 2 种拆法
    # 无更优订单说明随合同与计划落库，可重放核对
    report = load_report(db)
    for c in report['contracts']:
        assert 'no_better_order_note' in c['contract_proof']
        assert c['contract_proof']['feasible_count'] == proof['feasible_count']


# ---------- 同 SKU 整数拆分到多个供应商 ----------

def test_sku_split_across_vendors_is_optimal(tmp_path):
    db = setup_db(tmp_path)
    case = write_case(tmp_path / 'fx', 'BUY', [
        {'zone': 'A', 'sku': 'K', 'qty': 4, 'deadline_day': 10}],
        [('S', 'K', 1000, 3, 5, 0), ('F', 'K', 100, 1, 5, 0)])
    register(db, 'BUY', case)
    payload = plan_purchases(db, 'BUY')
    assert payload['status'] == '可签发'
    report = load_report(db)
    c = next(x for x in report['contracts'] if x['objective'] == 'min_total_cost')
    idx = lines_index(c)
    assert idx[('S', 'K')]['qty'] == 3 and idx[('F', 'K')]['qty'] == 1
    assert c['total_cents'] == 3100  # 拆单严格优于 S 全包的 4000
    # 齐套到货按各供应商到货联合核对（取最晚）
    assert c['requirements'][0]['effective_arrival_day'] == 5


def test_split_with_moq_rejected_when_vendor_total_below_threshold(tmp_path):
    db = setup_db(tmp_path)
    case = write_case(tmp_path / 'fx', 'BUY', [
        {'zone': 'A', 'sku': 'K', 'qty': 4, 'deadline_day': 10}],
        [('S', 'K', 1000, 4, 5, 0), ('F', 'K', 100, 1, 5, 5000)])
    register(db, 'BUY', case)
    payload = plan_purchases(db, 'BUY')
    # F 仅买 1 件=100 分，低于 5000 起订额；唯一完整可行解是 S 全包
    report = load_report(db)
    c = next(x for x in report['contracts'] if x['objective'] == 'min_total_cost')
    assert set(lines_index(c)) == {('S', 'K')}
    assert c['total_cents'] == 4000
    assert payload['proof']['rejected']['moq_not_satisfied'] == 1


def test_split_satisfies_moq_when_combined_lines_reach_threshold(tmp_path):
    db = setup_db(tmp_path)
    case = write_case(tmp_path / 'fx', 'BUY', [
        {'zone': 'A', 'sku': 'K1', 'qty': 2, 'deadline_day': 10},
        {'zone': 'B', 'sku': 'K2', 'qty': 2, 'deadline_day': 10}],
        [('S', 'K1', 100, 2, 5, 300), ('S', 'K2', 100, 1, 5, 300),
         ('F', 'K1', 110, 2, 5, 0), ('F', 'K2', 110, 2, 5, 0)])
    register(db, 'BUY', case)
    payload = plan_purchases(db, 'BUY')
    assert payload['status'] == '可签发'
    report = load_report(db)
    c = next(x for x in report['contracts'] if x['objective'] == 'min_total_cost')
    idx = lines_index(c)
    # S 容量不足以全包；向 S 买 K1×2 + K2×1 = 300 恰达跨行起订额，K2 余件买 F
    assert idx[('S', 'K1')]['qty'] == 2 and idx[('S', 'K2')]['qty'] == 1
    assert idx[('F', 'K2')]['qty'] == 1
    assert c['total_cents'] == 410  # 优于 F 全包的 440
    s_total = next(v for v in c['vendor_totals'] if v['vendor'] == 'S')
    assert s_total['total_cents'] == 300 and s_total['moq_satisfied'] == 1


# ---------- 报价数量恰满 ----------

def test_available_qty_exactly_fills_requirement(tmp_path):
    db = setup_db(tmp_path)
    case = write_case(tmp_path / 'fx', 'BUY', [
        {'zone': 'A', 'sku': 'K', 'qty': 5, 'deadline_day': 10}],
        [('S', 'K', 100, 5, 3, 0)])
    register(db, 'BUY', case)
    payload = plan_purchases(db, 'BUY')
    assert payload['status'] == '可签发'
    report = load_report(db)
    line = report['contracts'][0]['lines'][0]
    assert line['qty'] == 5 and line['available_qty'] == 5
    assert line['line_cents'] == 500


# ---------- 不同展区期限分别计算正逾期 ----------

def test_zone_deadlines_drive_separate_lateness(tmp_path):
    db = setup_db(tmp_path)
    case = write_case(tmp_path / 'fx', 'BUY', [
        {'zone': 'A', 'sku': 'K', 'qty': 1, 'deadline_day': 3},
        {'zone': 'B', 'sku': 'K', 'qty': 1, 'deadline_day': 8}],
        [('S', 'K', 100, 2, 5, 0)])
    register(db, 'BUY', case)
    plan_purchases(db, 'BUY')
    report = load_report(db)
    c = report['contracts'][0]
    lat = {r['zone']: r['lateness'] for r in c['requirements']}
    assert lat == {'A': 2, 'B': 0}
    assert c['max_lateness'] == 2


def test_split_arrival_is_jointly_checked_latest_wins(tmp_path):
    db = setup_db(tmp_path)
    case = write_case(tmp_path / 'fx', 'BUY', [
        {'zone': 'A', 'sku': 'K', 'qty': 2, 'deadline_day': 4}],
        [('S', 'K', 100, 1, 2, 0), ('F', 'K', 100, 1, 6, 0)])
    register(db, 'BUY', case)
    plan_purchases(db, 'BUY')
    report = load_report(db)
    c = next(x for x in report['contracts'] if x['objective'] == 'min_max_lateness')
    # 唯一货源组合：S 第 2 日、F 第 6 日，齐套按最晚第 6 日，逾期 2 天
    assert c['requirements'][0]['effective_arrival_day'] == 6
    assert c['requirements'][0]['lateness'] == 2


# ---------- 缺货 / 容量不足阻断：整单待核，不签发部分订单 ----------

def test_stockout_blocks_whole_order_as_pending(tmp_path):
    db = setup_db(tmp_path)
    case = write_case(tmp_path / 'fx', 'BUY', [
        {'zone': 'A', 'sku': 'K', 'qty': 5, 'deadline_day': 10}],
        [('S', 'K', 100, 4, 3, 0)])
    register(db, 'BUY', case)
    payload = plan_purchases(db, 'BUY')
    assert payload['status'] == '待核'
    assert 'capacity_insufficient' in payload['proof']['diagnostics']
    assert payload['contracts'] == []
    report = load_report(db)
    assert report['status'] == '待核' and report['contracts'] == []
    c = connect(db)
    try:
        assert c.execute('SELECT count(*) FROM purchase_contract').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM purchase_order_line').fetchone()[0] == 0
    finally:
        c.close()


def test_moq_makes_no_complete_feasible_order(tmp_path):
    db = setup_db(tmp_path)
    case = write_case(tmp_path / 'fx', 'BUY', [
        {'zone': 'A', 'sku': 'K', 'qty': 1, 'deadline_day': 10}],
        [('S', 'K', 100, 1, 3, 9999)])
    register(db, 'BUY', case)
    payload = plan_purchases(db, 'BUY')
    assert payload['status'] == '待核'
    assert payload['proof']['diagnostics'] == ['no_complete_feasible_order']
    assert payload['proof']['rejected']['moq_not_satisfied'] == 1


@pytest.mark.parametrize('rows,diag', [
    # 非正价格
    ([('S', 'K', 0, 5, 3, 0)], 'unit_price_not_positive'),
    ([('S', 'K', -5, 5, 3, 0)], 'unit_price_not_positive'),
    # 重复且相互矛盾的报价（同供应商同 SKU 两行）
    ([('S', 'K', 100, 3, 3, 0), ('S', 'K', 200, 3, 3, 0)], 'quote_duplicate_conflict'),
    # 同供应商跨行起订额矛盾
    ([('S', 'K', 100, 3, 3, 100), ('S', 'L', 100, 3, 3, 200)], 'min_order_conflict'),
    # 缺项：需求 SKU 无任何报价
    ([('S', 'L', 100, 3, 3, 0)], 'quote_missing'),
])
def test_bad_quotes_force_pending(tmp_path, rows, diag):
    db = setup_db(tmp_path)
    req = [{'zone': 'A', 'sku': 'K', 'qty': 1, 'deadline_day': 10}]
    if diag == 'min_order_conflict':
        req.append({'zone': 'B', 'sku': 'L', 'qty': 1, 'deadline_day': 10})
    case = write_case(tmp_path / 'fx', 'BUY', req, rows)
    register(db, 'BUY', case)
    payload = plan_purchases(db, 'BUY')
    assert payload['status'] == '待核'
    assert diag in payload['proof']['diagnostics']
    assert payload['contracts'] == []


def test_invalid_zone_identity_blocks(tmp_path):
    db = setup_db(tmp_path, zones=(('A', 5),))
    case = write_case(tmp_path / 'fx', 'BUY', [
        {'zone': 'Z', 'sku': 'K', 'qty': 1, 'deadline_day': 10}],
        [('S', 'K', 100, 1, 3, 0)])
    register(db, 'BUY', case)
    payload = plan_purchases(db, 'BUY')
    assert payload['status'] == '待核'
    assert 'venue_zone_missing' in payload['proof']['diagnostics']

def test_malformed_requirement_numbers_block(tmp_path):
    db = setup_db(tmp_path)
    case = write_case(tmp_path / 'fx', 'BUY', [
        {'zone': 'A', 'sku': 'K', 'qty': 'two', 'deadline_day': 10}],
        [('S', 'K', 100, 2, 3, 0)])
    register(db, 'BUY', case)
    payload = plan_purchases(db, 'BUY')
    assert payload['status'] == '待核'
    assert 'requirement_qty_invalid' in payload['proof']['diagnostics']


# ---------- 报价修订：旧单显式取消/补购，不叠加收费 ----------

def test_quote_revision_cancels_old_contract_with_delta(tmp_path):
    root = tmp_path / 'v1'; db = setup_db(tmp_path)
    # V1：S 便宜但慢（第 6 日），F 快但贵；最低成本选 S=2000，最短逾期选 F=2400
    case1 = write_case(root, 'BUY-V1', [
        {'zone': 'A', 'sku': 'K', 'qty': 2, 'deadline_day': 4}],
        [('S', 'K', 1000, 2, 6, 0), ('F', 'K', 1200, 2, 2, 0)])
    register(db, 'BUY-V1', case1)
    p1 = plan_purchases(db, 'BUY-V1')
    by1 = contracts_by_objective(p1)
    assert by1['min_total_cost']['total_cents'] == 2000
    assert by1['min_max_lateness']['total_cents'] == 2400

    # V2：S 补价降到 200/件，仍第 6 日到；两目标最优解都改变
    root2 = tmp_path / 'v2'
    case2 = write_case(root2, 'BUY-V2', [
        {'zone': 'A', 'sku': 'K', 'qty': 2, 'deadline_day': 4}],
        [('S', 'K', 200, 2, 6, 0), ('F', 'K', 1200, 2, 2, 0)])
    register(db, 'BUY-V2', case2)
    payload2 = plan_purchases(db, 'BUY-V2')
    assert payload2['status'] == '可签发'

    c = connect(db)
    try:
        old = c.execute("SELECT status,total_cents FROM purchase_contract WHERE id=?",
                        ('PO-BUY-V1-min_total_cost',)).fetchone()
        assert old['status'] == '取消' and old['total_cents'] == 2000
        new = c.execute("SELECT status,total_cents FROM purchase_contract WHERE id=?",
                        ('PO-BUY-V2-min_total_cost',)).fetchone()
        assert new['status'] == '有效' and new['total_cents'] == 400
        sup = c.execute("SELECT * FROM contract_supersession WHERE old_contract_id=?",
                        ('PO-BUY-V1-min_total_cost',)).fetchone()
        assert sup['relation'] == '替代' and sup['delta_cents'] == 400 - 2000
        # 同一需求每个目标只有一份有效合同，不叠加收费
        active = c.execute("SELECT count(*) FROM purchase_contract pc JOIN purchase_requirement pr "
                           "ON pr.contract_id=pc.id WHERE pc.status='有效' AND pr.zone='A' AND pr.sku='K'").fetchone()[0]
        assert active == 2  # 两个目标各一份有效合同
    finally:
        c.close()

    report = load_report(db, 'BUY-V2', 'readiness-v2.json')
    assert report['supersessions'] and '取消' in report['supersessions'][0]['note']


def test_revision_with_extra_requirement_is_recorded_as_topup(tmp_path):
    db = setup_db(tmp_path)
    # 旧单覆盖 A/K 与 B/L；新案例保留 A/K 并新增 C/M，B/L 成为旧单独有需求 → 补购
    case1 = write_case(tmp_path / 'v1', 'BUY-V1', [
        {'zone': 'A', 'sku': 'K', 'qty': 1, 'deadline_day': 10},
        {'zone': 'B', 'sku': 'L', 'qty': 1, 'deadline_day': 10}],
        [('S', 'K', 100, 2, 3, 0), ('S', 'L', 100, 2, 3, 0)])
    register(db, 'BUY-V1', case1); plan_purchases(db, 'BUY-V1')
    case2 = write_case(tmp_path / 'v2', 'BUY-V2', [
        {'zone': 'A', 'sku': 'K', 'qty': 1, 'deadline_day': 10},
        {'zone': 'C', 'sku': 'M', 'qty': 1, 'deadline_day': 10}],
        [('S', 'K', 100, 2, 3, 0), ('S', 'M', 100, 2, 3, 0)])
    register(db, 'BUY-V2', case2); plan_purchases(db, 'BUY-V2')
    c = connect(db)
    try:
        rows = c.execute("SELECT relation,note FROM contract_supersession").fetchall()
        assert any(r['relation'] == '补购' for r in rows)
        assert any('取消' in r['note'] for r in rows)
    finally:
        c.close()


def test_same_case_rerun_is_stable_and_never_overwritten(tmp_path):
    db = setup_db(tmp_path)
    register(db, 'BUY', FIX / 'venue-buy-case.json')
    first = plan_purchases(db, 'BUY')
    second = plan_purchases(db, 'BUY', plan_id='SHOULD-NOT-APPLY')
    assert second['plan_id'] == first['plan_id']
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == \
           json.dumps(second, ensure_ascii=False, sort_keys=True)
    c = connect(db)
    try:
        assert c.execute('SELECT count(*) FROM purchase_plan').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM purchase_contract').fetchone()[0] == 2
        assert c.execute('SELECT count(*) FROM purchase_order_line').fetchone()[0] == 4
    finally:
        c.close()


def test_changed_fixture_under_same_case_id_conflicts(tmp_path):
    db = setup_db(tmp_path)
    register(db, 'BUY', FIX / 'venue-buy-case.json')
    revised = write_case(tmp_path / 'fx', 'BUY', [
        {'zone': 'A', 'sku': 'DISPLAY-CASE', 'qty': 3, 'deadline_day': 4},
        {'zone': 'B', 'sku': 'LABEL-SET', 'qty': 1, 'deadline_day': 7}],
        [('S', 'DISPLAY-CASE', 5000, 3, 6, 12000), ('S', 'LABEL-SET', 2000, 1, 6, 12000)])
    with pytest.raises(ValueError, match='venue_procurement_conflict'):
        register(db, 'BUY', revised)


# ---------- 与闸机证据台互不改写 ----------

def test_procurement_does_not_touch_gate_tables(tmp_path):
    from gateflow.__main__ import demo
    report_path = demo(tmp_path / 'gate')
    gate = json.loads(report_path.read_text(encoding='utf-8'))
    event_count = len(gate['event']); decision_count = len(gate['decision'])
    db = tmp_path / 'gate' / 'gateflow.db'
    register(db, 'BUY', FIX / 'venue-buy-case.json')
    plan_purchases(db, 'BUY')
    c = connect(db)
    try:
        assert c.execute('SELECT count(*) FROM event').fetchone()[0] == event_count
        assert c.execute('SELECT count(*) FROM decision').fetchone()[0] == decision_count
        assert c.execute('SELECT count(*) FROM ticket').fetchone()[0] == 2
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        c.close()


def test_report_without_plan_raises(tmp_path):
    db = setup_db(tmp_path)
    register(db, 'BUY', FIX / 'venue-buy-case.json')
    with pytest.raises(ValueError, match='purchase_plan_missing'):
        readiness_report(db, 'BUY', tmp_path / 'x.json')


def test_plan_without_registered_case_raises(tmp_path):
    db = setup_db(tmp_path)
    with pytest.raises(ValueError, match='venue_procurement_case_missing'):
        plan_purchases(db, 'GHOST')


def test_case_beyond_local_bound_is_pending_not_approximated(tmp_path):
    db = setup_db(tmp_path, zones=[(f'Z{i}', 2) for i in range(9)])
    requirements = [{'zone': f'Z{i}', 'sku': f'K{i}', 'qty': 1, 'deadline_day': 10} for i in range(9)]
    quotes = [('S', f'K{i}', 100, 1, 5, 0) for i in range(9)]
    case = write_case(tmp_path / 'fx', 'BUY', requirements, quotes)
    register(db, 'BUY', case)
    payload = plan_purchases(db, 'BUY')
    assert payload['status'] == '待核'
    assert 'case_outside_exhaustive_bound' in payload['proof']['diagnostics']
    assert payload['contracts'] == []


def test_four_vendors_exceeds_local_bound(tmp_path):
    db = setup_db(tmp_path)
    quotes = [(v, 'K', 100, 1, 5, 0) for v in ('S1', 'S2', 'S3', 'S4')]
    case = write_case(tmp_path / 'fx', 'BUY', [
        {'zone': 'A', 'sku': 'K', 'qty': 1, 'deadline_day': 10}], quotes)
    register(db, 'BUY', case)
    payload = plan_purchases(db, 'BUY')
    assert payload['status'] == '待核'
    assert 'case_outside_exhaustive_bound' in payload['proof']['diagnostics']


# ---------- SQLite 迁移：旧库自动获得新表 ----------

def test_schema_migration_adds_tables_to_legacy_db(tmp_path):
    db_path = tmp_path / 'legacy.db'
    raw = sqlite3.connect(db_path)
    raw.executescript(
        'CREATE TABLE zone(id TEXT PRIMARY KEY,capacity INTEGER NOT NULL);'
        'CREATE TABLE venue_procurement_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL);')
    raw.commit(); raw.close()
    db = connect(db_path)
    try:
        names = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ('purchase_plan', 'purchase_contract', 'purchase_order_line',
                  'purchase_vendor_total', 'purchase_requirement', 'contract_supersession'):
            assert t in names
    finally:
        db.close()
