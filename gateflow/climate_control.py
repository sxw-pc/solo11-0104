"""Freeze gallery climate observations and plan a finite HVAC shift.

The module keeps the original evidence freeze (``register``) and adds an
exact planner for the small local shifts used by the venue (at most four
zones and eight slots).  Temperatures, exposures and energy are carried as
:class:`fractions.Fraction` so the optimum, the budget and the conservation
checks are exact rather than floating point approximations.

The planner solves all slots and zones jointly: a cooling action changes the
starting temperature of every later slot, and the shared cooling unit may
only serve a bounded number of zones per slot.  Per-zone threshold alarms or
"cool the currently hottest zone" heuristics are therefore not used.
"""
from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

from .store import connect

CONTRACTS = ('min_energy', 'min_max_zone_exposure')
CONTRACT_LABELS = {
    'min_energy': '先最小化用电整数分，再最小化最大分区累计暴露',
    'min_max_zone_exposure': '先最小化各区累计暴露的最大值，再最小化用电',
}
STATUS_FEASIBLE = '可行'
STATUS_REVIEW = '待核'
STATUS_INFEASIBLE = '无可行方案'
STATUS_UNRECOVERABLE = '无法追回'
STATUS_CODES = {
    STATUS_FEASIBLE: 'feasible',
    STATUS_REVIEW: 'needs_review',
    STATUS_INFEASIBLE: 'no_feasible_plan',
    STATUS_UNRECOVERABLE: 'unrecoverable',
}
MAX_ZONES = 4
MAX_SLOTS = 8
# Brute-force cross-checks are only attempted for small action spaces; the
# dynamic programme itself never depends on this limit.
_BRUTE_FORCE_LIMIT = 200_000


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _sha(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _num(value):
    """JSON-friendly number: integers stay integers, fractions become floats."""
    if isinstance(value, Fraction):
        if value.denominator == 1:
            return int(value)
        return float(value)
    if isinstance(value, bool):
        return value
    return value


def _frac(value):
    """Parse a finite numeric value into an exact Fraction, or raise ValueError."""
    if isinstance(value, bool):
        raise ValueError('not_numeric')
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('not_finite')
        return Fraction(Decimal(str(value)))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError('empty')
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise ValueError('not_numeric')
        if not number.is_finite():
            raise ValueError('not_finite')
        return Fraction(number)
    raise ValueError('not_numeric')


def _int(value):
    if isinstance(value, bool):
        raise ValueError('not_integer')
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            raise ValueError('not_integer')
    raise ValueError('not_integer')


def _parse_ts(value):
    """Return ``(utc_datetime, error_code, wall_clock)`` for an ISO timestamp.

    ``wall_clock`` is the naive local time as written in the source, used to
    detect readings that line up on the wall clock but not on the actual
    instant (a time-zone misalignment).
    """
    if not isinstance(value, str) or not value.strip():
        return None, 'timezone_error', None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None, 'timezone_error', None
    if parsed.tzinfo is None:
        return None, 'timezone_error', None
    return parsed.astimezone(timezone.utc), None, parsed.replace(tzinfo=None)


def _issue(code, subject, detail):
    return {'code': code, 'subject': str(subject), 'detail': str(detail)}


# ---------------------------------------------------------------------------
# evidence freeze (unchanged public behaviour)
# ---------------------------------------------------------------------------

def register(db_path, case_id, fixture):
    path = Path(fixture).resolve()
    raw = path.read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('zones') or not doc.get('sensors_file') or not doc.get('slots'):
        raise ValueError('bad_climate_case')
    sensor_path = (path.parent / doc['sensors_file']).resolve()
    sensor_raw = sensor_path.read_bytes()
    readings = list(csv.DictReader(sensor_raw.decode('utf-8-sig').splitlines()))
    if not readings:
        raise ValueError('climate_readings_missing')
    db = connect(db_path)
    try:
        with db:
            for zone in doc['zones']:
                if not db.execute('SELECT 1 FROM zone WHERE id=?', (zone['id'],)).fetchone():
                    raise ValueError('climate_zone_missing')
            digest = hashlib.sha256(raw + sensor_raw).hexdigest()
            payload = json.dumps({
                'fixture': str(path),
                'case': doc,
                'readings': readings,
                'sensor_sha256': hashlib.sha256(sensor_raw).hexdigest(),
            }, sort_keys=True, separators=(',', ':'))
            old = db.execute('SELECT digest FROM climate_case WHERE id=?', (case_id,)).fetchone()
            if old and old['digest'] != digest:
                raise ValueError('climate_case_conflict')
            db.execute('INSERT OR IGNORE INTO climate_case VALUES (?,?,?)', (case_id, payload, digest))
    finally:
        db.close()
    return {'case_id': case_id, 'digest': digest, 'readings': len(readings)}


def _load_case(db, case_id):
    row = db.execute('SELECT payload,digest FROM climate_case WHERE id=?', (case_id,)).fetchone()
    if not row:
        raise ValueError('climate_case_missing')
    return json.loads(row['payload']), row['digest']


# ---------------------------------------------------------------------------
# validation and case parsing
# ---------------------------------------------------------------------------

def _validate_case(case, readings, zone_table):
    """Validate the frozen case.

    Returns ``(model, issues)``.  When ``issues`` is non-empty the caller must
    publish no control command at all: a plan cannot be issued from a partial
    or conflicting evidence base.
    """
    issues = []
    zones_doc = case.get('zones')
    slots_doc = case.get('slots')
    if not isinstance(zones_doc, list) or not zones_doc:
        issues.append(_issue('zone_invalid', 'zones', 'zones must be a non-empty list'))
        return None, issues
    if not isinstance(slots_doc, list) or not slots_doc:
        issues.append(_issue('slot_invalid', 'slots', 'slots must be a non-empty list'))
        return None, issues
    if len(zones_doc) > MAX_ZONES:
        issues.append(_issue('case_too_large', 'zones', f'{len(zones_doc)} > {MAX_ZONES}'))
    if len(slots_doc) > MAX_SLOTS:
        issues.append(_issue('case_too_large', 'slots', f'{len(slots_doc)} > {MAX_SLOTS}'))

    # ---- slots -----------------------------------------------------------
    slots = []
    slot_ids = []
    for position, slot in enumerate(slots_doc):
        if not isinstance(slot, dict):
            issues.append(_issue('slot_invalid', position, 'slot must be an object'))
            continue
        try:
            slot_id = _int(slot.get('id'))
        except ValueError:
            issues.append(_issue('slot_invalid', position, 'slot id must be an integer'))
            continue
        slot_ids.append(slot_id)
        start, error, wall = _parse_ts(slot.get('start'))
        if error:
            issues.append(_issue(error, f'slot:{slot_id}', 'slot start requires an offset-aware timestamp'))
        try:
            energy = _int(slot.get('energy_cents'))
            if energy < 0:
                raise ValueError('negative')
        except ValueError:
            energy = None
            issues.append(_issue('energy_invalid', f'slot:{slot_id}', 'energy_cents must be a non-negative integer'))
        slots.append({
            'id': slot_id,
            'start': slot.get('start'),
            'start_dt': start,
            'start_wall': wall,
            'energy_cents': energy,
        })
    if len(set(slot_ids)) != len(slot_ids):
        issues.append(_issue('slot_invalid', 'slots', 'slot ids must be unique'))
    if sorted(slot_ids) != list(range(len(slot_ids))):
        issues.append(_issue('slot_invalid', 'slots', 'slot ids must be 0..n-1'))
    else:
        slots.sort(key=lambda item: item['id'])
        for earlier, later in zip(slots, slots[1:]):
            if earlier['start_dt'] and later['start_dt'] and later['start_dt'] <= earlier['start_dt']:
                issues.append(_issue('slot_invalid', f"slot:{later['id']}", 'slot starts must increase'))

    # ---- zones and heat --------------------------------------------------
    zones = []
    zone_ids = []
    parsed_doc_len = len(slots_doc)
    for zone_doc in zones_doc:
        if not isinstance(zone_doc, dict) or not zone_doc.get('id'):
            issues.append(_issue('zone_invalid', 'zone', 'zone id required'))
            continue
        zone_id = str(zone_doc['id'])
        zone_ids.append(zone_id)
        try:
            max_c = _frac(zone_doc.get('max_c'))
        except ValueError:
            max_c = None
            issues.append(_issue('zone_invalid', zone_id, 'max_c must be finite numeric'))
        heat_doc = zone_doc.get('heat_c_per_slot')
        heat = []
        if not isinstance(heat_doc, list) or len(heat_doc) != parsed_doc_len:
            issues.append(_issue('heat_missing', zone_id, 'heat_c_per_slot must cover every slot'))
            heat = [None] * parsed_doc_len
        else:
            for slot_id, value in enumerate(heat_doc):
                try:
                    heat.append(_frac(value))
                except ValueError:
                    heat.append(None)
                    issues.append(_issue('heat_missing', f'{zone_id}:slot:{slot_id}', 'heating value missing or non-numeric'))
        zones.append({'id': zone_id, 'max_c': max_c, 'heat': heat})
    if len(set(zone_ids)) != len(zone_ids):
        issues.append(_issue('zone_invalid', 'zones', 'zone ids must be unique'))

    for zone_id in sorted(set(zone_ids) - set(zone_table)):
        issues.append(_issue('climate_zone_missing', zone_id, 'zone is not registered in the venue'))

    # ---- device and budget ----------------------------------------------
    try:
        cooling = _frac(case.get('cooling_c_per_action'))
        if cooling <= 0:
            raise ValueError('nonphysical')
    except ValueError:
        cooling = None
        issues.append(_issue('cooling_nonphysical', 'cooling_c_per_action', 'cooling must be finite and positive'))
    try:
        capacity = _int(case.get('shared_cooling_capacity'))
        if capacity < 1:
            raise ValueError('capacity')
    except ValueError:
        capacity = None
        issues.append(_issue('capacity_insufficient', 'shared_cooling_capacity', 'shared device must serve at least one zone action per slot'))
    # The shared unit is single-zone by contract: at most one zone per slot.
    effective_capacity = min(capacity, 1) if capacity is not None else 0
    try:
        budget = _frac(case.get('total_exposure_budget'))
        if budget < 0:
            raise ValueError('negative')
    except ValueError:
        budget = None
        issues.append(_issue('budget_invalid', 'total_exposure_budget', 'exposure budget must be finite and non-negative'))

    # ---- sensor identity and readings -----------------------------------
    zone_sensor = {}
    sensor_zone = {}
    parsed_readings = []
    for index, reading in enumerate(readings):
        if not isinstance(reading, dict):
            issues.append(_issue('reading_invalid', index, 'reading must be an object'))
            continue
        zone_id = str(reading.get('zone', '')).strip()
        sensor = str(reading.get('sensor', '')).strip()
        if zone_id not in zone_ids:
            issues.append(_issue('reading_invalid', f'reading:{index}', f'unknown zone {zone_id!r}'))
            continue
        if not sensor:
            issues.append(_issue('sensor_identity_conflict', zone_id, 'sensor id missing'))
            continue
        if sensor in sensor_zone and sensor_zone[sensor] != zone_id:
            issues.append(_issue('sensor_identity_conflict', sensor, f'sensor shared by {sensor_zone[sensor]} and {zone_id}'))
        elif zone_id in zone_sensor and zone_sensor[zone_id] != sensor:
            issues.append(_issue('sensor_identity_conflict', zone_id, f'zone reported by {zone_sensor[zone_id]} and {sensor}'))
        sensor_zone.setdefault(sensor, zone_id)
        zone_sensor.setdefault(zone_id, sensor)
        at, error, wall = _parse_ts(reading.get('at'))
        if error:
            issues.append(_issue(error, f'{zone_id}:reading:{index}', 'reading timestamp requires an offset'))
            continue
        try:
            temp = _frac(reading.get('temp_c'))
        except ValueError:
            issues.append(_issue('reading_invalid', f'{zone_id}:reading:{index}', 'temp_c must be finite numeric'))
            continue
        parsed_readings.append({
            'zone': zone_id,
            'sensor': sensor,
            'at': reading.get('at'),
            'at_dt': at,
            'at_wall': wall,
            'temp': temp,
            'index': index,
        })

    # time zone misalignment: same wall-clock text with a different instant
    slot_starts = {slot['start_dt'] for slot in slots if slot['start_dt']}
    slot_walls = {slot['start_wall'] for slot in slots if slot['start_wall']}
    for reading in parsed_readings:
        if reading['at_dt'] in slot_starts:
            continue
        if reading['at_wall'] is not None and reading['at_wall'] in slot_walls:
            issues.append(_issue('timezone_misaligned', reading['zone'],
                                 f"reading {reading['at']} lines up on the wall clock but not on the actual instant"))
            continue
        local = reading['at_dt'].replace(tzinfo=None)
        for slot in slots:
            if slot['start_dt'] and local == slot['start_dt'].replace(tzinfo=None):
                issues.append(_issue('timezone_misaligned', reading['zone'],
                                     f"reading {reading['at']} does not align with slot {slot['id']} start"))
                break

    by_zone_initial = {}
    source_by_slot = {}
    readings_by_slot = {}
    first_dt = slots[0]['start_dt'] if slots else None
    slot_by_instant = {slot['start_dt']: slot for slot in slots if slot['start_dt']}
    for reading in parsed_readings:
        slot = slot_by_instant.get(reading['at_dt'])
        if slot is None:
            continue
        source_by_slot.setdefault(slot['id'], []).append({
            'zone': reading['zone'],
            'at': reading['at'],
            'temp_c': _num(reading['temp']),
            'sensor': reading['sensor'],
        })
        readings_by_slot.setdefault(slot['id'], {}).setdefault(reading['zone'], []).append(reading['temp'])
        if reading['at_dt'] == first_dt:
            by_zone_initial.setdefault(reading['zone'], []).append(reading['temp'])

    anchors = {}
    for slot_id, values in readings_by_slot.items():
        missing = sorted(set(zone_ids) - set(values))
        if missing:
            # a partially measured slot is a lost measurement, never zero
            issues.append(_issue('reading_missing', f'slot:{slot_id}',
                                 f"missing sensor readings for zones: {','.join(missing)}"))
            continue
        conflict = [zone_id for zone_id, temps in values.items() if len(set(temps)) != 1]
        if conflict:
            issues.append(_issue('sensor_identity_conflict', f'slot:{slot_id}',
                                 f"conflicting readings for zones: {','.join(sorted(conflict))}"))
            continue
        anchors[slot_id] = tuple(values[zone_id][0] for zone_id in zone_ids)

    initial = []
    for zone in zones:
        values = by_zone_initial.get(zone['id'], [])
        if not values:
            issues.append(_issue('reading_missing', zone['id'], 'no sensor reading at slot 0 start'))
            initial.append(None)
            continue
        if len(set(values)) != 1:
            issues.append(_issue('sensor_identity_conflict', zone['id'], 'conflicting initial temperatures'))
            initial.append(None)
            continue
        initial.append(values[0])

    model = {
        'zones': zones,
        'zone_ids': zone_ids,
        'slots': slots,
        'initial': tuple(initial),
        'anchors': anchors,
        'cooling': cooling,
        'capacity': effective_capacity,
        'declared_capacity': capacity,
        'budget': budget,
        'source_by_slot': source_by_slot,
        'parsed_readings': parsed_readings,
    }
    return model, issues


# ---------------------------------------------------------------------------
# exact joint dynamic programme
# ---------------------------------------------------------------------------

def _choices(zone_ids, capacity):
    ordered = sorted(zone_ids)
    choices = [()]
    for size in range(1, capacity + 1):
        choices.extend(itertools.combinations(ordered, size))
    return choices


def _entry_order(entry):
    cum, energy, actions = entry
    return (energy, max(cum) if cum else Fraction(0), cum, actions)


def _dominates(left, right):
    """True when left is componentwise no worse in energy and per-zone exposure."""
    left_cum, left_energy = left[0], left[1]
    right_cum, right_energy = right[0], right[1]
    return left_energy <= right_energy and all(a <= b for a, b in zip(left_cum, right_cum))


def _prune(entries):
    kept = []
    for entry in sorted(entries, key=_entry_order):
        if any(_dominates(other, entry) for other in kept):
            continue
        kept.append(entry)
    return kept


def _objective(entry, contract):
    cum, energy, actions = entry
    max_cum = max(cum) if cum else Fraction(0)
    if contract == 'min_energy':
        return (energy, max_cum, cum, actions)
    return (max_cum, energy, cum, actions)


def _solve(model, contract, locked_actions=None, forced_starts=None):
    """Exact joint DP over all slots and zones.

    ``locked_actions`` maps an already elapsed slot to the zones whose
    commands were physically executed.  ``forced_starts`` maps a slot to the
    measured start temperatures of every zone from late feedback.
    """
    zones = model['zones']
    slots = model['slots']
    zone_ids = model['zone_ids']
    cooling = model['cooling']
    budget = model['budget']
    locked_actions = locked_actions or {}
    forced_starts = forced_starts or {}
    free_choices = _choices(zone_ids, model['capacity'])

    start_key = tuple(model['initial'])
    states = {start_key: [(tuple(Fraction(0) for _ in zone_ids), 0, ())]}
    generated = 0
    pruned = 0
    anchors = model.get('anchors', {})

    for slot in slots:
        slot_id = slot['id']
        measured = None
        if slot_id in forced_starts:
            measured = tuple(forced_starts[slot_id])
        elif slot_id in anchors:
            measured = tuple(anchors[slot_id])
        if measured is not None:
            # A reading taken at this slot's start is the true end temperature
            # of the previous slot.  Correct that slot's exposure before the
            # measured state replaces the forecast; this is evidence repair,
            # not a rewrite of any executed command.
            merged = []
            for start_temps, entries in states.items():
                delta = []
                for index, zone in enumerate(zones):
                    predicted = start_temps[index]
                    actual = measured[index]
                    delta.append(
                        max(Fraction(0), actual - zone['max_c'])
                        - max(Fraction(0), predicted - zone['max_c'])
                    )
                for cum, energy, history in entries:
                    new_cum = tuple(a + b for a, b in zip(cum, delta))
                    if sum(new_cum, Fraction(0)) <= budget:
                        merged.append((new_cum, energy, history))
            states = {measured: _prune(merged)}
        choices = [locked_actions[slot_id]] if slot_id in locked_actions else free_choices
        next_states = {}
        for start_temps, entries in states.items():
            for cum, energy, history in entries:
                for choice in choices:
                    chosen = set(choice)
                    end_temps = []
                    exposure = []
                    for zone_id, start_temp, zone in zip(zone_ids, start_temps, zones):
                        cooled = start_temp - cooling if zone_id in chosen else start_temp
                        end = cooled + zone['heat'][slot_id]
                        end_temps.append(end)
                        exposure.append(max(Fraction(0), end - zone['max_c']))
                    new_cum = tuple(a + b for a, b in zip(cum, exposure))
                    if sum(new_cum, Fraction(0)) > budget:
                        continue
                    new_energy = energy + len(chosen) * slot['energy_cents']
                    entry = (new_cum, new_energy, history + (tuple(sorted(chosen)),))
                    generated += 1
                    next_states.setdefault(tuple(end_temps), []).append(entry)
        states = {}
        for key, entries in next_states.items():
            kept = _prune(entries)
            pruned += len(entries) - len(kept)
            states[key] = kept
        if not states:
            break

    optimality = {
        'method': 'exact_dynamic_programming',
        'exact': True,
        'scope': 'all slots and all zones jointly',
        'action_domain': 'every subset of zones up to the shared device capacity per slot',
        'state_key': 'zone start temperatures',
        'pruning': 'componentwise cumulative-exposure and energy dominance only; no non-dominated state is removed',
        'fixed_slots': sorted(locked_actions) if locked_actions else [],
        'measured_slots': sorted(set(anchors) | set(forced_starts)),
        'states_generated': generated,
        'states_pruned_dominated': pruned,
        'final_states': sum(len(entries) for entries in states.values()),
        'contract': contract,
    }
    if not states:
        return None, optimality

    candidates = [entry for entries in states.values() for entry in entries]
    best = min(candidates, key=lambda entry: _objective(entry, contract))
    optimality['goal_vector'] = _goal_vector(best, zone_ids)
    optimality['no_better'] = (
        'Every still-actionable admissible action for every slot was enumerated; states with '
        'identical zone temperatures were merged and only states componentwise no better in '
        'energy and per-zone exposure were discarded.  The returned plan is therefore a global '
        'lexicographic optimum for the frozen contract given the already executed commands and '
        'measured anchors, not a per-zone or greedy schedule.'
    )

    brute = _brute_force(model, contract, locked_actions, forced_starts)
    optimality['brute_force_checked'] = brute is not None
    if brute is not None:
        optimality['brute_force_goal_vector'] = _goal_vector(brute, zone_ids)
        optimality['brute_force_matches'] = _objective(brute, contract) == _objective(best, contract)
    return best, optimality


def _goal_vector(entry, zone_ids):
    cum, energy, _ = entry
    return {
        'energy_cents': energy,
        'max_zone_exposure': _num(max(cum) if cum else Fraction(0)),
        'total_exposure': _num(sum(cum, Fraction(0))),
        'cumulative_exposure': {zone_id: _num(value) for zone_id, value in zip(zone_ids, cum)},
    }


def _brute_force(model, contract, locked_actions, forced_starts):
    """Independent exhaustive check used as an optimality witness."""
    zones = model['zones']
    slots = model['slots']
    zone_ids = model['zone_ids']
    cooling = model['cooling']
    budget = model['budget']
    free_choices = _choices(zone_ids, model['capacity'])
    choices_per_slot = []
    total = 1
    for slot in slots:
        slot_id = slot['id']
        choices = [locked_actions[slot_id]] if slot_id in locked_actions else list(free_choices)
        choices_per_slot.append(choices)
        total *= len(choices)
    if total > _BRUTE_FORCE_LIMIT:
        return None
    anchors = model.get('anchors', {})
    best = None
    for combination in itertools.product(*choices_per_slot):
        temps = tuple(model['initial'])
        exposures = []
        energy = 0
        for index, (slot, choice) in enumerate(zip(slots, combination)):
            slot_id = slot['id']
            measured = None
            if slot_id in forced_starts:
                measured = tuple(forced_starts[slot_id])
            elif slot_id in anchors:
                measured = tuple(anchors[slot_id])
            if measured is not None:
                # the measured start is the previous slot's true end
                if index > 0:
                    exposures[index - 1] = [
                        max(Fraction(0), measured[zone_index] - zone['max_c'])
                        for zone_index, zone in enumerate(zones)
                    ]
                temps = measured
            chosen = set(choice)
            end_temps = []
            exposure = []
            for zone_id, start_temp, zone in zip(zone_ids, temps, zones):
                cooled = start_temp - cooling if zone_id in chosen else start_temp
                end = cooled + zone['heat'][slot_id]
                end_temps.append(end)
                exposure.append(max(Fraction(0), end - zone['max_c']))
            exposures.append(exposure)
            energy += len(chosen) * slot['energy_cents']
            temps = tuple(end_temps)
        cum = tuple(sum((row[zone_index] for row in exposures), Fraction(0))
                    for zone_index in range(len(zone_ids)))
        if sum(cum, Fraction(0)) > budget:
            continue
        entry = (cum, energy, combination)
        if best is None or _objective(entry, contract) < _objective(best, contract):
            best = entry
    return best


# ---------------------------------------------------------------------------
# plan construction and persistence
# ---------------------------------------------------------------------------

def _simulate(model, actions, forced_starts=None):
    """Replay a complete action assignment, returning per-slot evidence rows.

    Measured start temperatures are observed anchors.  A measurement taken at
    the start of slot ``s`` is the true end temperature of slot ``s-1``, so it
    repairs that already-elapsed slot's exposure and the running totals; it
    never changes the actions themselves.
    """
    zones = model['zones']
    zone_ids = model['zone_ids']
    forced_starts = forced_starts or {}
    anchors = model.get('anchors', {})
    temperatures = list(model['initial'])
    values = {}
    rows = []
    for slot in model['slots']:
        slot_id = slot['id']
        forced = slot_id in forced_starts
        anchored = not forced and slot_id in anchors
        measured = list(forced_starts[slot_id]) if forced else (list(anchors[slot_id]) if anchored else None)
        if measured is not None:
            temperatures = measured
        start_temps = list(temperatures)
        chosen = tuple(actions[slot_id])
        chosen_set = set(chosen)
        end_temps = []
        exposure = []
        for index, zone in enumerate(zones):
            cooled = start_temps[index] - model['cooling'] if zone['id'] in chosen_set else start_temps[index]
            end = cooled + zone['heat'][slot_id]
            end_temps.append(end)
            exposure.append(max(Fraction(0), end - zone['max_c']))
        values[slot_id] = {'exposure': exposure}
        rows.append({
            'slot': slot_id,
            'start': slot['start'],
            'energy_cents': slot['energy_cents'],
            'energy_used_cents': len(chosen) * slot['energy_cents'],
            'source_readings': model['source_by_slot'].get(slot_id, []),
            'source_basis': 'feedback' if forced else ('sensor' if (anchored or slot_id == 0) else 'forecast'),
            'start_temps': {zone_id: _num(value) for zone_id, value in zip(zone_ids, start_temps)},
            'end_temps': {zone_id: _num(value) for zone_id, value in zip(zone_ids, end_temps)},
            'action': {
                'type': 'start_cooling' if chosen else 'none',
                'zones': list(chosen),
                'zone': chosen[0] if len(chosen) == 1 else None,
                'cooling_c': _num(model['cooling']) if chosen else 0,
                'commands': [{'action': 'start_cooling', 'zone': zone_id, 'slot': slot_id} for zone_id in chosen],
            },
            'device': {
                'capacity': model['capacity'],
                'used': len(chosen),
                'occupied': 1 if chosen else 0,
                'zones': list(chosen),
            },
            'exposure': {},
            'slot_exposure': 0,
            'cumulative_exposure': {},
            'executed': False,
            'execution_id': None,
        })
        temperatures = end_temps
    # repair elapsed slots from later measurements, then roll exposure forward
    for index, row in enumerate(rows):
        if index == 0:
            continue
        if row['source_basis'] in ('sensor', 'feedback'):
            zone_values = row['start_temps']
            repaired = [
                max(Fraction(0), _frac(zone_values[zone_id]) - zone['max_c'])
                for zone_id, zone in zip(zone_ids, zones)
            ]
            previous = rows[index - 1]
            previous['measured_end_temps'] = dict(zone_values)
            previous['end_temps'] = dict(zone_values)
            values[previous['slot']] = {'exposure': repaired}
    cumulative = [Fraction(0) for _ in zone_ids]
    for row in rows:
        exposure = values[row['slot']]['exposure']
        cumulative = [a + b for a, b in zip(cumulative, exposure)]
        row['exposure'] = {zone_id: _num(value) for zone_id, value in zip(zone_ids, exposure)}
        row['slot_exposure'] = _num(sum(exposure, Fraction(0)))
        row['cumulative_exposure'] = {zone_id: _num(value) for zone_id, value in zip(zone_ids, cumulative)}
    return rows, cumulative


def _conservation(model, rows, cumulative):
    zone_ids = model['zone_ids']
    by_id = {zone['id']: zone for zone in model['zones']}
    energy = sum(row['energy_used_cents'] for row in rows)
    exposure_by_zone = {zone_id: Fraction(0) for zone_id in zone_ids}
    for row in rows:
        for zone_id in zone_ids:
            exposure_by_zone[zone_id] += _frac(row['exposure'][zone_id])
    temp_path_ok = True
    measured_ok = True
    for index, row in enumerate(rows):
        slot_id = row['slot']
        anchored_end = row.get('measured_end_temps') is not None
        for zone_id in zone_ids:
            heat = by_id[zone_id]['heat'][slot_id]
            start = _frac(row['start_temps'][zone_id])
            cooled = start - model['cooling'] if zone_id in row['action']['zones'] else start
            end = _frac(row['end_temps'][zone_id])
            if anchored_end:
                if end != _frac(row['measured_end_temps'][zone_id]):
                    measured_ok = False
            elif end != cooled + heat:
                temp_path_ok = False
            if _frac(row['exposure'][zone_id]) != max(Fraction(0), end - by_id[zone_id]['max_c']):
                measured_ok = False
        if index + 1 < len(rows):
            following = rows[index + 1]
            # a measured anchor legitimately replaces the forecast start; the
            # ended slot is reported with a measured end temperature instead
            if following.get('source_basis') in ('sensor', 'feedback'):
                if not anchored_end:
                    temp_path_ok = False
            else:
                for zone_id in zone_ids:
                    if _frac(following['start_temps'][zone_id]) != _frac(row['end_temps'][zone_id]):
                        temp_path_ok = False
    replay_total = sum(exposure_by_zone.values(), Fraction(0))
    return {
        'energy_cents': energy,
        'energy_equals_sum_of_slot_costs': energy == sum(row['energy_used_cents'] for row in rows),
        'exposure_sum_equals_final_cumulative': all(
            exposure_by_zone[zone_id] == cumulative[index]
            for index, zone_id in enumerate(zone_ids)
        ),
        'total_exposure': _num(replay_total),
        'total_exposure_matches_plan': replay_total == sum(cumulative, Fraction(0)),
        'temperature_path_consistent': temp_path_ok,
        'measured_endpoints_consistent': measured_ok,
        'forecast_slots': [row['slot'] for row in rows if row.get('source_basis') == 'forecast'],
        'measured_slots': [row['slot'] for row in rows if row.get('source_basis') in ('sensor', 'feedback')],
        'shared_device_respected': all(row['device']['used'] <= model['capacity'] for row in rows),
    }


def _placeholder_rows(model):
    """Rows for a blocked/partial case: no executable command is ever implied."""
    zone_ids = model['zone_ids']
    initial = model['initial']
    rows = []
    for slot in model['slots']:
        starts = {
            zone_id: _num(value)
            for zone_id, value in zip(zone_ids, initial)
            if value is not None
        }
        rows.append({
            'slot': slot['id'],
            'start': slot['start'],
            'energy_cents': slot['energy_cents'],
            'energy_used_cents': 0,
            'source_readings': model['source_by_slot'].get(slot['id'], []),
            'source_basis': 'sensor' if slot['id'] == 0 else 'forecast',
            'start_temps': starts,
            'end_temps': {},
            'action': {'type': 'blocked', 'zones': [], 'zone': None, 'cooling_c': 0, 'commands': []},
            'device': {'capacity': model['capacity'], 'used': 0, 'occupied': 0, 'zones': []},
            'exposure': {zone_id: 0 for zone_id in zone_ids},
            'slot_exposure': 0,
            'cumulative_exposure': {zone_id: 0 for zone_id in zone_ids},
            'executed': False,
            'execution_id': None,
        })
    return rows


def _input_digest(case_digest, contract, feedback_digest, executions_digest):
    return _sha(_canon({
        'case_digest': case_digest,
        'contract': contract,
        'feedback_digest': feedback_digest or '',
        'executions_digest': executions_digest or '',
    }))


def _feedback_digest(rows):
    canonical = sorted(
        ({'slot': row['slot'], 'zone': row['zone'], 'at': row['at'],
          'temp_c': _num(_frac(row['temp_c'])) if not isinstance(row['temp_c'], str) else row['temp_c'],
          'sensor': row.get('sensor') or ''} for row in rows),
        key=_canon,
    )
    return _sha(_canon(canonical))


def _executions_digest(db, case_id):
    rows = db.execute('SELECT slot,zone,command FROM climate_execution WHERE case_id=? ORDER BY slot,zone', (case_id,)).fetchall()
    return _sha(_canon([{'slot': row['slot'], 'zone': row['zone'], 'command': json.loads(row['command'])} for row in rows]))


def _feedback_rows(db, case_id):
    return [dict(row) for row in db.execute(
        'SELECT slot,zone,at,temp_c,sensor,role,source_sha256 FROM climate_feedback WHERE case_id=? ORDER BY slot,zone,at',
        (case_id,),
    )]


def _latest_plan(db, case_id, contract):
    return db.execute(
        'SELECT * FROM climate_plan WHERE case_id=? AND contract=? ORDER BY version DESC LIMIT 1',
        (case_id, contract),
    ).fetchone()


def _plan_version(db, case_id, contract):
    row = db.execute(
        'SELECT MAX(version) AS v FROM climate_plan WHERE case_id=? AND contract=?',
        (case_id, contract),
    ).fetchone()
    return (row['v'] or 0) + 1 if row else 1


def _store_plan(db, case_id, contract, report, source_digest, input_digest, version, supersedes=None):
    core = dict(report)
    for key in ('digest', 'plan_id', 'version', 'supersedes', 'created_at'):
        core.pop(key, None)
    digest = _sha(_canon(core))
    created_at = datetime.now(timezone.utc).isoformat()
    payload = dict(report)
    payload['digest'] = digest
    payload['version'] = version
    payload['supersedes'] = supersedes
    payload['created_at'] = created_at
    cursor = db.execute(
        'INSERT INTO climate_plan(case_id,contract,version,status,feasible,energy_cents,total_exposure,max_zone_exposure,input_digest,digest,source_sha256,supersedes,created_at,payload) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            case_id, contract, version, report['status'], 1 if report['feasible'] else 0,
            report.get('energy_cents'), report.get('total_exposure'), report.get('max_zone_exposure'),
            input_digest, digest, source_digest, supersedes, created_at, '{}',
        ),
    )
    plan_id = cursor.lastrowid
    payload['plan_id'] = plan_id
    db.execute('UPDATE climate_plan SET payload=? WHERE id=?', (_canon(payload), plan_id))
    for row in report['slots']:
        db.execute(
            'INSERT INTO climate_slot_plan(plan_id,slot,start,energy_cents,source_readings,start_temps,end_temps,exposure,cumulative_exposure,action_type,action_zones,cooling_c,device_occupied,device_used,source_basis,executed,execution_id) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                plan_id, row['slot'], row['start'], row['energy_cents'],
                _canon(row['source_readings']), _canon(row['start_temps']), _canon(row['end_temps']),
                _canon(row['exposure']), _canon(row['cumulative_exposure']), row['action']['type'],
                _canon(row['action']['zones']), row['action']['cooling_c'], row['device']['occupied'],
                row['device']['used'], row['source_basis'], 1 if row['executed'] else 0, row['execution_id'],
            ),
        )
    for issue in report.get('issues', []):
        db.execute(
            'INSERT INTO climate_issue(plan_id,code,subject,detail) VALUES (?,?,?,?)',
            (plan_id, issue['code'], issue['subject'], issue['detail']),
        )
    return plan_id, version, digest, payload


