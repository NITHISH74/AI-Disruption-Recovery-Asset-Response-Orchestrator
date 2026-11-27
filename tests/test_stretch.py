import os
import time
from fastapi.testclient import TestClient
from app.main import app
from app.engine import seed, compute_cascade, generate_options
from app.store import SupabaseStore
from app.llm import get_llm

client=TestClient(app)

def test_storm_and_confidences_offline(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.delenv('GROQ_API_KEY', raising=False)
    monkeypatch.delenv('ENABLE_LEGACY_FLOW', raising=False)
    start=time.monotonic()
    a=client.post('/api/incidents',json={'event':{'type':'storm'}})
    assert a.status_code==202
    state=client.get('/api/incidents/'+a.json()['incident_id']).json()
    assert time.monotonic()-start < 3
    assert state['impact']['pax_count']>0 and len(state['options'])==3
    assert all(40<=o['confidence']<=98 for o in state['options'])
    assert client.post('/api/incidents/'+state['id']+'/decision',json={'action':'approve','option_id':'B'}).status_code==403
    old_impact=state['impact']; old_options=state['options']
    early=client.post('/api/incidents/'+state['id']+'/replan').json()
    assert early['impact']['pax_count']<old_impact['pax_count'] and early['options']!=old_options
    assert any(entry['step']=='re_planning' for entry in early['logs'])
    b=client.post('/api/incidents',json={'event':{'type':'storm'}})
    repeat=client.get('/api/incidents/'+b.json()['incident_id']).json()
    assert repeat['impact']==old_impact and repeat['options']==old_options
    assert get_llm('role','data','fallback') == 'fallback'

def test_signal_is_rail_and_plans_are_bounded():
    vehicles, bookings, crew = seed()
    impact=compute_cascade(vehicles,bookings,crew,{'type':'signal','duration_h':3})
    assert len(impact['affected_vehicles'])==6 and impact['pax_count']>0
    assert all(x.startswith('TR-') for x in impact['affected_vehicles'])
    options=generate_options(impact,{'type':'signal','duration_h':3},vehicles)
    assert len(options)==3 and all(40 <= p['confidence'] <= 98 for p in options)

def test_paginate_past_postgrest_cap():
    class Result:
        def __init__(self,data): self.data=data
    class Query:
        def __init__(self): self.offset=0; self.end=0
        def select(self,*a): return self
        def order(self,*a): return self
        def range(self,a,b): self.offset=a;self.end=b;return self
        def execute(self): return Result([{'id':str(i)} for i in range(self.offset,min(1500,self.end+1))])
    class Client:
        def table(self,name): return Query()
    rows=SupabaseStore(Client()).rows('bookings')
    assert len(rows)==1500 and rows[0]['id']=='0' and rows[-1]['id']=='1499'

def test_supabase_repository_crud_and_network_mapping(monkeypatch):
    from copy import deepcopy
    from app import main
    from datetime import datetime, timedelta, timezone
    vehicles, bookings, crew=seed()
    class Response:
        def __init__(self,data): self.data=data
    class Client:
        def __init__(self): self.tables={}
        def table(self,name): return Query(self,name)
    class Query:
        def __init__(self,client,name): self.client,self.name=client,name; self.mode='select'; self.filters=[]; self.lower=[]; self.lo=0; self.hi=999999
        def select(self,*args): return self
        def order(self,*args): return self
        def eq(self,k,v): self.filters.append((k,v)); return self
        def lt(self,k,v): self.lower.append((k,v)); return self
        def range(self,a,b): self.lo,self.hi=a,b; return self
        def limit(self,n): self.hi=n-1; return self
        def insert(self,data): self.mode='insert'; self.data=data; return self
        def upsert(self,data,**kwargs): self.mode='upsert';self.data=data;return self
        def update(self,data): self.mode='update';self.data=data;return self
        def execute(self):
            table=self.client.tables.setdefault(self.name,[])
            if self.mode=='select':
                return Response(deepcopy([r for r in table if all(r.get(k)==v for k,v in self.filters)][self.lo:self.hi+1]))
            if self.mode in ('insert','upsert'):
                rows=self.data if isinstance(self.data,list) else [self.data]
                table.extend(deepcopy(rows)); return Response(deepcopy(rows))
            for r in table:
                if all(r.get(k)==v for k,v in self.filters) and all(r.get(k,1)<v for k,v in self.lower): r.update(deepcopy(self.data))
            return Response([])
    c=Client()
    c.tables['vehicles']=[{'id':v['id'],'mode':v['mode'],'from_hub':v['origin']+('-R' if v['mode']=='train' else ''),'to_hub':v['destination']+('-R' if v['mode']=='train' else ''),'dep_at':v['departure'],'arr_at':v['arrival'],'capacity':v['capacity'],'available_seats':v['available_seats'],'status':v['status'],'asset_id':v['asset_id']} for v in vehicles]
    c.tables['passengers']=list({p['id']:{k:p[k] for k in ('id','name','tier','language')} for p in bookings}.values())
    c.tables['bookings']=[{'id':b['booking_id'],'passenger_id':b['id'],'vehicle_id':b['vehicle_id'],'pnr':b['pnr'],'tier':b['tier'],'fare':b['fare'],'connecting_vehicle_id':b['connecting_vehicle_id']} for b in bookings]
    c.tables['crew']=[{'id':str(n),'name':p['name'],'duty_start':(datetime.fromisoformat(vehicles[n]['departure'])-timedelta(hours=12-p['remaining_hours'])).isoformat(),'duty_limit_hours':12} for n,p in enumerate(crew)]
    c.tables['crew_assignments']=[{'crew_id':str(n),'vehicle_id':p['vehicle_id']} for n,p in enumerate(crew)]
    store=SupabaseStore(c); network=store.network_data()
    assert len(network[0])==60 and len(network[1])==1500 and len(network[2])==30
    monkeypatch.setattr(main,'store',store); monkeypatch.setattr(main,'vehicles',network[0]); monkeypatch.setattr(main,'passengers',network[1]); monkeypatch.setattr(main,'crew',network[2]); monkeypatch.delenv('ENABLE_LEGACY_FLOW',raising=False)
    response=client.post('/api/incidents',json={'event':{'type':'storm'}})
    assert response.status_code==202
    state=client.get('/api/incidents/'+response.json()['incident_id']).json()
    assert len(state['options'])==3 and state['impact']['pax_count']>0
    assert len(c.tables['events'])==1 and len(c.tables['impacts'])==1 and len(c.tables['recovery_plans'])==3
    assert len(c.tables['agent_log'])>=6 and len(state['logs'])>=6
