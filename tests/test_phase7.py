import os, tempfile
os.environ.setdefault('DB_BACKEND','sqlite')
os.environ.setdefault('SYNCRIA_DB',tempfile.mktemp(suffix='.db'))
from fastapi.testclient import TestClient
from app import main
from app.store import SQLiteStore
from app.finance import manual_baseline,savings_estimate,compensation_eur

def context(tmp_path,monkeypatch):
    monkeypatch.setenv('SYNCRIA_DB',str(tmp_path/'phase7.db')); monkeypatch.setenv('DB_BACKEND','sqlite'); monkeypatch.setenv('ENABLE_FORECAST','0'); monkeypatch.setenv('ENABLE_WEATHER','0'); monkeypatch.setenv('REQUIRE_CONTROLLER_LOGIN','1'); monkeypatch.delenv('ENABLE_LEGACY_FLOW',raising=False)
    for key in ('GEMINI_API_KEY','GROQ_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY'): monkeypatch.delenv(key,raising=False)
    store=SQLiteStore(); v,b,c=store.network_data()
    for name,value in [('store',store),('vehicles',v),('passengers',b),('crew',c)]: monkeypatch.setattr(main,name,value)
    client=TestClient(main.app)
    auth={'Authorization':'Bearer '+client.post('/api/login',json={'email':'controller@syncria.ai','password':'syncria123'}).json()['token']}
    return store,client,auth

def test_savings_and_compensation_known_values():
    assert manual_baseline(100,2)==18000
    assert savings_estimate(100,2,12400)['savings_usd']==5600
    assert compensation_eur(1500,3)==250
    assert compensation_eur(2000,2.99)==0
    assert compensation_eur(2000,0,True)==400
    assert compensation_eur(4000,3)==600
    assert compensation_eur(None,4) is None

def test_roi_audit_and_timeline_offline(tmp_path,monkeypatch):
    store,client,auth=context(tmp_path,monkeypatch)
    incident=client.post('/api/incidents',headers=auth,json={'event':{'type':'storm'}}).json()['incident_id']
    pending=client.get('/api/incidents/'+incident,headers=auth).json()
    assert all('regulatory_exposure' in o for o in pending['options'])
    result=client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'approve','option_id':'B'})
    assert result.status_code==200,result.text
    current=result.json(); assert current['savings']['savings_usd']==manual_baseline(current['impact']['pax_count'],current['event']['duration_h'])-next(o['cost_usd'] for o in current['options'] if o['option_id']=='B')
    roi=client.get('/api/roi',headers=auth).json(); assert roi['incident_count']==1 and roi['total_savings_usd']==current['savings']['savings_usd']
    report=client.get('/api/incidents/'+incident+'/audit',headers=auth).json()
    assert len(report['plans'])==3 and report['decisions'][0]['action']=='approve' and report['executions'] and report['notifications']
    agents={e['agent'] for e in report['timeline']}
    assert {'detector','impact','planner','conductor','executor','communicator'}<=agents
    assert all(e['at'] for e in report['timeline'])
    assert any(e['step']=='compensation' for e in report['timeline']) and any(e['step']=='savings' for e in report['timeline'])
    assert TestClient(main.app).get('/api/roi').status_code==401

def test_what_if_writes_no_database_rows(tmp_path,monkeypatch):
    store,client,auth=context(tmp_path,monkeypatch)
    incident=client.post('/api/incidents',headers=auth,json={'event':{'type':'storm'}}).json()['incident_id']
    state=client.get('/api/incidents/'+incident,headers=auth).json(); service=state['impact']['affected_vehicles'][0]
    def snapshot():
        with store.connect() as db:
            tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            return {t:[tuple(row) for row in db.execute('SELECT * FROM "'+t+'" ORDER BY rowid')] for t in tables}
    before=snapshot()
    response=client.post('/api/incidents/'+incident+'/what-if',headers=auth,json={'action':'cancel','vehicle_id':service})
    assert response.status_code==200,response.text
    assert response.json()['dry_run'] and response.json()['options'][1]['cancel_ids']==[service]
    assert client.post('/api/incidents/'+incident+'/what-if',headers=auth,json={'action':'delay','delay_hours':3}).status_code==200
    assert snapshot()==before

def test_crew_legality_warns_without_incident_and_projected():
    from datetime import datetime,timezone,timedelta
    from app.controller_tools import crew_monitor
    now=datetime(2026,1,1,10,tzinfo=timezone.utc)
    roster=[{'id':'C1','name':'A','role':'pilot','duty_start':(now-timedelta(hours=10.5)).isoformat(),'duty_limit_hours':12,'assignments':[{'vehicle_id':'V1','departure':(now+timedelta(minutes=10)).isoformat(),'arrival':(now+timedelta(hours=3)).isoformat()}]}, {'id':'C2','name':'B','role':'pilot','duty_start':now.isoformat(),'duty_limit_hours':12,'assignments':[]}]
    result=crew_monitor(roster,now)
    assert result[0]['remaining_h']==1.5 and result[0]['alert']=='Less than 2 hours remaining'
    assert result[0]['projected_violations'][0]['vehicle_id']=='V1' and result[1]['alert'] is None

def test_roster_and_handover_offline(tmp_path,monkeypatch):
    store,client,auth=context(tmp_path,monkeypatch)
    crew=client.get('/api/crew',headers=auth)
    assert crew.status_code==200 and len(crew.json()['crew'])==30
    incident=client.post('/api/incidents',headers=auth,json={'event':{'type':'storm'}}).json()['incident_id']
    response=client.post('/api/handover',headers=auth)
    assert response.status_code==200 and incident in response.json()['text'] and 'Pending approvals: 1' in response.json()['text']
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM agent_log WHERE step IN ('handover','legality')").fetchone()[0]==2

def test_audit_replan_timeline_and_roi_not_double_counted(tmp_path,monkeypatch):
    store,client,auth=context(tmp_path,monkeypatch)
    incident=client.post('/api/incidents',headers=auth,json={'event':{'type':'storm'}}).json()['incident_id']
    approved=client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'approve','option_id':'B'})
    assert approved.status_code==200,approved.text
    replanned=client.post('/api/incidents/'+incident+'/replan',headers=auth)
    assert replanned.status_code==200,replanned.text
    again=client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'approve','option_id':'A'})
    assert again.status_code==200,again.text
    audit=client.get('/api/incidents/'+incident+'/audit',headers=auth).json()
    assert {p['plan_version'] for p in audit['plans']}=={1,2}
    assert len(audit['decisions'])==2 and len(audit['executions'])==2
    assert any(e['step']=='re_planning' for e in audit['timeline'])
    roi=client.get('/api/roi',headers=auth).json()
    assert roi['incident_count']==1 and roi['total_savings_usd']==again.json()['savings']['savings_usd']

def test_incident_index_retains_completed_for_report(tmp_path,monkeypatch):
    _,client,auth=context(tmp_path,monkeypatch)
    incident=client.post('/api/incidents',headers=auth,json={'event':{'type':'storm'}}).json()['incident_id']
    client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'approve','option_id':'A'})
    index=client.get('/api/incidents',headers=auth).json()
    assert next(i for i in index if i['id']==incident)['status']=='done'
    assert client.get('/api/incidents/'+incident+'/audit',headers=auth).status_code==200