def _build_report(model, issues, contract, case_id, case_digest, sensor_sha,
                  locked_actions=None, forced_starts=None, feedback_digest=None,
                  execution_rows=None, base_status=None):
    locked_actions = locked_actions or {}
    forced_starts = forced_starts or {}
    execution_rows = execution_rows or {}
    budget = model['budget'] if model else None

    def empty(status, feasible, issue_list):
        return {
            'case_id': case_id,
            'contract': contract,
            'contract_label': CONTRACT_LABELS[contract],
            'status': status,
            'status_code': STATUS_CODES[status],
            'feasible': feasible,
            'energy_cents': None,
            'total_exposure': None,
            'max_zone_exposure': None,
            'zone_exposure': {},
            'total_exposure_budget': _num(budget) if budget is not None else None,
            'budget_ok': False,
            'slots': _placeholder_rows(model) if model else [],
            'commands': [],
            'optimality': {'method': 'not_run', 'exact': False, 'reason': status},
            'conservation': {},
            'issues': issue_list,
            'executed_actions': [],
            'source': {'case_digest': case_digest, 'sensor_sha256': sensor_sha,
                       'feedback_digest': feedback_digest or ''},
        }

    if issues:
        return empty(base_status or STATUS_REVIEW, False, issues)

    best, optimality = _solve(model, contract, locked_actions, forced_starts)
    if best is None:
        report = empty(STATUS_INFEASIBLE, False, [
            _issue('no_feasible_plan', 'shift',
                   'no action assignment keeps total exposure within the contract budget'),
        ])
        report['optimality'] = optimality
        report['total_exposure_budget'] = _num(budget) if budget is not None else None
        return report

    cumulative, energy, history = best
    actions = {slot['id']: history[index] for index, slot in enumerate(model['slots'])}
    rows, replayed = _simulate(model, actions, forced_starts)
    for row in rows:
        executed = sorted(
            (item for item in execution_rows.values() if item['slot'] == row['slot']),
            key=lambda item: item['zone'] or '',
        )
        if executed:
            row['executed'] = True
            row['execution_id'] = executed[0]['id']
    zone_ids = model['zone_ids']
    total_exposure = sum(cumulative, Fraction(0))
    commands = []
    for row in rows:
        if row['action']['type'] != 'start_cooling':
            continue
        already = {item['zone'] for item in execution_rows.values() if item['slot'] == row['slot']}
        for zone_id in row['action']['zones']:
            if zone_id in already:
                continue
            commands.append({'action': 'start_cooling', 'zone': zone_id, 'slot': row['slot'],
                             'case_id': case_id, 'contract': contract})
    return {
        'case_id': case_id,
        'contract': contract,
        'contract_label': CONTRACT_LABELS[contract],
        'status': STATUS_FEASIBLE,
        'status_code': STATUS_CODES[STATUS_FEASIBLE],
        'feasible': True,
        'energy_cents': energy,
        'total_exposure': _num(total_exposure),
        'max_zone_exposure': _num(max(cumulative) if cumulative else Fraction(0)),
        'zone_exposure': {zone_id: _num(value) for zone_id, value in zip(zone_ids, cumulative)},
        'total_exposure_budget': _num(budget),
        'budget_ok': total_exposure <= budget,
        'slots': rows,
        'commands': commands,
        'optimality': optimality,
        'conservation': _conservation(model, rows, replayed),
        'issues': [],
        'executed_actions': [
            {'slot': item['slot'], 'zone': item['zone'], 'command': json.loads(item['command'])}
            for item in sorted(execution_rows.values(), key=lambda x: (x['slot'], x['zone'] or ''))
        ],
        'source': {'case_digest': case_digest, 'sensor_sha256': sensor_sha,
                   'feedback_digest': feedback_digest or ''},
    }


