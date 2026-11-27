"""Deterministic operational intelligence, independent of text generation."""
from datetime import datetime
from collections import Counter

def priority_score(booking, vehicles):
    score={'medic':100,'gold':80,'silver':60,'regular':40}.get(booking.get('tier'),40)
    source=vehicles.get(booking.get('vehicle_id')); connection=vehicles.get(booking.get('connecting_vehicle_id'))
    if source and connection and source.get('arrival') and connection.get('departure'):
        try:
            arrival=datetime.fromisoformat(source['arrival'].replace('Z','+00:00'))
            departure=datetime.fromisoformat(connection['departure'].replace('Z','+00:00'))
            minutes=(departure-arrival).total_seconds()/60
            if 0 <= minutes < 60: score+=20
        except (ValueError,TypeError): pass
    special=str(booking.get('special') or '').lower()
    if booking.get('family') or booking.get('elderly') or 'family' in special or 'elderly' in special: score+=10
    return score

def priority_order(bookings, vehicles):
    lookup={v['id']:v for v in vehicles} if isinstance(vehicles,list) else vehicles
    return sorted(bookings,key=lambda b:(-priority_score(b,lookup),b['booking_id']))

def priority_ranks(bookings,vehicles):
    ordered=priority_order(bookings,vehicles); total=len(ordered)
    return {b['booking_id']:{'rank':i+1,'total':total,'score':priority_score(b,{v['id']:v for v in vehicles})} for i,b in enumerate(ordered)}

def apply_learning(options, outcomes):
    logs=[]
    for o in options:
        rows=[r for r in outcomes if r['chosen_option']==o['option_id']]
        if len(rows)<3: continue
        successes=sum(bool(r['success']) for r in rows)
        bonus=round(8*successes/len(rows))
        o['confidence']=min(98,o['confidence']+bonus)
        o['learning']={'samples':len(rows),'successes':successes,'bonus':bonus,'definition':'all passengers needing a replacement seat were rebooked'}
        logs.append(f"Plan {o['option_id']}: {successes}/{len(rows)} past executions seated all displaced travelers (+{bonus} confidence)")
    return logs

def cluster_passengers(bookings, vehicles, affected_ids=None):
    """Deterministic K-means using one-hot route, departure hour, and tier score.

    An infeasible group remains unassigned; we never manufacture inventory.
    """
    import math
    if not bookings: return []
    lookup={v['id']:v for v in vehicles}; people=sorted(bookings,key=lambda b:b['booking_id'])
    routes=sorted({(lookup[b['vehicle_id']]['origin'],lookup[b['vehicle_id']]['destination']) for b in people})
    def vector(b):
        v=lookup[b['vehicle_id']]; hour=datetime.fromisoformat(v['departure'].replace('Z','+00:00')).hour
        return [4.0 if route==(v['origin'],v['destination']) else 0.0 for route in routes]+[hour/24,priority_score(b,lookup)/100]
    vectors=[vector(b) for b in people]; k=min(len(people),max(3,min(8,round(math.sqrt(len(people))))))
    # Deterministic farthest-point initialization avoids random run-to-run drift.
    centroids=[vectors[0]]
    def dist(a,b): return sum((x-y)**2 for x,y in zip(a,b))
    while len(centroids)<k:
        centroids.append(max(vectors,key=lambda v:min(dist(v,c) for c in centroids)))
    for _ in range(30):
        groups=[[] for _ in range(k)]
        for b,v in zip(people,vectors): groups[min(range(k),key=lambda i:(dist(v,centroids[i]),len(groups[i]),i))].append((b,v))
        new=[([sum(v[d] for _,v in g)/len(g) for d in range(len(vectors[0]))] if g else centroids[i]) for i,g in enumerate(groups)]
        if new==centroids: break
        centroids=new
    affected=set(affected_ids) if affected_ids is not None else {b['vehicle_id'] for b in people}; seats={v['id']:v['available_seats'] for v in vehicles}; result=[]
    # Reserve planned group inventory once for the whole set.
    groups=[g for g in groups if g]
    groups.sort(key=lambda g:(-max(priority_score(b,lookup) for b,_ in g),min(b['booking_id'] for b,_ in g)))
    for group in groups:
        members=[b for b,_ in group]; origins={lookup[b['vehicle_id']]['origin'] for b in members}; destinations={lookup[b['vehicle_id']]['destination'] for b in members}
        latest=max(lookup[b['vehicle_id']]['departure'] for b in members)
        candidates=sorted((v for v in vehicles if len(origins)==len(destinations)==1 and v['origin'] in origins and v['destination'] in destinations and v['id'] not in affected and v['status']=='scheduled' and v['departure']>latest and seats[v['id']]>=len(members)),key=lambda v:(v.get('arrival') or v['departure'],v['id']))
        target=candidates[0] if candidates else None
        if target: seats[target['id']]-=len(members)
        hours=[datetime.fromisoformat(lookup[b['vehicle_id']]['departure'].replace('Z','+00:00')).hour for b in members]
        result.append({'size':len(members),'destination':next(iter(destinations)) if len(destinations)==1 else 'mixed', 'origin':next(iter(origins)) if len(origins)==1 else 'mixed', 'time_window':f'{min(hours):02d}:00–{max(hours):02d}:59', 'booking_ids':[b['booking_id'] for b in members], 'vehicle_id':target['id'] if target else None,'departure':target['departure'] if target else None,'mode':target['mode'] if target else None})
    return result
