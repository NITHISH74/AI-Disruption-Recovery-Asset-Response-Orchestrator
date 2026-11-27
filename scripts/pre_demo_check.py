#!/usr/bin/env python3
"""Read-only live checks plus isolated offline cost/auth checks; never prints keys.

Exit 0 only when all checks pass, 1 on failure, 2 if checks are skipped.
Use --live deliberately: it makes real free-tier LLM calls and logs in.
"""
import argparse
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / '.env')

results = []
def report(name, status, detail=''):
    results.append(status)
    print(f'{status}: {name}' + (f' — {detail}' if detail else ''))

def check(name, fn):
    try:
        detail = fn()
        report(name, 'PASS', detail or '')
    except Exception as exc:
        # Avoid logging exceptions that could contain service URLs or credentials.
        report(name, 'FAIL', type(exc).__name__ + ': check configuration, connectivity, and provider quota')

def require(value):
    if not value: raise AssertionError('check failed')

def offline_checks(tmp):
    from app import llm
    with patch.dict(os.environ, {'DB_BACKEND':'sqlite','SYNCRIA_DB':str(Path(tmp)/'checks.db'),
                                'GEMINI_API_KEY':'offline-mock', 'GROQ_API_KEY':'',
                                'LLM_MODEL':'gemini-2.0-flash','LLM_FALLBACK':'',
                                'LLM_MAX_CALLS':'15','LLM_MAX_TOKENS':'25000'}, clear=False):
        from app.store import SQLiteStore
        SQLiteStore()
        llm._failures=0
        calls=[]
        def fake(*_):
            calls.append(1); return 'offline response',10,5
        def cache():
            with patch.object(llm,'_provider_request',side_effect=fake):
                first=llm.get_llm('check','repeat','fallback',incident_id='cache-check')
                before=len(calls)
                second=llm.get_llm('check','repeat','fallback',incident_id='cache-check')
            require(first==second=='offline response' and before==1 and len(calls)==before)
            db=llm._SQLiteAccounting()
            with db.connect() as conn:
                row=conn.execute("SELECT outcome,cache_hit FROM llm_usage ORDER BY id DESC LIMIT 1").fetchone()
            require(row['outcome']=='cache' and row['cache_hit']==1)
            return 'repeat: zero additional provider calls (mocked provider)'
        check('Cache hit',cache)
        def budget():
            with patch.dict(os.environ,{'LLM_MAX_CALLS':'0'}), patch.object(llm,'_provider_request',side_effect=AssertionError('must not call')):
                require(llm.get_llm('check','unique budget input','fallback',incident_id='budget-check')=='fallback')
            with llm._SQLiteAccounting().connect() as db:
                require(db.execute("SELECT outcome FROM llm_usage WHERE incident_id='budget-check' ORDER BY id DESC LIMIT 1").fetchone()['outcome']=='budget')
            return 'zero-call budget returned template without a provider call'
        check('Budget guard',budget)
        def login():
            from fastapi.testclient import TestClient
            from app import main
            with TestClient(main.app) as client:
                r=client.post('/api/login',json={'email':'controller@syncria.ai','password':'syncria123'})
                require(r.status_code==200 and bool(r.json().get('token')))
            return 'isolated SQLite demo account'
        check('Controller login (offline)',login)

def live_checks(api):
    from app import llm
    def supabase_counts():
        from supabase import create_client
        url=os.environ['SUPABASE_URL']; key=os.environ['SUPABASE_SERVICE_ROLE_KEY']
        client=create_client(url,key)
        expected={'hubs':6,'assets':7,'vehicles':60,'passengers':1100,'bookings':1500,'crew':30}
        counts={}
        for table,count in expected.items():
            counts[table]=client.table(table).select('id',count='exact').limit(1).execute().count
            require(counts[table]==count)
        for mode, expected_count in [('flight',40),('train',20)]:
            count=client.table('vehicles').select('id',count='exact').eq('mode',mode).limit(1).execute().count
            require(count==expected_count); counts[mode+'s']=count
        connections=client.table('bookings').select('id',count='exact').not_.is_('connecting_vehicle_id','null').limit(1).execute().count
        require(connections==210); counts['connections']=connections
        return ', '.join(f'{k}={v}' for k,v in counts.items())
    if os.getenv('SUPABASE_URL') and os.getenv('SUPABASE_SERVICE_ROLE_KEY'): check('Supabase seed counts',supabase_counts)
    else: report('Supabase seed counts','SKIP','set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in backend .env')
    def gemini():
        value,_,_=llm._provider_request('gemini-2.0-flash','Reply briefly.','Say hello.')
        require(bool(value.strip())); return 'free-tier model returned text'
    if os.getenv('GEMINI_API_KEY'): check('Gemini live',gemini)
    else: report('Gemini live','SKIP','set GEMINI_API_KEY in backend .env')
    def groq():
        # Force exactly the Gemini requests to fail; Groq request is real. Isolated
        # SQLite accounting avoids writing synthetic incident records to Supabase.
        original=llm._provider_request; called=[]
        def fail_gemini(model,*args):
            called.append(model)
            if llm.MODELS[model][0]=='gemini': raise RuntimeError('forced failure')
            return original(model,*args)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{
            'DB_BACKEND':'sqlite','SYNCRIA_DB':str(Path(tmp)/'fallback.db'),
            'GEMINI_API_KEY':'forced-failure','LLM_MODEL':'gemini-2.0-flash',
            'LLM_FALLBACK':'llama-3.3-70b-versatile','LLM_MAX_CALLS':'15','LLM_MAX_TOKENS':'25000'}):
            llm._failures=0
            with patch.object(llm,'_provider_request',side_effect=fail_gemini):
                text=llm.get_llm('Reply briefly.','Say hello '+uuid.uuid4().hex,'TEMPLATE',incident_id='fallback-check')
            require(text and text!='TEMPLATE' and called==['gemini-2.0-flash','gemini-2.0-flash','llama-3.3-70b-versatile'])
        return 'forced Gemini failure → live Groq text'
    if os.getenv('GROQ_API_KEY'): check('Groq fallback',groq)
    else: report('Groq fallback','SKIP','set GROQ_API_KEY in backend .env')
    def controller():
        import httpx
        require(bool(api))
        r=httpx.post(api.rstrip('/')+'/api/login',json={'email':'controller@syncria.ai','password':'syncria123'},timeout=15)
        require(r.status_code==200 and bool(r.json().get('token')))
        return 'running backend accepted demo credentials'
    if api: check('Controller login (running backend)',controller)
    else: report('Controller login (running backend)','SKIP','start backend and pass --api-url http://localhost:8000')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='make free-tier provider requests and check Supabase')
    parser.add_argument('--api-url',default='',help='running backend origin for live login check')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp: offline_checks(tmp)
    if args.live: live_checks(args.api_url)
    else:
        for name in ('Supabase seed counts','Gemini live','Groq fallback','Controller login (running backend)'):
            report(name,'SKIP','run with --live --api-url http://localhost:8000 and configure .env')
    if 'FAIL' in results: return 1
    if 'SKIP' in results:
        print('Incomplete: live checks skipped. Exit 2; run with --live to complete.')
        return 2
    print('All checks passed.'); return 0
if __name__=='__main__': sys.exit(main())