def _execution_rows(db, case_id):
    return {
        row['id']: dict(row)
        for row in db.execute('SELECT * FROM climate_execution WHERE case_id=? ORDER BY slot,zone', (case_id,))
    }


def _locked_actions(executions):
    locked = {}
    for row in executions.values():
        zones = locked.setdefault(row['slot'], [])
        if row['zone']:
            zones.append(row['zone'])
    return {slot: tuple(sorted(zones)) for slot, zones in locked.items()}


def _compute_report(db, case_id, contract, case_payload, case_digest, model, issues,
                    executions, previous):
    """Build the plan for the current evidence, feedback and executions.

    Any stored late measurement at a slot start is treated as an anchor; an
    executed slot keeps its recorded action; a slot that has already elapsed
    (before the earliest measurement) cannot receive a new command.
    """
    all_feedback = _feedback_rows(db, case_id)
    feedback_digest = _feedback_digest(all_feedback)

    if model is None:
        report = _build_report(
            None, issues, contract, case_id, case_digest, case_payload['sensor_sha256'],
            feedback_digest=feedback_digest, execution_rows=executions,
        )
        return _annotate_execution(report, executions), feedback_digest

    stored_by_slot, feedback_conflicts = _stored_feedback_by_slot(db, case_id, model)
    issues = list(issues) + feedback_conflicts

    if issues:
        # A defective evidence base blocks every command, but an already
        # executed action must still be reported truthfully.
        report = _build_report(
            model, issues, contract, case_id, case_digest, case_payload['sensor_sha256'],
            feedback_digest=feedback_digest, execution_rows=executions,
        )
        return _annotate_execution(report, executions), feedback_digest

    forced_starts = {}
    for slot_id, values in stored_by_slot.items():
        missing = [zone_id for zone_id in model['zone_ids'] if zone_id not in values]
        if missing:
            issues.append(_issue('reading_missing', f'slot:{slot_id}',
                                 f"feedback missing zones: {','.join(missing)}"))
            continue
        forced_starts[slot_id] = tuple(values[zone_id] for zone_id in model['zone_ids'])

    earliest = min(stored_by_slot) if stored_by_slot else None
    locked = _locked_actions(executions) if executions else {}
    for slot in model['slots']:
        slot_id = slot['id']
        if slot_id in locked:
            continue
        if earliest is not None and slot_id < earliest:
            locked[slot_id] = ()

    committed = _committed_exposure(model, executions, stored_by_slot)
    unrecoverable = committed > model['budget']
    if unrecoverable:
        issues = issues + [_issue(
            'exposure_budget_exceeded_unrecoverable', 'shift',
            f'executed exposure {_num(committed)} exceeds budget {_num(model["budget"])}; '
            'future cooling cannot offset exposure already caused',
        )]
        report = _build_report(
            model, issues, contract, case_id, case_digest, case_payload['sensor_sha256'],
            feedback_digest=feedback_digest, execution_rows=executions,
            base_status=STATUS_UNRECOVERABLE,
        )
        report['status'] = STATUS_UNRECOVERABLE
        report['status_code'] = STATUS_CODES[STATUS_UNRECOVERABLE]
        report['commands'] = []
        report['committed_exposure'] = _num(committed)
    else:
        report = _build_report(
            model, issues, contract, case_id, case_digest, case_payload['sensor_sha256'],
            locked_actions=locked, forced_starts=forced_starts,
            feedback_digest=feedback_digest, execution_rows=executions,
        )
        report['committed_exposure'] = _num(committed)
        if previous is not None and report['status'] == STATUS_FEASIBLE:
            report['deviation'] = _deviation(previous, report, stored_by_slot, executions)
    report = _annotate_execution(report, executions)
    return report, feedback_digest


