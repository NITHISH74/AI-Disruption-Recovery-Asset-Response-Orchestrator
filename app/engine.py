"""Pure deterministic simulation calculations; no network, clock or LLM calls."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import random
from . import text_templates as text
from .intelligence import priority_order


def seed():
    rng = random.Random(74)
    today = datetime.now(timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0)
    vehicles = []
    for i in range(60):
        flight = i < 40
        n = i if flight else i - 40
        origin, destination = [('BLR', 'DEL'), ('BLR', 'BOM'), ('DEL', 'BLR'), ('BOM', 'BLR')][n % 4]
        if not flight and n < 6:
            origin, destination = ('BLR','DEL') if n % 2 == 0 else ('DEL','BLR')
        dep = today + timedelta(minutes=n * 24)
        vehicles.append({'id': f'AK-{201+n}' if flight else f'TR-{1201+n}', 'mode': 'flight' if flight else 'train', 'origin': origin, 'destination': destination, 'departure': dep.isoformat(), 'arrival': (dep+timedelta(hours=2)).isoformat(), 'capacity': 180 if flight else 240, 'available_seats': 35 if flight else 60, 'status': 'scheduled', 'asset_id': ('RW-17R' if n < 6 else 'RW-35L') if flight else ('S-12' if n < 6 else 'TS-09')})
    names = ['Aarav Sharma', 'Priya Patel', 'Ananya Rao', 'Rohan Mehta', 'Kavya Nair', 'Arjun Singh', 'Meera Iyer', 'Ishaan Gupta', 'Sofia Torres', 'Neha Kapoor', 'Dev Kumar', 'Sara Khan']
    people = []
    for i in range(1100):
        people.append({'id': f'P{i+1:04}', 'name': names[i % len(names)], 'language': rng.choices(['en', 'hi', 'ta', 'es'], [70, 15, 10, 5])[0], 'tier': rng.choices(['regular', 'silver', 'gold', 'medic'], [90, 6, 3, 1])[0]})
    bookings = []
    for i in range(1500):
        person = people[i % 1100].copy()
        v = vehicles[i % 60]
        connecting = vehicles[(i+12)%60]['id'] if i % 7 == 0 else None
        person.update({'booking_id': f'BK{i+1:05}', 'vehicle_id': v['id'], 'pnr': f'SY{i+100000}', 'connection': bool(connecting), 'connecting_vehicle_id': connecting, 'fare': 200 + i % 250, 'rebooked_to': None})
        bookings.append(person)
    crew = [{'name': f'Capt. {name}', 'vehicle_id': f'AK-{201+i}', 'remaining_hours': 1 if i < 2 else 8} for i, name in enumerate(['Rao', 'Sharma', 'Singh', 'Iyer', 'Patel', 'Khan'])]
    for i in range(6,30):
        crew.append({'name': f'Crew {i+1}', 'vehicle_id': vehicles[i]['id'], 'remaining_hours': 8})
    return vehicles, bookings, crew


def compute_cascade(vehicles, passengers, crew, event):
    kind = event['type']
    if kind in ('storm', 'signal'):
        asset = event.get('asset_id', 'RW-17R' if kind == 'storm' else 'S-12')
        ids = [v['id'] for v in vehicles if v['asset_id'] == asset and v['mode'] == ('flight' if kind == 'storm' else 'train')]
        # Simulated feed can specify a window explicitly; without one retain the
        # reproducible demo cohort (the seeded services on the monitored asset).
        if event.get('window_start') and event.get('expected_end'):
            start, end = map(datetime.fromisoformat, (event['window_start'], event['expected_end']))
            ids = [v['id'] for v in vehicles if v['id'] in ids and start <= datetime.fromisoformat(v['departure'].replace('Z','+00:00')) <= end]
    elif kind == 'engine': ids = ['AK-201']
    else: ids = ['AK-207']
    # Previously canceled services/bookings are no longer part of the active
    # transport network, whether this is a re-plan or a later incident.
    active={v['id'] for v in vehicles if v.get('status') != 'canceled'}
    ids=[id for id in ids if id in active]
    if not event.get('replanning') and event.get('recovered_early'):
        ids=ids[:max(1,len(ids)//2)]
    passengers=[p for p in passengers if p.get('status') != 'canceled']
    affected = [p for p in passengers if p['vehicle_id'] in ids]
    by_id = {v['id']:v for v in vehicles}
    missed = 0
    for p in passengers:
        conn = p.get('connecting_vehicle_id')
        if not conn: continue
        connection_before_recovery = not event.get('expected_end') or (conn in by_id and datetime.fromisoformat(by_id[conn]['departure'].replace('Z','+00:00')) < datetime.fromisoformat(event['expected_end']))
        if conn in ids or (p['vehicle_id'] in ids and connection_before_recovery): missed += 1
    risks = []
    for c in crew:
        if c['vehicle_id'] not in ids: continue
        remaining = c.get('remaining_hours', 12)
        delay=event['duration_h']
        if event.get('expected_end'):
            delay=max(0,(datetime.fromisoformat(event['expected_end'])-datetime.fromisoformat(by_id[c['vehicle_id']]['departure'].replace('Z','+00:00'))).total_seconds()/3600)
        over = max(0, delay - remaining)
        if over > 0: risks.append({**c, 'crew': c['name'], 'vehicle': c['vehicle_id'], 'overage_h': round(over,2)})
    breakdown = Counter(p['tier'] for p in affected)
    priority = [p for p in affected if p['tier'] in ('gold','medic')]
    summary = text.IMPACT.format(services=len(ids),passengers=len(affected),connections=missed,crew=len(risks))
    return {'affected_vehicles': ids, 'pax_count': len(affected), 'pax_breakdown': {k:breakdown[k] for k in ('regular','silver','gold','medic')}, 'missed_connections':missed, 'connections':missed, 'crew_risks':risks, 'priority_pax':priority, 'passengers_top': sorted(affected, key=lambda p:(p['tier'] not in ('medic','gold'), p['tier'] != 'medic',p['name'],p['id']))[:10], 'passenger_ids':[p['id'] for p in affected], 'summary_md':summary, '_bookings':affected}


def confidence(crew_safe, rules_compliant, pax, cost, data_quality=False):
    return max(40, min(98, 100 - 25*int(not crew_safe) - 30*int(not rules_compliant) - 15*int(pax>400) - 10*int(cost>20000) - 10*int(data_quality)))


def generate_options(impact, event, vehicles, feedback=''):
    ids = impact['affected_vehicles']; pax = impact['pax_count']; risks = impact['crew_risks']; bookings = impact.get('_bookings', [])
    vehicle_by_id = {v['id']:v for v in vehicles}
    cancelled = sorted(ids, key=lambda x: vehicle_by_id[x]['departure'])[:max(1,len(ids)//2)] if ids else []
    for vehicle in ids:
        if vehicle.lower() in feedback.lower(): cancelled = [vehicle]; break
    affected_cancel = [b for b in bookings if b['vehicle_id'] in cancelled]
    seats = {v['id']:v['available_seats'] for v in vehicles}
    assigned = Counter(); unassigned = 0
    for booking in priority_order(affected_cancel,vehicle_by_id):
        original = vehicle_by_id[booking['vehicle_id']]
        candidates = sorted((v for v in vehicles if v['id'] not in ids and v['origin'] == original['origin'] and v['destination'] == original['destination'] and v['departure'] > original['departure'] and seats[v['id']] > 0), key=lambda v:(v['mode'] != original['mode'], v['departure'],v['id']))
        if not candidates: unassigned += 1; continue
        target=candidates[0]; seats[target['id']]-=1; assigned[target['id']]+=1
    crew_b = not any(r['vehicle'] not in cancelled for r in risks)
    rules_b = unassigned == 0
    weights={'regular':1,'silver':1.5,'gold':2,'medic':3}
    cost_a=round(sum(float(b.get('fare',200)) for b in bookings)*.005)
    cost_b=round(sum(25+float(b.get('fare',200))*.1*weights.get(b['tier'],1) for b in affected_cancel))
    cost_c=pax*(20 if event['type']=='signal' else 35)
    stage_delays={v:(.75 if event['type']=='signal' else (0 if idx < (len(ids)+1)//2 else 1)) for idx,v in enumerate(ids)}
    crew_c = not any(c['remaining_hours'] < stage_delays[c['vehicle_id']] for c in risks)
    def delay_label(v):
        if event.get('expected_end'):
            hours=max(0,(datetime.fromisoformat(event['expected_end'])-datetime.fromisoformat(vehicle_by_id[v]['departure'].replace('Z','+00:00'))).total_seconds()/3600)
            return f'{hours:g}'
        return f'{event["duration_h"]:g}'
    options=[
      {'option_id':'A','strategy':'Delay all services','subtitle':'MINIMIZE CANCELLATIONS','cost_usd':cost_a,'pax_affected':pax,'crew_safe':not risks,'rules_compliant':True,'explanation':text.PLAN_A, 'actions':[f'Delay {v} by {delay_label(v)} hours to recovery time' for v in ids], 'cancel_ids':[]},
      {'option_id':'B','strategy':'Cancel + rebook mix','subtitle':'BALANCED RECOVERY','cost_usd':cost_b,'pax_affected':pax,'crew_safe':crew_b,'rules_compliant':rules_b,'explanation':text.PLAN_B.format(cancelled=len(cancelled),assigned=sum(assigned.values()),unassigned=unassigned), 'actions':[f'Cancel {v} ({sum(b["vehicle_id"]==v for b in affected_cancel)} bookings)' for v in cancelled]+[f'Rebook {n} travelers on {v}' for v,n in sorted(assigned.items())]+([f'{unassigned} travelers need an alternative'] if unassigned else []), 'cancel_ids':cancelled},
      {'option_id':'C','strategy':'Alternate routing','subtitle':'KEEP THE NETWORK MOVING','cost_usd':cost_c,'pax_affected':pax,'crew_safe':crew_c,'rules_compliant':True,'explanation':text.PLAN_C, 'actions':[f'{"Reroute via alternate section (+45 min)" if event["type"]=="signal" else f"Stage on alternate route (+{stage_delays[v]:g}h)"} for {v}' for v in ids], 'cancel_ids':[]}]
    b_ops=round(25*len(affected_cancel))
    options[0]['cost_breakdown']={'compensation':0,'rebooking_ops':0,'delay_exposure':cost_a}
    options[1]['cost_breakdown']={'compensation':cost_b-b_ops,'rebooking_ops':b_ops,'delay_exposure':0}
    options[2]['cost_breakdown']={'compensation':0,'rebooking_ops':0,'delay_exposure':cost_c}
    for o in options:
        o['confidence']=confidence(o['crew_safe'],o['rules_compliant'],pax,o['cost_usd']); o['explanation_md']=o['explanation']
    return options


def score_options(raw, constraints=None): return raw

def cost_options(scored, fare_book=None): return scored
