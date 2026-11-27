import os
os.environ.setdefault('DB_BACKEND','sqlite')
os.environ.setdefault('ENABLE_WEATHER','0')
from fastapi.testclient import TestClient
from app import main
from app.store import SQLiteStore
import pytest

@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.setenv('SYNCRIA_DB',str(tmp_path/'phase3.db'))
    monkeypatch.setenv('DB_BACKEND','sqlite')
    monkeypatch.delenv('ENABLE_LEGACY_FLOW',raising=False)
    monkeypatch.delenv('GEMINI_API_KEY',raising=False); monkeypatch.delenv('GROQ_API_KEY',raising=False)
    store=SQLiteStore(); v,b,c=store.network_data()
    monkeypatch.setattr(main,'store',store); monkeypatch.setattr(main,'vehicles',v); monkeypatch.setattr(main,'passengers',b); monkeypatch.setattr(main,'crew',c)
    return TestClient(main.app),store

def test_auth(setup):
    client,_=setup
    assert client.post('/api/login',json={'email':'controller@syncria.ai','password':'bad'}).status_code==401
    response=client.post('/api/login',json={'email':'controller@syncria.ai','password':'syncria123'})
    assert response.status_code==200
    assert client.get('/api/session',headers={'Authorization':'Bearer '+response.json()['token']}).status_code==200

def test_reject_approve_persists_and_notifies(setup):
    client,store=setup
    token=client.post('/api/login',json={'email':'controller@syncria.ai','password':'syncria123'}).json()['token']; auth={'Authorization':'Bearer '+token}
    incident=client.post('/api/incidents',json={'event':{'type':'storm'}}).json()['incident_id']
    before=client.get('/api/incidents/'+incident).json()
    assert before['status']=='awaiting_approval'
    assert store.get_notifications(incident)==[]
    rejected=client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'reject','feedback':'cancel AK-201'})
    assert rejected.status_code==200, rejected.text
    assert rejected.json()['status']=='awaiting_approval'
    assert rejected.json()['options'][1]['cancel_ids']==['AK-201']
    response=client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'approve','option_id':'B'})
    assert response.status_code==200,response.text
    state=response.json(); assert state['status']=='done'
    vs,bookings,_=store.network_data(); assert next(v for v in vs if v['id']=='AK-201')['status']=='canceled'
    changed=state['executed']['changes']; assert changed and any(c['to_vehicle'] for c in changed)
    assert all(next(b for b in bookings if b['booking_id']==c['booking_id'])['vehicle_id']==c['to_vehicle'] for c in changed if c['to_vehicle'])
    notes=store.get_notifications(incident); assert len(notes)==len(set(before['impact']['passenger_ids']))
    assert all(set(n['elements'])=={'what','why','done','choice','when'} and all(n['elements'].values()) for n in notes)
    assert client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'approve','option_id':'B'}).status_code==409

def test_route_gate_and_rejection_limit(setup,monkeypatch):
    client,_=setup; monkeypatch.setenv('REQUIRE_CONTROLLER_LOGIN','1')
    assert client.get('/api/schedule').status_code==401
    token=client.post('/api/login',json={'email':'controller@syncria.ai','password':'syncria123'}).json()['token']; auth={'Authorization':'Bearer '+token}
    assert client.get('/api/schedule',headers=auth).status_code==200
    incident=client.post('/api/incidents',headers=auth,json={'event':{'type':'storm'}}).json()['incident_id']
    for _ in range(2):
        assert client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'reject','feedback':'cancel AK-202'}).status_code==200
    response=client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'reject'})
    assert response.status_code==409
    assert client.get('/api/incidents/'+incident,headers=auth).json()['status']=='awaiting_approval'

def test_self_service_updates_capacity_and_logs(setup):
    client,store=setup
    token=client.post('/api/login',json={'email':'controller@syncria.ai','password':'syncria123'}).json()['token']; auth={'Authorization':'Bearer '+token}
    incident=client.post('/api/incidents',json={'event':{'type':'storm'}}).json()['incident_id']
    approved=client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'approve','option_id':'B'}).json()
    note=next(n for n in approved['notifications'] if n['booking_id'] and n['rebooked_to'])
    q={'passenger_id':note['passenger_id'],'booking_id':note['booking_id']}
    alternatives=client.get('/api/incidents/'+incident+'/alternatives',params=q).json()['alternatives']
    assert alternatives
    target=alternatives[0]
    r=client.post('/api/rebooking',json={'incident_id':incident,**q,'vehicle_id':target['id']})
    assert r.status_code==200,r.text
    vs,bs,_=store.network_data()
    assert next(b for b in bs if b['booking_id']==note['booking_id'])['vehicle_id']==target['id']
    assert next(v for v in vs if v['id']==target['id'])['available_seats']==target['available_seats']-1
    assert len([n for n in store.get_notifications(incident) if n['passenger_id']==note['passenger_id']])==2