def plan(db_path, case_id, contract='min_energy'):
    """Compute and persist the exact plan for one frozen contract.

    Re-running with identical evidence, feedback and executions is stable:
    the existing plan row and payload are returned unchanged.
    """
    if contract not in CONTRACTS:
        raise ValueError('unknown_contract')
    db = connect(db_path)
    try:
        with db:
            case_payload, case_digest = _load_case(db, case_id)
            zone_table = {row['id'] for row in db.execute('SELECT id FROM zone')}
            model, issues = _validate_case(case_payload['case'], case_payload['readings'], zone_table)
            executions = _execution_rows(db, case_id)
            executions_digest = _executions_digest(db, case_id)
            feedback_digest = _feedback_digest(_feedback_rows(db, case_id))
            input_digest = _input_digest(case_digest, contract, feedback_digest, executions_digest)
            latest = _latest_plan(db, case_id, contract)
            if latest and latest['input_digest'] == input_digest:
                return json.loads(latest['payload'])

            previous = json.loads(latest['payload']) if latest else None
            report, feedback_digest = _compute_report(
                db, case_id, contract, case_payload, case_digest, model, issues, executions, previous,
            )
            report['source']['input_digest'] = input_digest
            version = _plan_version(db, case_id, contract)
            plan_id, version, digest, payload = _store_plan(
                db, case_id, contract, report, case_payload['sensor_sha256'], input_digest,
                version, latest['id'] if latest else None,
            )
            return payload
    finally:
        db.close()


