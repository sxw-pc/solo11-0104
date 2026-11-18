"""布展搬运：冻结展品库存与搬运请求，生成精确最优的取展、运送与归车路线。

计划仅为派工证据：不表明展品已实物交接，也不改变闸机事件、门票客流或分区超容结论。
求解对最多 8 件展品、2 组人员的本地案例精确最优：枚举展品归属，子集动态规划遍历
全部取放顺序，每段取时间依赖的最短可通行路径（有向通道、按时窗关闭、允许等待），
容量与班次约束全程生效。不可行时报告原因，不静默少搬一件。
"""
import hashlib, json
from datetime import timedelta
from heapq import heappop, heappush
from pathlib import Path
from .ingest import at
from .store import connect

SOLVER_VERSION = '1'
CONTRACTS = ('min_walk', 'min_max_lateness')
MAX_EXHIBITS = 8
MAX_CREWS = 2
INF = 1 << 60


def load_catalog(db_path, fixture):
    doc = json.loads(Path(fixture).read_text(encoding='utf-8'))
    db = connect(db_path)
    try:
        with db:
            for item in doc['exhibits']:
                try:
                    units = item['crate_units']
                    nodes = (item['id'], item['name'], item['current_node'], item['target_node'], item['target_zone'])
                except (KeyError, TypeError):
                    raise ValueError('bad_catalog')
                if not all(isinstance(x, str) and x for x in nodes):
                    raise ValueError('bad_catalog')
                if isinstance(units, bool) or not isinstance(units, int) or units < 1:
                    raise ValueError('bad_catalog')
                if not db.execute('SELECT 1 FROM zone WHERE id=?', (item['target_zone'],)).fetchone():
                    raise ValueError('exhibit_zone_missing')
                db.execute('INSERT OR IGNORE INTO exhibit VALUES (?,?,?,?,?,?)',
                           (item['id'], item['name'], item['current_node'], item['target_node'], item['target_zone'], units))
    finally:
        db.close()
    return {'exhibits': len(doc['exhibits'])}


def register(db_path, case_id, fixture):
    path = Path(fixture).resolve(); raw = path.read_bytes(); doc = json.loads(raw)
    if not case_id:
        raise ValueError('bad_move_case')
    _validate_case(doc)
    db = connect(db_path)
    try:
        with db:
            items = []
            for move in doc['moves']:
                row = db.execute('SELECT * FROM exhibit WHERE id=?', (move['exhibit'],)).fetchone()
                if not row:
                    raise ValueError('exhibit_missing')
                items.append(dict(row))
            if len({x['id'] for x in items}) != len(items):
                raise ValueError('duplicate_exhibit')
            digest = hashlib.sha256(raw).hexdigest()
            payload = json.dumps({'fixture': str(path), 'case': doc, 'exhibits': items}, sort_keys=True, separators=(',', ':'))
            old = db.execute('SELECT digest FROM exhibit_move_case WHERE id=?', (case_id,)).fetchone()
            if old and old['digest'] != digest:
                raise ValueError('move_case_conflict')
            db.execute('INSERT OR IGNORE INTO exhibit_move_case VALUES (?,?,?)', (case_id, payload, digest))
    finally:
        db.close()
    return {'case_id': case_id, 'digest': digest, 'moves': len(items)}


