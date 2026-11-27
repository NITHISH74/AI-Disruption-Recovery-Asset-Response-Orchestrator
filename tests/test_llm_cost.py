import os
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from app import main, llm
from app.store import SQLiteStore

@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_BACKEND','sqlite')
    monkeypatch.setenv('SYNCRIA_DB',str(tmp_path/'metered.db'))
    monkeypatch.setenv('ENABLE_WEATHER','0')
    monkeypatch.setenv('REQUIRE_CONTROLLER_LOGIN','0')
    monkeypatch.delenv('ENABLE_LEGACY_FLOW',raising=False)
    for key in ('GEMINI_API_KEY','GROQ_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY','LLM_MODEL','LLM_FALLBACK','LLM_MODEL_PRIORITY','LLM_MAX_CALLS','LLM_MAX_TOKENS'):
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setattr(llm,'_failures',0)
    store=SQLiteStore(); v,b,c=store.network_data()
    monkeypatch.setattr(main,'store',store); monkeypatch.setattr(main,'vehicles',v); monkeypatch.setattr(main,'passengers',b); monkeypatch.setattr(main,'crew',c)
    return TestClient(main.app),store


def test_same_incident_inputs_use_cache_on_second_run(context,monkeypatch):
    client,store=context
    incidents=[client.post('/api/incidents',json={'event':{'type':'storm'}}).json()['incident_id'] for _ in range(2)]
    calls=[]
    def provider(model,system,text):
        calls.append((model,system,text))
        return ('A: Delay.\nB: Rebook.\nC: Reroute.' if 'recovery planner' in system else 'Impact summary.',120,40)
    monkeypatch.setattr(llm,'_provider_request',provider)
    monkeypatch.setenv('GEMINI_API_KEY','test-only')
    main.enrich_text(incidents[0]); assert len(calls)==2
    main.enrich_text(incidents[1]); assert len(calls)==2
    with store.connect() as db:
        rows=db.execute('SELECT incident_id,agent,cache_hit,prompt_tokens,completion_tokens,cost_usd FROM llm_usage ORDER BY id').fetchall()
    assert [r['cache_hit'] for r in rows]==[0,0,1,1]
    assert [r['incident_id'] for r in rows]==[incidents[0],incidents[0],incidents[1],incidents[1]]
    assert [(r['agent'],r['prompt_tokens'],r['completion_tokens']) for r in rows[:2]]==[('impact',120,40),('planner',120,40)]
    assert all(r['cost_usd']==0 for r in rows)


def test_budget_guard_logs_and_flow_continues(context,monkeypatch):
    client,store=context
    incident=client.post('/api/incidents',json={'event':{'type':'storm'}}).json()['incident_id']
    monkeypatch.setenv('GEMINI_API_KEY','test-only'); monkeypatch.setenv('LLM_MAX_CALLS','0')
    calls=[]; monkeypatch.setattr(llm,'_provider_request',lambda *args: calls.append(args))
    assert llm.get_llm('role','inputs','TEMPLATE',incident_id=incident,agent='planner')=='TEMPLATE'
    assert not calls
    with store.connect() as db:
        row=db.execute('SELECT outcome,cache_hit FROM llm_usage WHERE incident_id=?',(incident,)).fetchone()
        warning=db.execute("SELECT message FROM agent_log WHERE incident_id=? AND step='budget'",(incident,)).fetchone()
    assert row['outcome']=='budget' and not row['cache_hit'] and 'budget' in warning['message'].lower()
    auth={'Authorization':'Bearer '+client.post('/api/login',json={'email':'controller@syncria.ai','password':'syncria123'}).json()['token']}
    # Do not launch an optional background text task while checking the core flow.
    monkeypatch.delenv('GEMINI_API_KEY')
    result=client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'approve','option_id':'B'})
    assert result.status_code==200 and result.json()['status']=='done'


def test_paid_price_and_token_accounting(context,monkeypatch):
    _,store=context
    monkeypatch.setenv('LLM_MODEL','gpt-4o-mini'); monkeypatch.setenv('LLM_FALLBACK','')
    monkeypatch.setenv('OPENAI_API_KEY','test-only')
    monkeypatch.setattr(llm,'_provider_request',lambda *_:('paid response',1000,500))
    assert llm.get_llm('role','data','fallback',incident_id='inc-cost',agent='impact')=='paid response'
    with store.connect() as db: row=db.execute('SELECT * FROM llm_usage WHERE incident_id=?',('inc-cost',)).fetchone()
    assert row['prompt_tokens']==1000 and row['completion_tokens']==500
    assert Decimal(str(row['cost_usd']))==Decimal('0.00045')


