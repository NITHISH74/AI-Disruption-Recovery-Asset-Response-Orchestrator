import json
import os
import tempfile
import pytest
os.environ.setdefault('DB_BACKEND','sqlite')
os.environ.setdefault('SYNCRIA_DB',tempfile.mktemp(suffix='.db'))
from fastapi.testclient import TestClient
from app import main, llm
from app.agents import detector_agent,impact_agent,planner_agent
from app.store import SQLiteStore

@pytest.fixture
def environment(tmp_path,monkeypatch):
    monkeypatch.setenv('DB_BACKEND','sqlite'); monkeypatch.setenv('SYNCRIA_DB',str(tmp_path/'agents.db'))
    monkeypatch.setenv('ENABLE_WEATHER','0'); monkeypatch.setenv('ENABLE_FORECAST','0'); monkeypatch.setenv('REQUIRE_CONTROLLER_LOGIN','1')
    monkeypatch.setenv('GEMINI_API_KEY','test-only'); monkeypatch.setenv('LLM_FALLBACK','')
    for key in ('GROQ_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY','LLM_MODEL','LLM_MAX_CALLS','LLM_MAX_TOKENS','ENABLE_LEGACY_FLOW'):
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setattr(llm,'_failures',0)
    store=SQLiteStore(); v,b,c=store.network_data()
    for name,value in [('store',store),('vehicles',v),('passengers',b),('crew',c)]: monkeypatch.setattr(main,name,value)
    return store

def detector_input(): return {'event_type':'storm','severity':'high','asset':'RW-17R','asset_exists':True,'duration_h':2,'services_in_window':3}
def impact_input(): return {'ground_truth':{'affected_vehicles':['AK-201'],'pax_at_risk':2,'missed_connections':1,'crew_risks':['AK-201'],'priority_pax':['BK001']}}
def planner_input(): return {'options':[{'option_id':'A','crew_safe':True,'rules_compliant':True,'confidence':80,'cost_usd':50,'pax_affected':2},{'option_id':'B','crew_safe':False,'rules_compliant':True,'confidence':90,'cost_usd':40,'pax_affected':2}]}
def detector_answer(): return {'event_type':'storm','severity':'high','affected_asset':'RW-17R','confidence':0.8,'reasoning':'reported_disruption'}
def impact_answer(): return {'affected_vehicles':['AK-201'],'pax_at_risk':2,'missed_connections':1,'crew_risks':['AK-201'],'priority_pax':['BK001'],'cascade_narrative':'connections_and_crew'}
def planner_answer(): return {'recommended_option':'A','reasoning':'operational_resilience','trade_offs':['cost'],'risks':['delay'],'confidence_adjustment':3}

@pytest.mark.parametrize('agent,inputs,answer',[(detector_agent,detector_input,detector_answer),(impact_agent,impact_input,impact_answer),(planner_agent,planner_input,planner_answer)])
def test_structured_agents_reject_invalid_and_never_cache_it(environment,monkeypatch,agent,inputs,answer):
    monkeypatch.setattr(llm,'_provider_request',lambda *args:(json.dumps({'invalid':'untrusted'}),20,10))
    assert agent().run(inputs(),'INC-invalid') is None
    with environment.connect() as db:
        assert db.execute('SELECT count(*) FROM llm_cache').fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM llm_usage WHERE outcome='invalid'").fetchone()[0]==1

@pytest.mark.parametrize('agent,inputs,answer',[(detector_agent,detector_input,detector_answer),(impact_agent,impact_input,impact_answer),(planner_agent,planner_input,planner_answer)])
def test_agents_validate_and_use_metered_cache(environment,monkeypatch,agent,inputs,answer):
    calls=[]
    monkeypatch.setattr(llm,'_provider_request',lambda *args:(calls.append(args) or json.dumps(answer()),20,10))
    assert agent().run(inputs(),'INC-cache') is not None
    assert agent().run(inputs(),'INC-cache') is not None
    assert len(calls)==1
    with environment.connect() as db:
        assert db.execute("SELECT count(*) FROM llm_usage WHERE outcome='cache'").fetchone()[0]==1