def _validate_case(doc):
    """校验并规范化搬运案例；结构性问题一律报 bad_move_case。"""
    if not isinstance(doc, dict):
        raise ValueError('bad_move_case')
    try:
        depot = doc['depot']
        start = at(doc['shift_start']); end = at(doc['shift_end'])
        moves, arcs, crews = doc['moves'], doc['arcs'], doc['crews']
    except (KeyError, TypeError, ValueError):
        raise ValueError('bad_move_case')
    if not isinstance(depot, str) or not depot or not moves or not arcs or not crews:
        raise ValueError('bad_move_case')
    if not end > start:
        raise ValueError('bad_move_case')
    seconds = (end - start).total_seconds()
    if seconds % 60:
        raise ValueError('bad_move_case')
    if len(moves) > MAX_EXHIBITS or len(crews) > MAX_CREWS:
        raise ValueError('case_too_large')
    norm_crews, seen = [], set()
    for c in crews:
        try:
            cid, cap, cl = c['id'], c['cart_capacity'], c['clearance']
        except (KeyError, TypeError):
            raise ValueError('bad_move_case')
        if not isinstance(cid, str) or not cid or cid in seen:
            raise ValueError('bad_move_case')
        if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
            raise ValueError('bad_move_case')
        if not isinstance(cl, list) or not cl or not all(isinstance(z, str) and z for z in cl):
            raise ValueError('bad_move_case')
        seen.add(cid)
        norm_crews.append({'id': cid, 'cart_capacity': cap, 'clearance': sorted(set(cl))})
    deadlines = {}
    for mv in moves:
        try:
            ex, dl = mv['exhibit'], mv['deadline_minutes']
        except (KeyError, TypeError):
            raise ValueError('bad_move_case')
        if not isinstance(ex, str) or not ex:
            raise ValueError('bad_move_case')
        if isinstance(dl, bool) or not isinstance(dl, int) or dl < 0:
            raise ValueError('bad_move_case')
        if ex in deadlines:
            raise ValueError('duplicate_exhibit')
        deadlines[ex] = dl
    norm_arcs, arc_map, nodes, has_closures = [], {}, {depot}, False
    for a in arcs:
        try:
            frm, to, minutes, closed = a['from'], a['to'], a['minutes'], a.get('closed', [])
        except (KeyError, TypeError):
            raise ValueError('bad_move_case')
        if not isinstance(frm, str) or not isinstance(to, str) or not frm or not to:
            raise ValueError('bad_move_case')
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 1:
            raise ValueError('bad_move_case')
        if (frm, to) in arc_map or not isinstance(closed, list):
            raise ValueError('bad_move_case')
        wins = []
        for w in closed:
            try:
                s, e = w['start'], w['end']
            except (KeyError, TypeError):
                raise ValueError('bad_move_case')
            if isinstance(s, bool) or isinstance(e, bool) or not isinstance(s, int) or not isinstance(e, int) or s < 0 or e <= s:
                raise ValueError('bad_move_case')
            wins.append((s, e))
        wins.sort()
        if wins:
            has_closures = True
        arc_map[(frm, to)] = (minutes, tuple(wins))
        norm_arcs.append((frm, to, minutes, tuple(wins)))
        nodes.add(frm); nodes.add(to)
    contracts = doc.get('contracts') or list(CONTRACTS)
    if not isinstance(contracts, list) or not contracts or any(c not in CONTRACTS for c in contracts):
        raise ValueError('bad_move_case')
    if len(set(contracts)) != len(contracts):
        raise ValueError('bad_move_case')
    return {'depot': depot, 'shift_start': start, 'shift_end': end, 'shift_minutes': int(seconds // 60),
            'crews': norm_crews, 'deadlines': deadlines, 'arcs': norm_arcs, 'arc_map': arc_map,
            'has_closures': has_closures, 'nodes': sorted(nodes), 'contracts': list(contracts)}


def _earliest_departure(t, minutes, windows):
    """不早于 t 且穿越区间 [d, d+minutes) 不碰任何闭合窗的最早出发时刻。"""
    d = t
    for s, e in windows:
        if d < e and d + minutes > s:
            d = e
    return d


def _static_paths(ctx):
    """无闭合窗时的全源有向最短路；堆序 (距离, 路径) 保证确定性。"""
    adj = {}
    for frm, to, m, _ in ctx['arcs']:
        adj.setdefault(frm, []).append((to, m))
    for frm in adj:
        adj[frm].sort()
    dist, path = {}, {}
    for src in ctx['nodes']:
        dist[(src, src)] = 0; path[(src, src)] = (src,)
        best = {src: 0}
        heap = [(0, (src,), src)]
        while heap:
            d, p, u = heappop(heap)
            if d > best.get(u, INF):
                continue
            for v, m in adj.get(u, ()):
                nd, np_ = d + m, p + (v,)
                if nd < best.get(v, INF):
                    best[v] = nd; dist[(src, v)] = nd; path[(src, v)] = np_
                    heappush(heap, (nd, np_, v))
    return dist, path


def _frontier_leg(ctx, u, v, t0):
    """时间依赖双目标最短路：返回 u→v 不早于 t0 的 Pareto (到达, 步行, 路径) 前沿。"""
    if u == v:
        return [(t0, 0, (u,))]
    shift = ctx['shift_minutes']
    labels = {u: [(t0, 0, (u,))]}
    queue, inq = [u], {u}
    while queue:
        x = queue.pop(); inq.discard(x)
        for to, m, wins in ctx['adj_full'].get(x, ()):
            for t, w, p in list(labels.get(x, ())):
                arr = _earliest_departure(t, m, wins) + m
                if arr > shift:
                    continue
                nw, np_ = w + m, p + (to,)
                fl = labels.setdefault(to, [])
                if any(t2 <= arr and w2 <= nw for t2, w2, _ in fl):
                    continue
                labels[to] = [L for L in fl if not (arr <= L[0] and nw <= L[1])] + [(arr, nw, np_)]
                if to not in inq:
                    queue.append(to); inq.add(to)
    res = labels.get(v, [])
    res.sort()
    return res


def _leg(ctx, u, v, t):
    """u→v 在时刻 t 后可通行的 (到达, 步行, 路径) 选项；不可达或超出班次为空。"""
    if not ctx['has_closures']:
        d = ctx['dist'].get((u, v))
        if d is None or t + d > ctx['shift_minutes']:
            return []
        return [(t + d, d, ctx['path'][(u, v)])]
    key = (u, v, t)
    if key not in ctx['leg_memo']:
        ctx['leg_memo'][key] = _frontier_leg(ctx, u, v, t)
    return ctx['leg_memo'][key]


def _time_path(ctx, path, t0, horizon=None):
    """沿给定节点路径按最早可通行出发重放，返回逐通道时刻表。"""
    legs, t = [], t0
    for a, b in zip(path, path[1:]):
        arc = ctx['arc_map'].get((a, b))
        if arc is None:
            raise ValueError('arc_missing')
        m, wins = arc
        d = _earliest_departure(t, m, wins)
        arr = d + m
        if horizon is not None and arr > horizon:
            raise ValueError('arc_closed')
        legs.append({'from': a, 'to': b, 'depart_minute': d, 'arrive_minute': arr, 'minutes': m})
        t = arr
    return legs


def _crew_frontier(ctx, crew, subset, items, stats):
    """单组人员搬运子集 subset 的精确 DP。

    状态 (已取, 已送, 所在节点)，标签 (时刻, 累计步行, 至今最大正逾期) 按分量 Pareto 剪枝；
    返回 {'terminals': [...], 'arena': [...]}，人员结构上无法承运时返回 None。
    """
    cap = crew['cart_capacity']
    n = len(items)
    for i in range(n):
        if subset >> i & 1:
            e = items[i]
            if e['target_zone'] not in crew['clearance'] or e['crate_units'] > cap:
                return None
    depot, shift, dl = ctx['depot'], ctx['shift_minutes'], ctx['deadlines']
    full = subset
    arena, fronts, heap = [], {}, []
    seq = created = 0

    def add(time, walk, maxlat, state, prev, move):
        nonlocal seq, created
        if time > shift:
            return
        f = fronts.setdefault(state, [])
        for j in f:
            L = arena[j]
            if L[0] <= time and L[1] <= walk and L[2] <= maxlat:
                return
        fronts[state] = [j for j in f if not (time <= arena[j][0] and walk <= arena[j][1] and maxlat <= arena[j][2])]
        idx = len(arena)
        arena.append((time, walk, maxlat, state, prev, move))
        fronts[state].append(idx)
        seq += 1; created += 1
        heappush(heap, (time, walk, maxlat, seq, idx))

    add(0, 0, 0, (0, 0, depot), -1, ('start',))
    terminals = []
    while heap:
        _, _, _, _, idx = heappop(heap)
        cur = fronts.get(arena[idx][3])
        if cur is None or idx not in cur:
            continue
        time, walk, maxlat, state, _, _ = arena[idx]
        picked, delivered, loc = state
        if delivered == full and loc == depot:
            terminals.append(idx)
            continue
        load = sum(items[i]['crate_units'] for i in range(n) if (picked >> i & 1) and not (delivered >> i & 1))
        for i in range(n):
            if not (subset >> i & 1) or (picked >> i & 1):
                continue
            e = items[i]
            if e['current_node'] == loc and load + e['crate_units'] <= cap:
                add(time, walk, maxlat, (picked | (1 << i), delivered, loc), idx, ('pickup', e['id']))
        for i in range(n):
            if not (picked >> i & 1) or (delivered >> i & 1):
                continue
            e = items[i]
            if e['target_node'] == loc:
                late = time - dl[e['id']]
                add(time, walk, max(maxlat, late if late > 0 else 0),
                    (picked, delivered | (1 << i), loc), idx, ('deliver', e['id']))
        targets = set()
        for i in range(n):
            if not (subset >> i & 1):
                continue
            e = items[i]
            if not (picked >> i & 1) and e['current_node'] != loc:
                targets.add(e['current_node'])
            if (picked >> i & 1) and not (delivered >> i & 1) and e['target_node'] != loc:
                targets.add(e['target_node'])
        if delivered == full and loc != depot:
            targets.add(depot)
        for tgt in sorted(targets):
            for arr, wlk, p in _leg(ctx, loc, tgt, time):
                add(arr, walk + wlk, maxlat, (picked, delivered, tgt), idx,
                    ('travel', loc, tgt, p, time, arr, wlk))
    if stats is not None:
        stats['states'] += len(fronts)
        stats['labels'] += created
    return {'terminals': terminals, 'arena': arena}


def _iso(ctx, t):
    return (ctx['shift_start'] + timedelta(minutes=t)).isoformat()


def _reconstruct(ctx, arena, term_idx, by_id):
    """由回溯指针重建单组人员的停车/装卸序列。"""
    chain, idx = [], term_idx
    while True:
        mv = arena[idx][5]
        if mv[0] == 'start':
            break
        chain.append(mv); idx = arena[idx][4]
    chain.reverse()
    stops, cart, t, seq = [], [], 0, 0

    def units():
        return sum(by_id[x]['crate_units'] for x in cart)

    for mv in chain:
        if mv[0] == 'travel':
            _, frm, to, path, t0, arr, wlk = mv
            legs = _time_path(ctx, path, t0, horizon=ctx['shift_minutes'])
            seq += 1
            stops.append({'seq': seq, 'action': 'travel', 'from': frm, 'to': to, 'path': list(path),
                          'arcs': legs, 'depart_minute': t0, 'arrive_minute': arr, 'walk_minutes': wlk,
                          'wait_minutes': arr - t0 - wlk, 'depart_at': _iso(ctx, t0), 'arrive_at': _iso(ctx, arr),
                          'cart_exhibits': sorted(cart), 'cart_crate_units': units()})
            t = arr
        elif mv[0] == 'pickup':
            e = by_id[mv[1]]
            cart.append(mv[1]); seq += 1
            stops.append({'seq': seq, 'action': 'pickup', 'node': e['current_node'], 'at_minute': t,
                          'at': _iso(ctx, t), 'exhibit': mv[1], 'crate_units': e['crate_units'],
                          'target_node': e['target_node'], 'target_zone': e['target_zone'],
                          'cart_exhibits': sorted(cart), 'cart_crate_units': units()})
        else:
            e = by_id[mv[1]]
            late = t - ctx['deadlines'][mv[1]]
            cart.remove(mv[1]); seq += 1
            stops.append({'seq': seq, 'action': 'deliver', 'node': e['target_node'], 'at_minute': t,
                          'at': _iso(ctx, t), 'exhibit': mv[1], 'target_zone': e['target_zone'],
                          'deadline_minute': ctx['deadlines'][mv[1]],
                          'lateness_minutes': late if late > 0 else 0,
                          'cart_exhibits': sorted(cart), 'cart_crate_units': units()})
    return stops, t


def _replay(ctx, by_id, crew, route):
    """重放一条路线：校验有向通道、闭合窗、容量、资格、班次，重算目标值。"""
    t, loc = 0, ctx['depot']
    cart, done, picked = [], set(), set()
    walk_total = maxlat = 0
    for st in route['stops']:
        a = st['action']
        if a == 'travel':
            path = st['path']
            if not path or path[0] != loc:
                raise ValueError('replay_path_broken')
            legs = _time_path(ctx, tuple(path), t, horizon=ctx['shift_minutes'])
            arr = legs[-1]['arrive_minute'] if legs else t
            wlk = sum(x['minutes'] for x in legs)
            if arr != st['arrive_minute'] or wlk != st['walk_minutes']:
                raise ValueError('replay_time_mismatch')
            if [(x['from'], x['to']) for x in legs] != [(x['from'], x['to']) for x in st['arcs']]:
                raise ValueError('replay_arc_mismatch')
            if sorted(cart) != st['cart_exhibits']:
                raise ValueError('replay_cart_mismatch')
            walk_total += wlk; t = arr; loc = path[-1]
        elif a == 'pickup':
            e = by_id[st['exhibit']]
            if e['current_node'] != loc or st['node'] != loc or st['at_minute'] != t:
                raise ValueError('replay_pickup_node')
            if e['id'] in picked:
                raise ValueError('replay_duplicate_pickup')
            if sum(by_id[x]['crate_units'] for x in cart) + e['crate_units'] > crew['cart_capacity']:
                raise ValueError('replay_capacity')
            picked.add(e['id']); cart.append(e['id'])
            if sorted(cart) != st['cart_exhibits']:
                raise ValueError('replay_cart_mismatch')
        elif a == 'deliver':
            e = by_id[st['exhibit']]
            if e['target_node'] != loc or st['node'] != loc or st['at_minute'] != t:
                raise ValueError('replay_deliver_node')
            if e['id'] not in cart:
                raise ValueError('replay_not_on_cart')
            if e['target_zone'] not in crew['clearance']:
                raise ValueError('replay_clearance')
            late = t - ctx['deadlines'][e['id']]
            late = late if late > 0 else 0
            if st['lateness_minutes'] != late:
                raise ValueError('replay_lateness')
            maxlat = max(maxlat, late)
            cart.remove(e['id']); done.add(e['id'])
            if sorted(cart) != st['cart_exhibits']:
                raise ValueError('replay_cart_mismatch')
        else:
            raise ValueError('replay_action')
    if loc != ctx['depot']:
        raise ValueError('replay_not_returned')
    if t > ctx['shift_minutes']:
        raise ValueError('replay_shift')
    if cart:
        raise ValueError('replay_cart_not_empty')
    return walk_total, maxlat, done


def _note(contract, search):
    pairs = ','.join(f"({p['total_walk_minutes']},{p['max_lateness_minutes']})" for p in search['pareto_frontier'])
    base = (f"精确最优：枚举 {search['assignments_enumerated']} 种展品归属（可行 {search['assignments_feasible']} 种），"
            f"子集动态规划遍历全部取放顺序（{search['states_explored']} 状态、{search['labels_created']} 标签），"
            f"全局 Pareto 前沿(总步行,最大正逾期)=[{pairs}]；")
    if contract == 'min_walk':
        return base + '按合同先比全员总步行分钟、再比最大正逾期，不存在更优的可行计划。'
    return base + '按合同先比最大正逾期、再比全员总步行分钟，不存在更优的可行计划。'


def _solve(doc, exhibits):
    """精确求解冻结案例，返回 (ctx, solved)；路线自带重放自检。"""
    ctx = _validate_case(doc)
    by_id = {}
    for e in exhibits:
        try:
            units = e['crate_units']
            keys = (e['id'], e['name'], e['current_node'], e['target_node'], e['target_zone'])
        except (KeyError, TypeError):
            raise ValueError('bad_catalog')
        if not all(isinstance(x, str) and x for x in keys):
            raise ValueError('bad_catalog')
        if isinstance(units, bool) or not isinstance(units, int) or units < 1:
            raise ValueError('bad_catalog')
        by_id[e['id']] = dict(e)
    if sorted(by_id) != sorted(ctx['deadlines']):
        raise ValueError('exhibit_missing')
    items = [by_id[i] for i in sorted(by_id)]
    n = len(items)
    adj_full = {}
    for frm, to, m, wins in ctx['arcs']:
        adj_full.setdefault(frm, []).append((to, m, wins))
    for frm in adj_full:
        adj_full[frm].sort(key=lambda x: (x[0], x[1]))
    ctx['adj_full'] = adj_full
    if not ctx['has_closures']:
        ctx['dist'], ctx['path'] = _static_paths(ctx)
    ctx['leg_memo'] = {}
    stats = {'states': 0, 'labels': 0}
    crews = ctx['crews']

    def infeasible(reasons, enumerated=0, feasible=0):
        contracts = {c: {'status': 'infeasible', 'objective': None, 'exhibits_delivered': [], 'routes': [],
                         'optimality_note': '无可行计划，未生成路线；未静默丢弃任何展品。'}
                     for c in ctx['contracts']}
        search = {'assignments_enumerated': enumerated, 'assignments_feasible': feasible,
                  'states_explored': stats['states'], 'labels_created': stats['labels'], 'pareto_frontier': []}
        return ctx, {'status': 'infeasible', 'contracts': contracts, 'search': search,
                     'infeasible': {'reasons': reasons, 'exhibits_undelivered': [e['id'] for e in items]}}

    reasons = []
    for e in items:
        cleared = [c for c in crews if e['target_zone'] in c['clearance']]
        if not cleared:
            reasons.append({'code': 'clearance_missing', 'exhibit': e['id'], 'target_zone': e['target_zone'],
                            'detail': f"没有人员的通行资格覆盖目标分区 {e['target_zone']}"})
        elif all(e['crate_units'] > c['cart_capacity'] for c in cleared):
            reasons.append({'code': 'capacity_exceeded', 'exhibit': e['id'],
                            'detail': f"箱数 {e['crate_units']} 超过所有具备分区 {e['target_zone']} 资格人员的车容量"})
    reach_from = {}

    def reach(src):
        if src not in reach_from:
            seen, stack = {src}, [src]
            while stack:
                x = stack.pop()
                for to, _, _ in adj_full.get(x, ()):
                    if to not in seen:
                        seen.add(to); stack.append(to)
            reach_from[src] = seen
        return reach_from[src]

    depot = ctx['depot']
    for e in items:
        if (e['current_node'] not in reach(depot) or e['target_node'] not in reach(e['current_node'])
                or depot not in reach(e['target_node'])):
            reasons.append({'code': 'node_unreachable', 'exhibit': e['id'],
                            'detail': f"有向通道无法完成 库房→{e['current_node']}→{e['target_node']}→库房 的往返"})
    if reasons:
        return infeasible(reasons)

    full = (1 << n) - 1
    masks = [full] if len(crews) == 1 else list(range(1 << n))
    cache = {}

    def crew_summary(ci, mask):
        key = (ci, mask)
        if key in cache:
            return cache[key]
        if mask == 0:
            cache[key] = [(0, 0)]
            return cache[key]
        r = _crew_frontier(ctx, crews[ci], mask, items, stats)
        if r is None:
            cache[key] = None
            return None
        terms = sorted({(r['arena'][i][1], r['arena'][i][2]) for i in r['terminals']})
        pr, best_l = [], None
        for w, l in terms:
            if best_l is not None and best_l <= l:
                continue
            pr.append((w, l)); best_l = l
        cache[key] = pr
        return pr

    combined, feasible_masks = [], 0
    for mask in masks:
        f0 = crew_summary(0, mask)
        f1 = crew_summary(1, full ^ mask) if len(crews) > 1 else [(0, 0)]
        if not f0 or not f1:
            continue
        feasible_masks += 1
        for w0, l0 in f0:
            for w1, l1 in f1:
                combined.append((w0 + w1, max(l0, l1), mask, w0, l0, w1, l1))
    if not combined:
        return infeasible([{'code': 'shift_exceeded',
                            'detail': '所有归属与顺序方案均无法在班次内完成取送并归车'}],
                          enumerated=len(masks), feasible=0)
    combined.sort(key=lambda e: (e[0], e[1]))
    frontier, best_l = [], None
    for e in combined:
        if best_l is not None and best_l <= e[1]:
            continue
        frontier.append(e); best_l = e[1]
    search = {'assignments_enumerated': len(masks), 'assignments_feasible': feasible_masks,
              'states_explored': stats['states'], 'labels_created': stats['labels'],
              'pareto_frontier': [{'total_walk_minutes': w, 'max_lateness_minutes': l} for w, l, *_ in frontier]}
    contracts_out = {}
    for contract in ctx['contracts']:
        if contract == 'min_walk':
            chosen = min(frontier, key=lambda e: (e[0], e[1]))
        else:
            chosen = min(frontier, key=lambda e: (e[1], e[0]))
        w, l, mask, w0, l0, w1, l1 = chosen
        pairs = ((0, mask, w0, l0), (1, full ^ mask, w1, l1)) if len(crews) > 1 else ((0, mask, w0, l0),)
        routes = []
        for ci, mi, wi, li in pairs:
            crew = crews[ci]
            if mi == 0:
                routes.append({'crew': crew['id'], 'cart_capacity': crew['cart_capacity'],
                               'clearance': crew['clearance'], 'stops': [], 'total_walk_minutes': 0,
                               'return_minute': 0, 'return_at': _iso(ctx, 0), 'delivered': []})
                continue
            r = _crew_frontier(ctx, crews[ci], mi, items, None)
            term = next(i for i in r['terminals'] if (r['arena'][i][1], r['arena'][i][2]) == (wi, li))
            stops, ret = _reconstruct(ctx, r['arena'], term, by_id)
            routes.append({'crew': crew['id'], 'cart_capacity': crew['cart_capacity'],
                           'clearance': crew['clearance'], 'stops': stops,
                           'total_walk_minutes': sum(s['walk_minutes'] for s in stops if s['action'] == 'travel'),
                           'return_minute': ret, 'return_at': _iso(ctx, ret),
                           'delivered': sorted(items[i]['id'] for i in range(n) if mi >> i & 1)})
        check_walk = check_ml = 0
        check_done = set()
        for route, (ci, mi, _, _) in zip(routes, pairs):
            rw, rl, done = _replay(ctx, by_id, crews[ci], route)
            if rw != route['total_walk_minutes'] or done != {items[i]['id'] for i in range(n) if mi >> i & 1}:
                raise RuntimeError('solver_inconsistent')
            check_walk += rw; check_ml = max(check_ml, rl); check_done |= done
        if check_walk != w or check_ml != l or check_done != set(by_id):
            raise RuntimeError('solver_inconsistent')
        contracts_out[contract] = {'status': 'optimal',
                                   'objective': {'total_walk_minutes': w, 'max_lateness_minutes': l},
                                   'exhibits_delivered': sorted(by_id),
                                   'routes': routes,
                                   'optimality_note': _note(contract, search),
                                   'replay_checked': True}
    return ctx, {'status': 'optimal', 'contracts': contracts_out, 'search': search, 'infeasible': None}


def _build_report(case_id, case_digest, doc, exhibits, ctx, solved):
    return {
        'kind': 'exhibit_move_plan',
        'solver_version': SOLVER_VERSION,
        'plan_only': True,
        'evidence_boundary': '仅为派工计划：不表明展品已实物交接；不改动闸机事件、门票客流与分区超容结论。',
        'case_id': case_id,
        'case_digest': case_digest,
        'shift': {'start': doc['shift_start'], 'end': doc['shift_end'], 'minutes': ctx['shift_minutes']},
        'depot': ctx['depot'],
        'exhibits': [{'id': e['id'], 'name': e['name'], 'current_node': e['current_node'],
                      'target_node': e['target_node'], 'target_zone': e['target_zone'],
                      'crate_units': e['crate_units'],
                      'deadline_minutes': ctx['deadlines'][e['id']]} for e in sorted(exhibits, key=lambda x: x['id'])],
        'crews': ctx['crews'],
        'search': solved['search'],
        'contracts': solved['contracts'],
        'infeasible': solved['infeasible'],
    }


def _plan_md(report):
    lines = ['# 布展搬运计划证据', '',
             f"- 案例 {report['case_id']}（冻结输入摘要 {report['case_digest'][:16]}…）",
             '- 仅为派工计划：不表明展品已实物交接；闸机事件、客流与分区结论不受影响。', '']
    for c, s in report['contracts'].items():
        lines.append(f"## {c}")
        if s['status'] == 'optimal':
            o = s['objective']
            lines.append(f"- 精确最优：总步行 {o['total_walk_minutes']} 分钟，最大正逾期 {o['max_lateness_minutes']} 分钟")
            for r in s['routes']:
                if not r['stops']:
                    lines.append(f"- {r['crew']}：留守库房")
                    continue
                desc = []
                for st in r['stops']:
                    if st['action'] == 'travel':
                        desc.append(f"{st['from']}→{st['to']}")
                    elif st['action'] == 'pickup':
                        desc.append(f"取{st['exhibit']}@{st['at_minute']}")
                    else:
                        desc.append(f"卸{st['exhibit']}@{st['at_minute']}")
                lines.append(f"- {r['crew']}：" + '，'.join(desc) + f"，第 {r['return_minute']} 分钟归车")
        else:
            codes = '；'.join(x['code'] for x in (report['infeasible'] or {}).get('reasons', []))
            lines.append(f"- 不可行：{codes}")
    return '\n'.join(lines) + '\n'


def _inventory_snapshot(db, ids):
    marks = ','.join('?' for _ in ids)
    return [dict(r) for r in db.execute(f'SELECT * FROM exhibit WHERE id IN ({marks}) ORDER BY id', sorted(ids))]


def _contract_digest(case_digest, contract, section):
    blob = json.dumps({'case_digest': case_digest, 'contract': contract, 'solver_version': SOLVER_VERSION,
                       'section': section}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(blob.encode()).hexdigest()


def _load_case(db, case_id):
    row = db.execute('SELECT payload,digest FROM exhibit_move_case WHERE id=?', (case_id,)).fetchone()
    if not row:
        raise ValueError('move_case_missing')
    payload = json.loads(row['payload'])
    frozen = payload['exhibits']
    expect = _inventory_snapshot(db, [e['id'] for e in frozen])
    if expect != sorted((dict(e) for e in frozen), key=lambda x: x['id']):
        raise ValueError('inventory_changed')
    return payload['case'], expect, row['digest']


def plan(db_path, case_id, out=None):
    """对冻结案例生成两种合同的精确最优路线，写入 exhibit_plan 并输出证据报告。"""
    db = connect(db_path)
    try:
        doc, exhibits, case_digest = _load_case(db, case_id)
        ctx, solved = _solve(doc, exhibits)
        report = _build_report(case_id, case_digest, doc, exhibits, ctx, solved)
        with db:
            for contract, section in solved['contracts'].items():
                digest = _contract_digest(case_digest, contract, section)
                old = db.execute('SELECT digest FROM exhibit_plan WHERE case_id=? AND contract=?',
                                 (case_id, contract)).fetchone()
                if old and old['digest'] != digest:
                    raise ValueError('plan_conflict')
                db.execute('INSERT OR IGNORE INTO exhibit_plan(case_id,contract,status,objective,payload,digest) '
                           'VALUES (?,?,?,?,?,?)',
                           (case_id, contract, section['status'],
                            json.dumps(section['objective'], ensure_ascii=False, sort_keys=True),
                            json.dumps(section, ensure_ascii=False, sort_keys=True), digest))
        if out:
            p = Path(out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            p.with_suffix('.md').write_text(_plan_md(report), encoding='utf-8')
        return {'case_id': case_id,
                'contracts': {c: {'status': s['status'], 'objective': s['objective']}
                              for c, s in solved['contracts'].items()}}
    finally:
        db.close()


def verify(db_path, case_id):
    """重放最优性证据：由冻结输入重算路线与目标值，核对已存计划摘要。"""
    db = connect(db_path)
    try:
        doc, exhibits, case_digest = _load_case(db, case_id)
        ctx, solved = _solve(doc, exhibits)
        stored = {r['contract']: r['digest'] for r in
                  db.execute('SELECT contract,digest FROM exhibit_plan WHERE case_id=?', (case_id,))}
        if not stored:
            raise ValueError('plan_missing')
        out, ok = {}, True
        for contract, section in solved['contracts'].items():
            match = stored.get(contract) == _contract_digest(case_digest, contract, section)
            ok = ok and match
            out[contract] = {'digest_match': match, 'status': section['status'], 'objective': section['objective']}
        if set(stored) != set(solved['contracts']):
            ok = False
        return {'ok': ok, 'contracts': out}
    finally:
        db.close()
