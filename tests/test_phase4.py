import os, json, asyncio, tempfile
os.environ.setdefault("DB_BACKEND","sqlite")
os.environ.setdefault("SYNCRIA_DB",tempfile.mktemp(suffix=".db"))
from datetime import datetime, timedelta
import pytest
from fastapi.testclient import TestClient
from app import main
from app.store import SQLiteStore

@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.setenv('SYNCRIA_DB',str(tmp_path/'phase4.db')); monkeypatch.setenv('DB_BACKEND','sqlite')
    monkeypatch.setenv('ENABLE_WEATHER','0'); monkeypatch.setenv('REQUIRE_CONTROLLER_LOGIN','1')
    for k in ('ENABLE_LEGACY_FLOW','GEMINI_API_KEY','GROQ_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY'): monkeypatch.delenv(k,raising=False)
    store=SQLiteStore(); v,b,c=store.network_data()
    for key,value in [('store',store),('vehicles',v),('passengers',b),('crew',c)]: monkeypatch.setattr(main,key,value)
    client=TestClient(main.app)
    token=client.post('/api/login',json={'email':'controller@syncria.ai','password':'syncria123'}).json()['token']
    return client,store,{'Authorization':'Bearer '+token}

def test_replan_same_thread_preserves_versions_and_pause(setup):
    client,store,auth=setup
    incident=client.post('/api/incidents',headers=auth,json={'event':{'type':'storm'}}).json()['incident_id']
    before=client.get('/api/incidents/'+incident,headers=auth).json()
    config=main.graph_config(incident); checkpoint=main.graph.get_state(config).config['configurable']['checkpoint_id']
    r=client.post('/api/incidents/'+incident+'/replan',headers=auth)
    assert r.status_code==200,r.text
    after=r.json()
    assert after['id']==incident and after['status']=='awaiting_approval' and after['plan_version']==2
    assert after['impact']['pax_count']<before['impact']['pax_count']
    assert after['options']!=before['options'] and not after['executed']
    assert datetime.fromisoformat(after['event']['expected_end'])==datetime.fromisoformat(before['event']['expected_end'])-timedelta(hours=1)
    graph_state=main.graph.get_state(config)
    assert graph_state.next==('approve',) and graph_state.config['configurable']['checkpoint_id']!=checkpoint
    with store.connect() as db:
        rows=db.execute('SELECT plan_version,superseded FROM recovery_plan_versions WHERE incident_id=? ORDER BY plan_version,option_id',(incident,)).fetchall()
        asset=db.execute('SELECT expected_repair_end FROM domain_assets WHERE id=?',(after['event']['asset_id'],)).fetchone()
        events=db.execute('SELECT count(*) FROM events WHERE incident_id=?',(incident,)).fetchone()[0]
    assert [(r['plan_version'],r['superseded']) for r in rows]==[(1,1)]*3+[(2,0)]*3
    assert asset['expected_repair_end']==after['event']['expected_end'] and events==2
    assert client.post('/api/incidents/'+incident+'/replan',headers=auth).status_code==409
    async def read():
        response=await main.stream(incident); chunks=[]
        async for chunk in response.body_iterator:
            chunks.append(chunk)
            if 'event: awaiting_approval' in chunk: break
        await response.body_iterator.aclose(); return ''.join(chunks)
    events=asyncio.run(read())
    assert events.index('event: re_planning') < events.index('event: state_update') < events.index('event: awaiting_approval')

def test_replan_after_approval_is_a_new_approval_not_replay(setup):
    client,store,auth=setup
    incident=client.post('/api/incidents',headers=auth,json={'event':{'type':'storm'}}).json()['incident_id']
    first=client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'approve','option_id':'B'}).json()
    before_v,before_b,_=store.network_data()
    response=client.post('/api/incidents/'+incident+'/replan',headers=auth)
    assert response.status_code==200,response.text
    after=response.json()
    assert after['status']=='awaiting_approval' and after['plan_version']==2
    assert store.network_data()[:2]==(before_v,before_b)
    with store.connect() as db: assert db.execute('SELECT count(*) FROM execution_versions WHERE incident_id=?',(incident,)).fetchone()[0]==1
    second=client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'approve','option_id':'A'})
    assert second.status_code==200,second.text
    with store.connect() as db: assert db.execute('SELECT count(*) FROM execution_versions WHERE incident_id=?',(incident,)).fetchone()[0]==2
    assert len(second.json()['notifications'])>=len(first['notifications'])

def test_work_order_status_persisted_and_logged(setup):
    client,store,auth=setup
    incident=client.post('/api/incidents',headers=auth,json={'event':{'type':'storm'}}).json()['incident_id']
    client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'approve','option_id':'B'})
    orders=client.get('/api/work_orders',headers=auth).json(); item=next(x for x in orders if x['incident_id']==incident)
    assert client.patch('/api/work_orders/'+item['id'],headers=auth,json={'status':'done'}).status_code==409
    assert client.patch('/api/work_orders/'+item['id'],headers=auth,json={'status':'in_progress'}).status_code==200
    assert client.patch('/api/work_orders/'+item['id'],headers=auth,json={'status':'done'}).status_code==200
    assert next(w for w in store.list_orders() if w['id']==item['id'])['status']=='done'
    logs=client.get('/api/incidents/'+incident,headers=auth).json()['logs']
    assert any(l['step']=='work_order' and 'completed' in l['message'] for l in logs)