@pytest.mark.parametrize('agent,inputs,answer,field,value',[
 (detector_agent,detector_input,detector_answer,'affected_asset','fake'),
 (impact_agent,impact_input,impact_answer,'pax_at_risk',999),
 (planner_agent,planner_input,planner_answer,'recommended_option','B'),
 (planner_agent,planner_input,planner_answer,'confidence_adjustment',11),
])
def test_rejects_ungrounded_or_unsafe_proposals(environment,monkeypatch,agent,inputs,answer,field,value):
    data=answer();data[field]=value
    monkeypatch.setattr(llm,'_provider_request',lambda *args:(json.dumps(data),20,10))
    assert agent().run(inputs(),'INC-unsafe') is None

def test_agents_share_validated_evidence_human_gate_and_fallback(environment,monkeypatch):
    observed=[]
    def provider(model,system,text):
        data=json.loads(text); observed.append((system,data))
        if 'disruption detector' in system:
            result={'event_type':data['event_type'],'severity':data['severity'],'affected_asset':data['asset'],'confidence':0.8,'reasoning':'reported_disruption'}
        elif 'impact analyst' in system:
            g=data['ground_truth']; result={**g,'cascade_narrative':'passenger_disruption'}
        else:
            safe=[o for o in data['options'] if o['crew_safe'] and o['rules_compliant']]
            if not safe: return ('{}',20,10)
            result={**planner_answer(),'recommended_option':safe[0]['option_id']}
        return (json.dumps(result),20,10)
    monkeypatch.setattr(llm,'_provider_request',provider)
    client=TestClient(main.app)
    auth={'Authorization':'Bearer '+client.post('/api/login',json={'email':'controller@syncria.ai','password':'syncria123'}).json()['token']}
    id=client.post('/api/incidents',headers=auth,json={'event':{'type':'storm'}}).json()['incident_id']
    state=client.get('/api/incidents/'+id,headers=auth).json()
    assert state['status']=='awaiting_approval' and not state['executed']
    assert state['agent_detector']['source']=='validated_llm'
    assert state['agent_impact']['source']=='validated_llm'
    assert state['agent_recommendation']['source']=='validated_llm'
    assert observed[1][1]['detector_assessment']['source']=='validated_llm'
    assert observed[2][1]['analyst_assessment']['source']=='validated_llm'
    with environment.connect() as db: assert db.execute('SELECT count(*) FROM execution_versions').fetchone()[0]==0
    answer=client.post('/api/incidents/'+id+'/decision',headers=auth,json={'action':'approve','option_id':'A'})
    assert answer.status_code==200 and answer.json()['status']=='done'
    # Failure of both providers does not stop the next complete flow.
    monkeypatch.setattr(llm,'_provider_request',lambda *args: (_ for _ in ()).throw(RuntimeError('offline')))
    llm._failures=0
    id2=client.post('/api/incidents',headers=auth,json={'event':{'type':'signal'}}).json()['incident_id']
    pending=client.get('/api/incidents/'+id2,headers=auth).json()
    assert pending['status']=='awaiting_approval' and pending['agent_recommendation']['source']=='deterministic_fallback'
    assert client.post('/api/incidents/'+id2+'/decision',headers=auth,json={'action':'approve','option_id':'A'}).json()['status']=='done'

def test_budget_capped_at_twelve_and_paid_models_never_called(environment,monkeypatch):
    monkeypatch.setenv('LLM_MAX_CALLS','99'); assert llm._limits()[0]==12
    monkeypatch.setenv('LLM_MODEL','gpt-4o-mini'); monkeypatch.setenv('OPENAI_API_KEY','test-only')
    calls=[];monkeypatch.setattr(llm,'_provider_request',lambda *args: calls.append(args))
    assert detector_agent().run(detector_input(),'INC-paid') is None
    assert calls==[]

def test_agent_limit_enforced_across_unique_inputs(environment,monkeypatch):
    monkeypatch.setenv('LLM_MAX_CALLS','99')
    calls=[]
    monkeypatch.setattr(llm,'_provider_request',lambda *args:(calls.append(args) or json.dumps(detector_answer()),20,10))
    for n in range(13):
        data=detector_input(); data['services_in_window']=n+1
        result=detector_agent().run(data,'INC-limit')
        assert (result is not None)==(n<12)
    assert len(calls)==12
    with environment.connect() as db:
        assert db.execute("SELECT count(*) FROM llm_usage WHERE incident_id='INC-limit' AND outcome='live'").fetchone()[0]==12