def test_memory_restart_restores_pending_gate(setup,monkeypatch):
    from app.graph import build_graph
    client,store=setup
    auth={'Authorization':'Bearer '+client.post('/api/login',json={'email':'controller@syncria.ai','password':'syncria123'}).json()['token']}
    incident=client.post('/api/incidents',json={'event':{'type':'storm'}}).json()['incident_id']
    monkeypatch.setattr(main,'graph',build_graph(main.detect_step,main.impact_step,main.planning_step,main.execute_step,main.notify_step))
    response=client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'approve','option_id':'B'})
    assert response.status_code==200,response.text
    assert response.json()['status']=='done'

def test_execution_rechecks_seats(setup):
    import json
    client,store=setup
    auth={'Authorization':'Bearer '+client.post('/api/login',json={'email':'controller@syncria.ai','password':'syncria123'}).json()['token']}
    incident=client.post('/api/incidents',json={'event':{'type':'storm'}}).json()['incident_id']
    state=client.get('/api/incidents/'+incident).json()
    plan=next(o for o in state['options'] if o['option_id']=='B')
    allocation=next(a for a in plan['actions'] if a.startswith('Rebook '))
    target=allocation.split()[-1]
    with store.connect() as db:
        v=json.loads(db.execute('SELECT data FROM domain_vehicles WHERE id=?',(target,)).fetchone()['data'])
        v['available_seats']=0; db.execute('UPDATE domain_vehicles SET data=? WHERE id=?',(json.dumps(v),target))
    result=client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'approve','option_id':'B'})
    assert result.status_code==200,result.text
    assert all(c['to_vehicle']!=target for c in result.json()['executed']['changes'])

def test_sse_yields_agent_and_approval_events(setup):
    import asyncio
    client,_=setup
    incident=client.post('/api/incidents',json={'event':{'type':'storm'}}).json()['incident_id']
    async def collect():
        response=await main.stream(incident); events=[]
        async for chunk in response.body_iterator:
            events.append(chunk)
            if 'event: awaiting_approval' in chunk: break
        await response.body_iterator.aclose()
        return ''.join(events)
    payload=asyncio.run(collect())
    assert 'event: agent_start' in payload and 'event: agent_log' in payload
    assert 'event: awaiting_approval' in payload and 'event: state_update' in payload

@pytest.mark.parametrize('event,option',[('storm','A'),('signal','C'),('crew_noshow','B')])
def test_other_events_execute_without_invalid_work_orders(setup,event,option):
    client,store=setup
    auth={'Authorization':'Bearer '+client.post('/api/login',json={'email':'controller@syncria.ai','password':'syncria123'}).json()['token']}
    incident=client.post('/api/incidents',json={'event':{'type':event}}).json()['incident_id']
    response=client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'approve','option_id':option})
    assert response.status_code==200,response.text
    assert response.json()['status']=='done'
    if event=='crew_noshow': assert response.json()['work_orders']==[]

def test_greeting_generation_is_batched_and_placeholder_safe(monkeypatch):
    from app.notifications import greeting_templates, GREETINGS
    from app import llm
    calls=[]
    def fake(system,data,fallback):
        calls.append(data)
        return 'unsafe <script>' if 'Language: en.' in data else 'Hello {name}.'
    monkeypatch.setattr(llm,'get_llm',fake)
    result=greeting_templates(['en','en','es'])
    assert len(calls)==2 and result['en']==GREETINGS['en'] and result['es']=='Hello {name}.'

def test_postgres_checkpoint_falls_back_to_memory(monkeypatch):
    from app.graph import make_checkpointer
    from langgraph.checkpoint.memory import MemorySaver
    import psycopg
    monkeypatch.setenv('CHECKPOINTER','postgres')
    monkeypatch.setenv('DATABASE_URL','postgresql://test.invalid/db')
    def fail(*args,**kwargs): raise OSError('offline')
    monkeypatch.setattr(psycopg,'connect',fail)
    assert isinstance(make_checkpointer(),MemorySaver)
