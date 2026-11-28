"""Budgeted, cached, best-effort generation; validated agent proposals are advisory.

All paid requests require an atomic usage reservation. If accounting is unavailable,
we fail closed to the caller's template instead of making an unmetered request.
"""
import hashlib, json, logging, os, sqlite3, threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx
from .llm_prices import MODELS, cost_usd

_failures = 0
_lock = threading.RLock()  # Single-flight cache and circuit breaker within this process.
_logger = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parent.parent
_MAX_OUTPUT = 1024


def _now(): return datetime.now(timezone.utc)

def _limits():
    try: calls = max(0, min(12, int(os.getenv('LLM_MAX_CALLS', '12'))))
    except ValueError: calls = 12
    try: tokens = max(0, min(25000, int(os.getenv('LLM_MAX_TOKENS', '25000'))))
    except ValueError: tokens = 25000
    return calls, tokens


def cache_key(model, system, text):
    payload = json.dumps([model, system, text], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


class _SQLiteAccounting:
    def __init__(self):
        self.path = os.getenv('SYNCRIA_DB', str(_ROOT / 'syncria.db'))
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS llm_cache (cache_key TEXT PRIMARY KEY,response TEXT NOT NULL,model TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS llm_usage (id INTEGER PRIMARY KEY AUTOINCREMENT,incident_id TEXT NOT NULL,agent TEXT NOT NULL,model TEXT NOT NULL,prompt_tokens INTEGER,completion_tokens INTEGER,cost_usd NUMERIC,cache_hit INTEGER NOT NULL DEFAULT 0,at TEXT NOT NULL,outcome TEXT NOT NULL DEFAULT 'live',reserved_tokens INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS llm_usage_incident_idx ON llm_usage(incident_id,id);
            ''')
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db
    def cache_get(self, key):
        with self.connect() as db:
            row = db.execute('SELECT response FROM llm_cache WHERE cache_key=? AND expires_at>?', (key, _now().isoformat())).fetchone()
        return row['response'] if row else None
    def cache_put(self, key, model, response):
        now = _now()
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO llm_cache VALUES (?,?,?,?,?)', (key,response,model,now.isoformat(),(now+timedelta(hours=24)).isoformat()))
    def record(self, incident, agent, model, outcome, cache_hit=False):
        with self.connect() as db:
            db.execute('INSERT INTO llm_usage (incident_id,agent,model,prompt_tokens,completion_tokens,cost_usd,cache_hit,at,outcome) VALUES (?,?,?,?,?,?,?,?,?)', (incident,agent,model,0,0,0,int(cache_hit),_now().isoformat(),outcome))
    def reserve(self, incident, agent, model, tokens, max_calls, max_tokens):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT count(*) AS calls,coalesce(sum(CASE WHEN outcome IN ('pending','unknown') THEN reserved_tokens ELSE coalesce(prompt_tokens,0)+coalesce(completion_tokens,0) END),0) AS tokens FROM llm_usage WHERE incident_id=? AND outcome IN ('pending','live','unknown','failed')",(incident,)).fetchone()
            if row['calls'] >= max_calls or row['tokens'] + tokens > max_tokens: return None
            cursor = db.execute("INSERT INTO llm_usage (incident_id,agent,model,cache_hit,at,outcome,reserved_tokens) VALUES (?,?,?,0,?,'pending',?)",(incident,agent,model,_now().isoformat(),tokens))
            return cursor.lastrowid
    def finish(self, row_id, outcome, prompt=None, completion=None, cost=None):
        with self.connect() as db:
            db.execute('UPDATE llm_usage SET outcome=?,prompt_tokens=?,completion_tokens=?,cost_usd=? WHERE id=?',(outcome,prompt,completion,str(cost) if cost is not None else None,row_id))
    def warn(self, incident, agent):
        if incident == '__unscoped__': return
        with self.connect() as db:
            exists = db.execute("SELECT 1 FROM agent_log WHERE incident_id=? AND agent='llm' AND step='budget' LIMIT 1",(incident,)).fetchone()
            if not exists: db.execute('INSERT INTO agent_log (incident_id,agent,step,message,at) VALUES (?,?,?,?,?)',(incident,'llm','budget',f'LLM budget guard: template fallback for {agent}; incident limit reached.',_now().isoformat()))


class _SupabaseAccounting:
    def __init__(self):
        from supabase import create_client
        self.client = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY'])
    def cache_get(self,key):
        rows=self.client.table('llm_cache').select('response').eq('cache_key',key).gt('expires_at',_now().isoformat()).limit(1).execute().data
        return rows[0]['response'] if rows else None
    def cache_put(self,key,model,response):
        now=_now()
        self.client.table('llm_cache').upsert({'cache_key':key,'response':response,'model':model,'created_at':now.isoformat(),'expires_at':(now+timedelta(hours=24)).isoformat()},on_conflict='cache_key').execute()
    def record(self,incident,agent,model,outcome,cache_hit=False):
        self.client.table('llm_usage').insert({'incident_id':incident,'agent':agent,'model':model,'prompt_tokens':0,'completion_tokens':0,'cost_usd':0,'cache_hit':cache_hit,'at':_now().isoformat(),'outcome':outcome}).execute()
    def reserve(self,incident,agent,model,tokens,max_calls,max_tokens):
        return self.client.rpc('syncria_llm_reserve',{'p_incident':incident,'p_agent':agent,'p_model':model,'p_tokens':tokens,'p_max_calls':max_calls,'p_max_tokens':max_tokens}).execute().data
    def finish(self,row_id,outcome,prompt=None,completion=None,cost=None):
        self.client.table('llm_usage').update({'outcome':outcome,'prompt_tokens':prompt,'completion_tokens':completion,'cost_usd':float(cost) if cost is not None else None}).eq('id',row_id).execute()
    def warn(self,incident,agent):
        if incident == '__unscoped__': return
        rows=self.client.table('agent_log').select('id').eq('incident_id',incident).eq('agent','llm').eq('step','budget').limit(1).execute().data
        if not rows: self.client.table('agent_log').insert({'incident_id':incident,'agent':'llm','step':'budget','message':f'LLM budget guard: template fallback for {agent}; incident limit reached.','at':_now().isoformat()}).execute()


def _accounting():
    backend=os.getenv('DB_BACKEND','supabase').lower()
    if backend=='sqlite': return _SQLiteAccounting()
    if backend=='supabase': return _SupabaseAccounting()
    raise ValueError('Unknown DB_BACKEND')


def _provider_key(provider):
    return os.getenv({'gemini':'GEMINI_API_KEY','groq':'GROQ_API_KEY','openai':'OPENAI_API_KEY','anthropic':'ANTHROPIC_API_KEY'}[provider], '')


def _provider_request(model, system, text):
    provider=MODELS[model][0]; key=_provider_key(provider)
    remote_model=model.removesuffix('-paid')
    if provider=='gemini':
        r=httpx.post(f'https://generativelanguage.googleapis.com/v1beta/models/{remote_model}:generateContent',params={'key':key},json={'systemInstruction':{'parts':[{'text':system}]},'contents':[{'parts':[{'text':text}]}],'generationConfig':{'maxOutputTokens':_MAX_OUTPUT}},timeout=20)
        r.raise_for_status(); data=r.json(); value=data['candidates'][0]['content']['parts'][0]['text'].strip()
        usage=data.get('usageMetadata') or {}; prompt=usage.get('promptTokenCount'); total=usage.get('totalTokenCount'); completion=(total-prompt) if total is not None and prompt is not None else usage.get('candidatesTokenCount')
    elif provider in ('openai','groq'):
        url='https://api.openai.com/v1/chat/completions' if provider=='openai' else 'https://api.groq.com/openai/v1/chat/completions'
        r=httpx.post(url,headers={'Authorization':'Bearer '+key},json={'model':remote_model,'messages':[{'role':'system','content':system},{'role':'user','content':text}],'max_tokens':_MAX_OUTPUT},timeout=20)
        r.raise_for_status(); data=r.json(); value=data['choices'][0]['message']['content'].strip()
        usage=data.get('usage') or {}; prompt=usage.get('prompt_tokens'); completion=usage.get('completion_tokens')
    else:
        r=httpx.post('https://api.anthropic.com/v1/messages',headers={'x-api-key':key,'anthropic-version':'2023-06-01'},json={'model':remote_model,'system':system,'messages':[{'role':'user','content':text}],'max_tokens':_MAX_OUTPUT},timeout=20)
        r.raise_for_status(); data=r.json(); value=''.join(part.get('text','') for part in data.get('content',[]) if part.get('type')=='text').strip()
        usage=data.get('usage') or {}; prompt=usage.get('input_tokens'); completion=usage.get('output_tokens')
    if not value: raise ValueError('Empty model response')
    if not isinstance(prompt,int) or isinstance(prompt,bool) or prompt<0 or not isinstance(completion,int) or isinstance(completion,bool) or completion<0: prompt=completion=None
    return value,prompt,completion


def get_llm(system, text, fallback, *, incident_id=None, agent='unknown', priority=False, validator=None, free_only=False):
    """Return generated text or fallback. All accounting failures fail closed.

    Calls are reserved per attempt; cache hits and templates do not consume the live
    budget. Token reservations are conservative estimates, not provider quotas.
    """
    global _failures
    incident=incident_id or '__unscoped__'
    primary=(os.getenv('LLM_MODEL_PRIORITY') if priority else None) or os.getenv('LLM_MODEL','gemini-2.0-flash')
    secondary=os.getenv('LLM_FALLBACK','llama-3.3-70b-versatile')
    models=list(dict.fromkeys(m for m in (primary,secondary) if m))
    if free_only: models=[m for m in models if m in MODELS and all(rate == 0 for rate in MODELS[m][1:])]
    try:
        with _lock:
            db=_accounting()
            # A deliberately selected paid model must not be silently replaced by
            # an older free fallback cache when its own key is configured.
            primary_paid = primary in MODELS and any(rate > 0 for rate in MODELS[primary][1:]) and bool(_provider_key(MODELS[primary][0]))
            for model in (models[:1] if primary_paid else models):
                if model not in MODELS: continue
                cached=db.cache_get(cache_key(model,system,text))
                if cached is not None and (validator is None or validator(cached)):
                    db.record(incident,agent,model,'cache',True)
                    return cached
            if _failures >= 3:
                db.record(incident,agent,primary,'circuit'); return fallback
            calls,tokens=_limits()
            # UTF-8 byte count + message framing + max output is deliberately conservative.
            reservation=len(system.encode('utf-8'))+len(text.encode('utf-8'))+256+_MAX_OUTPUT
            for model in models:
                if model not in MODELS:
                    _logger.warning('LLM model is not priced/configured; skipping %s',model)
                    continue
                if model != primary and primary_paid:
                    cached=db.cache_get(cache_key(model,system,text))
                    if cached is not None and (validator is None or validator(cached)):
                        db.record(incident,agent,model,'cache',True)
                        return cached
                if not _provider_key(MODELS[model][0]): continue
                for _ in range(2 if MODELS[model][0]=='gemini' else 1):
                    if _failures >= 3: break
                    row_id=db.reserve(incident,agent,model,reservation,calls,tokens)
                    if row_id is None:
                        db.record(incident,agent,model,'budget')
                        try: db.warn(incident,agent)
                        except Exception: _logger.warning('Could not write LLM budget warning')
                        return fallback
                    try:
                        value,prompt,completion=_provider_request(model,system,text)
                    except Exception:
                        _failures+=1
                        # A timed-out request may have incurred usage; retain its reservation.
                        db.finish(row_id,'unknown')
                        continue
                    _failures=0
                    if prompt is None or completion is None:
                        db.finish(row_id,'unknown')
                    else:
                        db.finish(row_id,'live',prompt,completion,cost_usd(model,prompt,completion))
                    # Never cache invalid structured agent responses.
                    if validator is not None and not validator(value):
                        db.record(incident,agent,model,'invalid')
                        return fallback
                    db.cache_put(cache_key(model,system,text),model,value)
                    return value
            db.record(incident,agent,primary,'template')
            return fallback
    except Exception as exc:
        # Do not leak credentials or send an unmetered request if the DB is down.
        _logger.warning('LLM accounting unavailable (%s); using template',type(exc).__name__)
        return fallback