def plan_all(db_path, case_id):
    return {contract: plan(db_path, case_id, contract) for contract in CONTRACTS}


# ---------------------------------------------------------------------------
# offline command execution
# ---------------------------------------------------------------------------

def execute(db_path, case_id, contract, plan_id=None, slots=None):
    """Publish executable commands for a feasible plan atomically.

    The whole request is validated before any write: if one requested slot is
    not executable, no partial command set is published.
    """
    db = connect(db_path)
    try:
        with db:
            if plan_id is None:
                row = _latest_plan(db, case_id, contract)
            else:
                row = db.execute('SELECT * FROM climate_plan WHERE id=? AND case_id=? AND contract=?',
                                 (plan_id, case_id, contract)).fetchone()
            if not row:
                raise ValueError('plan_missing')
            payload = json.loads(row['payload'])
            if payload.get('status') != STATUS_FEASIBLE:
                raise ValueError('plan_not_feasible')
            plan_rows = {item['slot']: item for item in payload['slots']}
            if slots is None:
                targets = sorted(slot for slot, item in plan_rows.items()
                                 if item['action']['type'] == 'start_cooling')
            else:
                targets = [int(item) for item in slots]
            requests = []
            for slot_id in targets:
                plan_row = plan_rows.get(slot_id)
                if plan_row is None:
                    raise ValueError(f'slot_missing:{slot_id}')
                if plan_row['action']['type'] != 'start_cooling':
                    raise ValueError(f'no_command:{slot_id}')
                if plan_row['device']['used'] > plan_row['device']['capacity']:
                    raise ValueError(f'device_overloaded:{slot_id}')
                existing = {
                    item['zone']: item for item in db.execute(
                        'SELECT * FROM climate_execution WHERE case_id=? AND slot=?', (case_id, slot_id)
                    )
                }
                planned = set(plan_row['action']['zones'])
                recorded = {zone for zone in existing if zone}
                if recorded and recorded != planned:
                    # the physical record and this plan version disagree; an
                    # executed command can never be replaced by a new one
                    raise ValueError(f'slot_execution_conflict:{slot_id}')
                for zone_id in plan_row['action']['zones']:
                    if zone_id not in existing:
                        requests.append((slot_id, zone_id))
            commands = []
            for slot_id, zone_id in requests:
                command = {'action': 'start_cooling', 'zone': zone_id, 'slot': slot_id,
                           'case_id': case_id, 'contract': contract, 'plan_id': row['id']}
                executed_at = datetime.now(timezone.utc).isoformat()
                cursor = db.execute(
                    'INSERT INTO climate_execution(case_id,plan_id,slot,zone,command,executed_at,source_sha256) VALUES (?,?,?,?,?,?,?)',
                    (case_id, row['id'], slot_id, zone_id, _canon(command), executed_at, row['digest']),
                )
                execution_id = cursor.lastrowid
                db.execute(
                    'UPDATE climate_slot_plan SET executed=1,execution_id=? WHERE plan_id=? AND slot=?',
                    (execution_id, row['id'], slot_id),
                )
                commands.append({'execution_id': execution_id, 'plan_id': row['id'], 'slot': slot_id,
                                 'zone': zone_id, 'command': command, 'executed_at': executed_at})
            return {'case_id': case_id, 'contract': contract, 'plan_id': row['id'], 'commands': commands}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# late feedback
