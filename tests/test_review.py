import os,tempfile
os.environ.setdefault('DB_BACKEND','sqlite')
os.environ.setdefault('SYNCRIA_DB',tempfile.mktemp(suffix='.db'))
from fastapi.testclient import TestClient
from app import main
from app.engine import seed,compute_cascade
from app.store import SQLiteStore

def test_new_impact_ignores_previously_canceled_services_and_bookings():
    vehicles,bookings,crew=seed(); vehicles[0]['status']='canceled'
    canceled=next(b for b in bookings if b['vehicle_id']==vehicles[1]['id']); canceled['status']='canceled'
    impact=compute_cascade(vehicles,bookings,crew,{'type':'storm','asset_id':'RW-17R','duration_h':2})
    assert vehicles[0]['id'] not in impact['affected_vehicles']
    assert canceled['booking_id'] not in {b['booking_id'] for b in impact['_bookings']}
    assert all(b.get('status')!='canceled' for b in impact['_bookings'])

def test_latest_returns_newest_active_even_if_newer_incident_finished(tmp_path,monkeypatch):
    monkeypatch.setenv('SYNCRIA_DB',str(tmp_path/'review.db'));monkeypatch.setenv('DB_BACKEND','sqlite');monkeypatch.setenv('REQUIRE_CONTROLLER_LOGIN','0');monkeypatch.setenv('ENABLE_WEATHER','0');monkeypatch.delenv('ENABLE_LEGACY_FLOW',raising=False)
    for key in ('GEMINI_API_KEY','GROQ_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY'): monkeypatch.delenv(key,raising=False)
    store=SQLiteStore();v,b,c=store.network_data()
    for name,value in [('store',store),('vehicles',v),('passengers',b),('crew',c)]: monkeypatch.setattr(main,name,value)
    client=TestClient(main.app)
    first=client.post('/api/incidents',json={'event':{'type':'storm'}}).json()['incident_id']
    second=client.post('/api/incidents',json={'event':{'type':'signal'}}).json()['incident_id']
    token=client.post('/api/login',json={'email':'controller@syncria.ai','password':'syncria123'}).json()['token']
    assert client.post('/api/incidents/'+second+'/decision',headers={'Authorization':'Bearer '+token},json={'action':'approve','option_id':'A'}).status_code==200
    assert client.get('/api/incidents/latest').json()['id']==first

def test_schedule_reads_current_persisted_vehicle_status(tmp_path,monkeypatch):
    monkeypatch.setenv('SYNCRIA_DB',str(tmp_path/'schedule.db'));monkeypatch.setenv('DB_BACKEND','sqlite');monkeypatch.setenv('REQUIRE_CONTROLLER_LOGIN','0')
    store=SQLiteStore(); monkeypatch.setattr(main,'store',store)
    import json
    with store.connect() as db:
        row=db.execute('SELECT data FROM domain_vehicles ORDER BY id LIMIT 1').fetchone()
        vehicle=json.loads(row['data']);vehicle['status']='canceled'
        db.execute('UPDATE domain_vehicles SET data=? WHERE id=?',(json.dumps(vehicle),vehicle['id']))
    response=TestClient(main.app).get('/api/schedule')
    assert next(v for v in response.json()['vehicles'] if v['id']==vehicle['id'])['status']=='canceled'