def test_expiry_failure_and_fail_closed(context,monkeypatch):
    _,store=context
    monkeypatch.setenv('GEMINI_API_KEY','test-only'); monkeypatch.setenv('LLM_FALLBACK','')
    count=[]; monkeypatch.setattr(llm,'_provider_request',lambda *_:(count.append(1) or ('fresh',10,5)))
    assert llm.get_llm('role','data','fallback',incident_id='inc-expiry',agent='planner')=='fresh'
    with store.connect() as db: db.execute("UPDATE llm_cache SET expires_at='2000-01-01T00:00:00+00:00'")
    assert llm.get_llm('role','data','fallback',incident_id='inc-expiry',agent='planner')=='fresh'
    assert len(count)==2
    monkeypatch.setattr(llm,'_accounting',lambda: (_ for _ in ()).throw(OSError('offline')))
    assert llm.get_llm('role','different','safe',incident_id='inc-expiry')=='safe' and len(count)==2


def test_failover_records_attempts_and_caches_fallback(context,monkeypatch):
    _,store=context
    monkeypatch.setenv('GEMINI_API_KEY','test-only'); monkeypatch.setenv('GROQ_API_KEY','test-only')
    calls=[]
    def provider(model,*args):
        calls.append(model)
        if model=='gemini-2.0-flash': raise TimeoutError('offline')
        return 'fallback provider',7,3
    monkeypatch.setattr(llm,'_provider_request',provider)
    assert llm.get_llm('role','slice','template',incident_id='inc-fail',agent='planner')=='fallback provider'
    assert calls==['gemini-2.0-flash','gemini-2.0-flash','llama-3.3-70b-versatile']
    assert llm.get_llm('role','slice','template',incident_id='inc-repeat',agent='planner')=='fallback provider'
    assert len(calls)==3
    with store.connect() as db: rows=db.execute('SELECT outcome,prompt_tokens,completion_tokens,cost_usd FROM llm_usage ORDER BY id').fetchall()
    assert [r['outcome'] for r in rows]==['unknown','unknown','live','cache']
    assert rows[0]['prompt_tokens'] is None and rows[0]['cost_usd'] is None
    assert (rows[2]['prompt_tokens'],rows[2]['completion_tokens'])==(7,3)


def test_priority_routing_and_token_budget(context,monkeypatch):
    _,store=context
    monkeypatch.setenv('LLM_MODEL_PRIORITY','claude-3-5-haiku-20241022')
    monkeypatch.setenv('ANTHROPIC_API_KEY','test-only'); monkeypatch.setenv('LLM_FALLBACK','')
    calls=[]; monkeypatch.setattr(llm,'_provider_request',lambda model,*args:(calls.append(model) or ('hello',10,5)))
    assert llm.get_llm('role','priority','template',incident_id='inc-priority',agent='communicator',priority=True)=='hello'
    assert calls==['claude-3-5-haiku-20241022']
    monkeypatch.setenv('LLM_MAX_TOKENS','5')
    assert llm.get_llm('role','another','template',incident_id='inc-small',agent='impact',priority=True)=='template'
    assert len(calls)==1
    with store.connect() as db: row=db.execute("SELECT outcome FROM llm_usage WHERE incident_id='inc-small'").fetchone()
    assert row['outcome']=='budget'