# ---------------------------------------------------------------------------

def _read_feedback(path, model):
    raw = Path(path).read_bytes()
    try:
        readings = list(csv.DictReader(raw.decode('utf-8-sig').splitlines()))
    except (UnicodeDecodeError, csv.Error) as exc:
        return [], _sha(raw), [_issue('reading_invalid', str(path), str(exc))]
    issues = []
    zone_ids = set(model['zone_ids'])
    zone_sensor = {}
    sensor_zone = {}
    parsed = []
    slot_by_instant = {slot['start_dt']: slot for slot in model['slots'] if slot['start_dt']}
    slot_starts = set(slot_by_instant)
    slot_walls = {slot['start_wall'] for slot in model['slots'] if slot.get('start_wall')}
    seen = {}
    for index, reading in enumerate(readings):
        zone_id = str(reading.get('zone', '')).strip()
        sensor = str(reading.get('sensor', '')).strip()
        if zone_id not in zone_ids:
            issues.append(_issue('reading_invalid', f'reading:{index}', f'unknown zone {zone_id!r}'))
            continue
        if not sensor:
            issues.append(_issue('sensor_identity_conflict', zone_id, 'sensor id missing'))
            continue
        if sensor in sensor_zone and sensor_zone[sensor] != zone_id:
            issues.append(_issue('sensor_identity_conflict', sensor, f'sensor shared by {sensor_zone[sensor]} and {zone_id}'))
        elif zone_id in zone_sensor and zone_sensor[zone_id] != sensor:
            issues.append(_issue('sensor_identity_conflict', zone_id, f'zone reported by {zone_sensor[zone_id]} and {sensor}'))
        sensor_zone.setdefault(sensor, zone_id)
        zone_sensor.setdefault(zone_id, sensor)
        at, error, wall = _parse_ts(reading.get('at'))
        if error:
            issues.append(_issue(error, f'{zone_id}:reading:{index}', 'feedback timestamp requires an offset'))
            continue
        try:
            temp = _frac(reading.get('temp_c'))
        except ValueError:
            issues.append(_issue('reading_invalid', f'{zone_id}:reading:{index}', 'temp_c must be finite numeric'))
            continue
        slot = slot_by_instant.get(at)
        if slot is None:
            misaligned = (at.replace(tzinfo=None) in {value.replace(tzinfo=None) for value in slot_starts}
                          or (wall is not None and wall in slot_walls))
            code = 'timezone_misaligned' if misaligned else 'reading_invalid'
            issues.append(_issue(code, f'{zone_id}:reading:{index}',
                                 f'feedback at {reading.get("at")} does not match a slot start'))
            continue
        key = (slot['id'], zone_id)
        if key in seen and seen[key] != temp:
            issues.append(_issue('feedback_conflict', f'slot:{slot["id"]}:{zone_id}',
                                 'two different measured temperatures for the same slot and zone'))
        seen[key] = temp
        parsed.append({'slot': slot['id'], 'zone': zone_id, 'sensor': sensor,
                       'at': reading.get('at'), 'temp': temp, 'temp_c': _num(temp)})
    return parsed, _sha(raw), issues


