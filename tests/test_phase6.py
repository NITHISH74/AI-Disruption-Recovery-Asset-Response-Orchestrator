import json
from datetime import datetime,timedelta,timezone
from fastapi.testclient import TestClient
from app import main
from app.store import SQLiteStore
from app.weather import forecast_watches
from app.intelligence import priority_score

def context(tmp_path,monkeypatch):
    monkeypatch.setenv('SYNCRIA_DB',str(tmp_path/'six.db')); monkeypatch.setenv('DB_BACKEND','sqlite'); monkeypatch.setenv('ENABLE_FORECAST','0'); monkeypatch.setenv('ENABLE_WEATHER','0'); monkeypatch.setenv('REQUIRE_CONTROLLER_LOGIN','0'); monkeypatch.delenv('ENABLE_LEGACY_FLOW',raising=False)
    for k in ('GEMINI_API_KEY','GROQ_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY'): monkeypatch.delenv(k,raising=False)
    store=SQLiteStore(); v,b,c=store.network_data()
    for k,value in [('store',store),('vehicles',v),('passengers',b),('crew',c)]: monkeypatch.setattr(main,k,value)
    return store,TestClient(main.app)

def test_priority_scarce_seat_orders_medic_first(tmp_path,monkeypatch):
    store,_=context(tmp_path,monkeypatch); vs,bs,_=store.network_data()
    source=vs[0]; target=next(v for v in vs if v['origin']==source['origin'] and v['destination']==source['destination'] and v['departure']>source['departure'])
    booking1={'booking_id':'A-REG','id':'regular','vehicle_id':source['id'],'tier':'regular'}
    booking2={'booking_id':'Z-MED','id':'medic','vehicle_id':source['id'],'tier':'medic'}
    with store.connect() as db:
        db.execute('DELETE FROM domain_bookings')
        for b in (booking1,booking2): db.execute('INSERT INTO domain_bookings VALUES (?,?)',(b['booking_id'],json.dumps(b)))
        for v in vs:
            v['available_seats']=1 if v['id']==target['id'] else 0
            db.execute('UPDATE domain_vehicles SET data=? WHERE id=?',(json.dumps(v),v['id']))
    state={'id':'p-test','created_at':datetime.now(timezone.utc).isoformat(),'impact':{'affected_vehicles':[source['id']]},'event':{'type':'storm','asset_id':source['asset_id'],'duration_h':2,'expected_end':source['departure']}}
    result=store.execute_plan(state,{'option_id':'B','cancel_ids':[source['id']],'cost_usd':100})
    assert result['changes'][0]['passenger_id']=='medic' and result['changes'][0]['to_vehicle']==target['id']
    assert result['changes'][1]['to_vehicle'] is None
    assert store.outcomes('storm')[0]['actual_pax_moved']==1 and not store.outcomes('storm')[0]['success']
    arrivals={source['id']:source,'conn':{'id':'conn','departure':(datetime.fromisoformat(source['arrival'])+timedelta(minutes=30)).isoformat()}}
    assert priority_score({**booking1,'connecting_vehicle_id':'conn','elderly':True},arrivals)==70

def test_watch_forecast_and_advisory_only(tmp_path,monkeypatch):
    store,client=context(tmp_path,monkeypatch); at=datetime.now(timezone.utc)+timedelta(hours=3)
    payload={'hourly':{'time':[at.isoformat()],'weather_code':[95],'wind_speed_10m':[45]}}
    watches=forecast_watches('BLR',payload)
    monkeypatch.setattr(main,'poll_forecasts',lambda:watches)
    main.poll_watch_once()
    result=client.get('/api/watches').json()
    assert result[0]['reason']=='thunderstorm' and result[0]['hub']=='BLR'
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM incidents").fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM agent_log WHERE step='watch'").fetchone()[0]==1

def test_learning_from_three_recorded_outcomes(tmp_path,monkeypatch):
    store,client=context(tmp_path,monkeypatch)
    with store.connect() as db:
        for i in range(3): db.execute('INSERT INTO incident_outcomes VALUES (?,?,?,?,?,?,?,?,?)',(str(i),1,'storm','B',100,5,20,1,datetime.now(timezone.utc).isoformat()))
    id=client.post('/api/incidents',json={'event':{'type':'storm'}}).json()['incident_id']
    state=client.get('/api/incidents/'+id).json(); option=state['options'][1]
    assert option['learning']['samples']==3 and option['learning']['bonus']==8
    assert any(l['step']=='learning' for l in state['logs'])

def test_grouping_counts_and_whole_group_capacity():
    from app.intelligence import cluster_passengers
    base=datetime(2026,1,1,8,tzinfo=timezone.utc)
    def v(id,origin,dest,h,seats):
        return {'id':id,'origin':origin,'destination':dest,'departure':(base+timedelta(hours=h)).isoformat(),'arrival':(base+timedelta(hours=h+2)).isoformat(),'available_seats':seats,'status':'scheduled','mode':'train'}
    vehicles=[v('S','BLR','BOM',0,0),v('T','BLR','BOM',5,30)]
    bookings=[{'booking_id':f'B{i:02}','id':f'P{i}', 'vehicle_id':'S','tier':'regular'} for i in range(16)]
    groups=cluster_passengers(bookings,vehicles,['S'])
    assert len(groups)==4 and sum(g['size'] for g in groups)==16
    assert all(g['vehicle_id']=='T' for g in groups)
    assert sum(g['size'] for g in groups if g['vehicle_id']=='T')<=30
    vehicles[1]['available_seats']=1
    assert any(g['vehicle_id'] is None for g in cluster_passengers(bookings,vehicles,['S']))

def test_voice_intent_reads_current_booking_and_logs(tmp_path,monkeypatch):
    store,client=context(tmp_path,monkeypatch)
    id=client.post('/api/incidents',json={'event':{'type':'storm'}}).json()['incident_id']
    token=client.post('/api/login',json={'email':'controller@syncria.ai','password':'syncria123'}).json()['token']
    result=client.post('/api/incidents/'+id+'/decision',headers={'Authorization':'Bearer '+token},json={'action':'approve','option_id':'B'}).json()
    note=result['notifications'][0]
    booking=next(b for b in store.network_data()[1] if b['booking_id']==note['booking_id'])
    response=client.post('/api/incidents/'+id+'/voice',json={'passenger_id':note['passenger_id'],'booking_id':note['booking_id'],'text':'where is my flight'})
    assert response.status_code==200 and response.json()['intent']=='where' and booking['vehicle_id'] in response.json()['answer']
    other=client.post('/api/incidents/'+id+'/voice',json={'passenger_id':note['passenger_id'],'booking_id':note['booking_id'],'text':'help me please'})
    assert other.status_code==200 and other.json()['answer']
    assert any(l['step']=='passenger_assistant' for l in client.get('/api/incidents/'+id).json()['logs'])