def test_cached_responses_singleflight_concurrently(context,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    _,store=context
    monkeypatch.setenv('GEMINI_API_KEY','test-only'); monkeypatch.setenv('LLM_FALLBACK','')
    calls=[]; monkeypatch.setattr(llm,'_provider_request',lambda *args:(calls.append(1) or ('shared',12,4)))
    with ThreadPoolExecutor(max_workers=8) as pool:
        result=list(pool.map(lambda n:llm.get_llm('same role','same input','fallback',incident_id=f'inc-{n}',agent='impact'), range(8)))
    assert result==['shared']*8 and len(calls)==1
    with store.connect() as db: hits=db.execute('SELECT count(*) FROM llm_usage WHERE cache_hit=1').fetchone()[0]
    assert hits==7


def test_paid_model_selection_not_shadowed_by_fallback_cache(context,monkeypatch):
    monkeypatch.setenv('LLM_MODEL','llama-3.3-70b-versatile'); monkeypatch.setenv('LLM_FALLBACK','')
    monkeypatch.setenv('GROQ_API_KEY','test-only')
    calls=[]
    def provider(model,*_): calls.append(model); return model,20,10
    monkeypatch.setattr(llm,'_provider_request',provider)
    assert llm.get_llm('role','data','template',incident_id='first')=='llama-3.3-70b-versatile'
    monkeypatch.setenv('LLM_MODEL','gpt-4o-mini'); monkeypatch.setenv('LLM_FALLBACK','llama-3.3-70b-versatile'); monkeypatch.setenv('OPENAI_API_KEY','test-only')
    assert llm.get_llm('role','data','template',incident_id='second')=='gpt-4o-mini'
    assert calls==['llama-3.3-70b-versatile','gpt-4o-mini']


def test_openai_provider_usage_parsing(context,monkeypatch):
    _,store=context
    monkeypatch.setenv('LLM_MODEL','gpt-4o-mini');monkeypatch.setenv('LLM_FALLBACK','');monkeypatch.setenv('OPENAI_API_KEY','test-only')
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'choices':[{'message':{'content':'sample'}}],'usage':{'prompt_tokens':1000,'completion_tokens':500}}
    monkeypatch.setattr(llm.httpx,'post',lambda *args,**kwargs:Response())
    assert llm.get_llm('role','data','fallback',incident_id='inc-parsed',agent='planner')=='sample'
    with store.connect() as db: row=db.execute('SELECT * FROM llm_usage WHERE incident_id=?',('inc-parsed',)).fetchone()
    assert (row['prompt_tokens'],row['completion_tokens'],Decimal(str(row['cost_usd'])))==(1000,500,Decimal('0.00045'))


def test_live_attempt_cap_does_not_block_cached_text(context,monkeypatch):
    _,store=context
    monkeypatch.setenv('GEMINI_API_KEY','test-only');monkeypatch.setenv('LLM_FALLBACK','');monkeypatch.setenv('LLM_MAX_CALLS','1')
    calls=[];monkeypatch.setattr(llm,'_provider_request',lambda *args:(calls.append(1) or ('one',100,20)))
    assert llm.get_llm('system','one','fallback',incident_id='cap')=='one'
    assert llm.get_llm('system','one','fallback',incident_id='cap')=='one'
    assert llm.get_llm('system','different','fallback',incident_id='cap')=='fallback'
    with store.connect() as db: outcomes=[r[0] for r in db.execute('SELECT outcome FROM llm_usage WHERE incident_id=? ORDER BY id',('cap',))]
    assert outcomes==['live','cache','budget'] and len(calls)==1


def test_provider_without_usage_does_not_claim_free(context,monkeypatch):
    _,store=context
    monkeypatch.setenv('LLM_MODEL','gpt-4o-mini');monkeypatch.setenv('LLM_FALLBACK','');monkeypatch.setenv('OPENAI_API_KEY','test-only')
    monkeypatch.setattr(llm,'_provider_request',lambda *_:('response',None,None))
    assert llm.get_llm('role','data','fallback',incident_id='unknown')=='response'
    with store.connect() as db: row=db.execute('SELECT * FROM llm_usage WHERE incident_id=?',('unknown',)).fetchone()
    assert row['outcome']=='unknown' and row['cost_usd'] is None and row['reserved_tokens']>0


def test_cache_hit_available_without_keys_even_with_open_circuit(context,monkeypatch):
    _,store=context
    monkeypatch.setenv('GEMINI_API_KEY','test-only');monkeypatch.setenv('LLM_FALLBACK','')
    calls=[];monkeypatch.setattr(llm,'_provider_request',lambda *_:(calls.append(1) or ('cached text',10,3)))
    assert llm.get_llm('system','same','fallback',incident_id='first')=='cached text'
    monkeypatch.delenv('GEMINI_API_KEY');monkeypatch.setattr(llm,'_failures',3)
    assert llm.get_llm('system','same','fallback',incident_id='second')=='cached text'
    assert len(calls)==1
    with store.connect() as db: assert db.execute("SELECT cache_hit FROM llm_usage WHERE incident_id='second'").fetchone()[0]==1


def test_notification_templates_share_cache_across_incidents(context,monkeypatch):
    from app.notifications import greeting_templates
    _,store=context
    monkeypatch.setenv('GEMINI_API_KEY','test-only');monkeypatch.setenv('LLM_FALLBACK','')
    calls=[];monkeypatch.setattr(llm,'_provider_request',lambda *args:(calls.append(args) or ('Hello {name}.',8,3)))
    assert greeting_templates(['en','hi'],incident_id='first')==greeting_templates(['en','hi'],incident_id='second')
    assert len(calls)==2
    with store.connect() as db:
        rows=db.execute("SELECT cache_hit FROM llm_usage WHERE incident_id='second' AND agent='communicator'").fetchall()
    assert len(rows)==2 and all(r[0]==1 for r in rows)