def _committed_exposure(model, executions, feedback_by_slot):
    """Exposure already physically caused, using executed commands only.

    A measurement at the start of slot ``s+1`` is the true end temperature of
    slot ``s``, so it corrects the exposure of the slot it closes rather than
    only the slot it opens.  The elapsed horizon is the later of the last
    executed slot and the slot just before the latest measurement: exposure
    that has already physically happened cannot be undone by a future action.
    """
    if not executions and not feedback_by_slot:
        return Fraction(0)
    zone_ids = model['zone_ids']
    chosen_by_slot = {}
    for row in executions.values():
        if row['zone']:
            chosen_by_slot.setdefault(row['slot'], set()).add(row['zone'])
    horizons = []
    if executions:
        horizons.append(max(row['slot'] for row in executions.values()))
    if feedback_by_slot:
        horizons.append(max(feedback_by_slot) - 1)
    if not horizons:
        return Fraction(0)
    last = max(horizons)
    start_temps = list(model['initial'])
    cumulative = [Fraction(0) for _ in zone_ids]
    for slot in model['slots']:
        slot_id = slot['id']
        if slot_id > last:
            break
        chosen = chosen_by_slot.get(slot_id, set())
        end = []
        for index, zone in enumerate(model['zones']):
            cooled = start_temps[index] - model['cooling'] if zone['id'] in chosen else start_temps[index]
            end.append(cooled + zone['heat'][slot_id])
        measured_end = feedback_by_slot.get(slot_id + 1)
        if measured_end is not None and all(zone_id in measured_end for zone_id in zone_ids):
            reported_end = [measured_end[zone_id] for zone_id in zone_ids]
        else:
            reported_end = end
        for index, zone in enumerate(model['zones']):
            cumulative[index] += max(Fraction(0), reported_end[index] - zone['max_c'])
        start_temps = end
    return sum(cumulative, Fraction(0))


def _stored_feedback_by_slot(db, case_id, model):
    zone_ids = model['zone_ids']
    by_slot = {}
    conflict = []
    for row in _feedback_rows(db, case_id):
        by_slot.setdefault(row['slot'], {})
        current = by_slot[row['slot']].get(row['zone'])
        temp = _frac(row['temp_c'])
        if current is not None and current != temp:
            conflict.append((row['slot'], row['zone']))
        by_slot[row['slot']][row['zone']] = temp
    issues = [
        _issue('feedback_conflict', f'slot:{slot}:{zone}', 'two different measured temperatures for the same slot and zone')
        for slot, zone in sorted(set(conflict))
    ]
    return by_slot, issues


