"""Illustrative deterministic estimates; not actual savings or legal advice.

Manual baseline assumptions in USD per affected booking: $60 extra compensation,
$25 per hour for the disruption duration plus two additional hours of manual
handling, and 10% extra cancellations at $200 per booking (= $20 per booking).
Costs are not currency-converted and exclude the separate EU-style euro estimate.
"""
from datetime import datetime
from math import radians, sin, cos, asin, sqrt

COORDS={'BLR':(13.1986,77.7066),'BOM':(19.0896,72.8656),'DEL':(28.5562,77.1)}

def manual_baseline(pax_count,duration_h):
    return round(max(0,pax_count)*(60+25*(max(0,duration_h)+2)+20))

def savings_estimate(pax_count,duration_h,chosen_cost):
    baseline=manual_baseline(pax_count,duration_h)
    return {'baseline_usd':baseline,'plan_cost_usd':chosen_cost,'savings_usd':baseline-chosen_cost,'currency':'USD','basis':'estimated, not actual'}

def compensation_eur(distance_km,delay_hours,canceled=False):
    """Illustrative EU261-like fixed bands, assuming scope/eligibility; not legal advice.

    Cancellation or >=3h arrival delay qualifies; no extraordinary-circumstance,
    rerouting, jurisdiction, notice, court, or passenger-rights assessment is made.
    Distance unknown returns None. Thresholds are simplified for demonstration.
    """
    if distance_km is None or distance_km<0: return None
    if not canceled and delay_hours<3: return 0
    return 250 if distance_km<=1500 else 400 if distance_km<=3500 else 600

def distance_km(vehicle):
    if vehicle.get('distance_km') is not None: return float(vehicle['distance_km'])
    a=COORDS.get(vehicle.get('origin')); b=COORDS.get(vehicle.get('destination'))
    if not a or not b: return None
    la1,lo1,la2,lo2=map(radians,(*a,*b)); h=sin((la2-la1)/2)**2+cos(la1)*cos(la2)*sin((lo2-lo1)/2)**2
    return 6371*2*asin(sqrt(min(1,h)))

def regulatory_exposure(bookings,vehicles,event,option):
    by_vehicle={v['id']:v for v in vehicles}; per_person={}; details={}; unknown=set()
    ordered=option.get('cancel_ids',[]); affected=option.get('_affected_vehicles',[])
    for b in bookings:
        v=by_vehicle.get(b.get('vehicle_id'))
        if not v or v.get('mode')!='flight': continue
        canceled=v['id'] in ordered
        if option['option_id']=='C':
            index=affected.index(v['id']) if v['id'] in affected else 0
            delay=0 if index<(len(affected)+1)//2 else 1
        else:
            try:
                end=datetime.fromisoformat(event['expected_end'].replace('Z','+00:00'))
                dep=datetime.fromisoformat(v['departure'].replace('Z','+00:00'))
                delay=max(0,(end-dep).total_seconds()/3600)
            except (KeyError,ValueError,TypeError): delay=max(0,event.get('duration_h',0))
        amount=compensation_eur(distance_km(v),delay,canceled)
        if amount is None: unknown.add(b['id']); continue
        if b['id'] not in per_person or amount>per_person[b['id']]:
            per_person[b['id']]=amount
            details[b['id']]={'passenger_id':b['id'],'booking_id':b['booking_id'],'estimated_eur':amount,'distance_km':round(distance_km(v),1),'assumed_delay_h':round(delay,2),'canceled':canceled}
    return {'total_eur':sum(per_person.values()),'passengers_considered':len(per_person),'eligible_count':sum(amount>0 for amount in per_person.values()),'unknown_distance_count':len(unknown),'passengers':list(details.values()),'basis':'illustrative EU-style exposure; jurisdiction and exclusions not assessed'}