def apply_feedback(db_path, case_id, contract, feedback_file):
    """Record late measured temperatures and publish a corrected plan version.

    Executed commands are immutable.  The new version keeps every executed
    action, corrects the start temperature of the affected slot and all later
    slots, and re-solves only what is still actionable.
    """
    if contract not in CONTRACTS:
        raise ValueError('unknown_contract')
    db = connect(db_path)
    try:
        with db:
            case_payload, case_digest = _load_case(db, case_id)
            zone_table = {row['id'] for row in db.execute('SELECT id FROM zone')}
            model, issues = _validate_case(case_payload['case'], case_payload['readings'], zone_table)
            if model is None:
                raise ValueError('climate_case_invalid')
            parsed, feedback_sha, feedback_issues = _read_feedback(feedback_file, model)
            issues = list(issues) + feedback_issues
            latest = _latest_plan(db, case_id, contract)
            previous = json.loads(latest['payload']) if latest else None
            executions = _execution_rows(db, case_id)

            # store the new readings first (idempotent on slot/zone/at)
            for item in parsed:
                role = 'executed' if any(row['slot'] == item['slot'] for row in executions.values()) else 'pending'
                db.execute(
                    'INSERT OR IGNORE INTO climate_feedback(case_id,plan_id,slot,zone,at,temp_c,sensor,role,source_sha256,created_at) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (case_id, latest['id'] if latest else None, item['slot'], item['zone'], item['at'],
                     item['temp_c'], item['sensor'], role, feedback_sha,
                     datetime.now(timezone.utc).isoformat()),
                )
            all_feedback = _feedback_rows(db, case_id)
            combined_feedback_digest = _feedback_digest(all_feedback)
            executions_digest = _executions_digest(db, case_id)
            input_digest = _input_digest(case_digest, contract, combined_feedback_digest, executions_digest)
            if latest and latest['input_digest'] == input_digest:
                return json.loads(latest['payload'])

            report, _ = _compute_report(
                db, case_id, contract, case_payload, case_digest, model, issues, executions, previous,
            )
            report['source']['input_digest'] = input_digest
            version = _plan_version(db, case_id, contract)
            plan_id, version, digest, payload = _store_plan(
                db, case_id, contract, report, case_payload['sensor_sha256'], input_digest,
                version, latest['id'] if latest else None,
            )
            return payload
    finally:
        db.close()


def _annotate_execution(report, executions):
    executed_slots = {}
    for row in executions.values():
        executed_slots.setdefault(row['slot'], []).append(row)
    for slot in report.get('slots', []):
        rows = executed_slots.get(slot['slot'], [])
        if rows:
            slot['executed'] = True
            slot['execution_id'] = sorted(rows, key=lambda item: item['id'])[0]['id']
    report['executed_actions'] = [
        {'slot': row['slot'], 'zone': row['zone'], 'command': json.loads(row['command'])}
        for row in sorted(executions.values(), key=lambda item: (item['slot'], item['zone'] or ''))
    ]
    if report.get('status') == STATUS_FEASIBLE:
        report['commands'] = [
            command for command in report.get('commands', [])
            if not any(row['slot'] == command['slot'] and row['zone'] == command['zone']
                       for row in executions.values())
        ]
    return report


def _deviation(previous, current, feedback_by_slot, executions):
    old_rows = {row['slot']: row for row in previous.get('slots', [])}
    new_rows = {row['slot']: row for row in current.get('slots', [])}
    feedback_slot = min(feedback_by_slot) if feedback_by_slot else None
    deviation = {
        'feedback_slot': feedback_slot,
        'predicted_start_temps': {},
        'actual_start_temps': {},
        'delta_c': {},
        'executed_actions_unchanged': [
            {'slot': row['slot'], 'zone': row['zone']}
            for row in sorted(executions.values(), key=lambda item: (item['slot'], item['zone'] or ''))
        ],
        'previous_plan_version': previous.get('version'),
        'previous_total_exposure': previous.get('total_exposure'),
        'new_total_exposure': current.get('total_exposure'),
        'previous_energy_cents': previous.get('energy_cents'),
        'new_energy_cents': current.get('energy_cents'),
    }
    if feedback_slot is not None and feedback_slot in old_rows:
        predicted = old_rows[feedback_slot]['start_temps']
        actual = {zone_id: _num(_frac(value)) for zone_id, value in feedback_by_slot[feedback_slot].items()}
        deviation['predicted_start_temps'] = predicted
        deviation['actual_start_temps'] = actual
        deviation['delta_c'] = {
            zone_id: _num(_frac(actual[zone_id]) - _frac(predicted[zone_id]))
            for zone_id in actual if zone_id in predicted
        }
    start = feedback_slot if feedback_slot is not None else 0
    executed_slots = {row['slot'] for row in executions.values()}
    remaining = [slot_id for slot_id in sorted(new_rows)
                 if slot_id >= start and slot_id not in executed_slots]
    deviation['remaining_energy_cents'] = sum(
        new_rows[slot_id]['energy_used_cents'] for slot_id in remaining
    )
    deviation['remaining_actions'] = [
        {'slot': slot_id, 'zones': new_rows[slot_id]['action']['zones']}
        for slot_id in remaining
        if new_rows[slot_id]['action']['type'] == 'start_cooling'
    ]
    # "pending recompute": a slot whose future action changed versus the
    # previous plan version, or whose start temperature was corrected
    changed = []
    for slot_id in sorted(new_rows):
        if slot_id < start:
            continue
        old = old_rows.get(slot_id)
        if old is None:
            continue
        new_row = new_rows[slot_id]
        if executed_slots and slot_id in executed_slots:
            continue
        if (old['action']['zones'] != new_row['action']['zones']
                or old['start_temps'] != new_row['start_temps']):
            changed.append(slot_id)
    deviation['recalculated_slots'] = changed
    return deviation


# ---------------------------------------------------------------------------
# evidence report
# ---------------------------------------------------------------------------

def report(db_path, case_id, out):
    """Write a JSON (and Markdown) evidence report for a climate case."""
    db = connect(db_path)
    try:
        case = db.execute('SELECT * FROM climate_case WHERE id=?', (case_id,)).fetchone()
        if not case:
            raise ValueError('climate_case_missing')
        plans = []
        for row in db.execute('SELECT * FROM climate_plan WHERE case_id=? ORDER BY contract,version', (case_id,)):
            data = json.loads(row['payload'])
            data['issues'] = [dict(item) for item in db.execute(
                'SELECT code,subject,detail FROM climate_issue WHERE plan_id=? ORDER BY id', (row['id'],)
            )]
            plans.append(data)
        executions = [dict(row) for row in db.execute(
            'SELECT id,case_id,plan_id,slot,zone,command,executed_at,source_sha256 FROM climate_execution WHERE case_id=? ORDER BY slot,zone',
            (case_id,),
        )]
        for row in executions:
            row['command'] = json.loads(row['command'])
        feedback = [dict(row) for row in db.execute(
            'SELECT slot,zone,at,temp_c,sensor,role,source_sha256 FROM climate_feedback WHERE case_id=? ORDER BY slot,zone,at',
            (case_id,),
        )]
        data = {
            'case_id': case_id,
            'case_digest': case['digest'],
            'case': json.loads(case['payload']),
            'plans': plans,
            'executions': executions,
            'feedback': feedback,
        }
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        lines = ['# 展馆保存环境控制证据', '']
        for plan_report in plans:
            lines.append(f"- {plan_report['contract']} v{plan_report.get('version')}: {plan_report['status']}，"
                         f"电费 {plan_report.get('energy_cents')} 分，总暴露 {plan_report.get('total_exposure')}，"
                         f"最大分区暴露 {plan_report.get('max_zone_exposure')}")
            for slot in plan_report.get('slots', []):
                action = slot.get('action', {})
                lines.append(f"  - 时段 {slot['slot']} {slot.get('start')}: "
                             f"{action.get('type')} {action.get('zones')}；"
                             f"暴露 {slot.get('exposure')}；电费 {slot.get('energy_used_cents')} 分")
        if executions:
            lines.append('')
            lines.append('## 已执行命令')
            for row in executions:
                lines.append(f"- 时段 {row['slot']} 区 {row['zone']}: {row['command']}")
        path.with_suffix('.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return path
    finally:
        db.close()
