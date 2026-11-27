import asyncio, copy, json, os, secrets, threading, uuid
from datetime import timedelta
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks, Response, Request
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from .engine import compute_cascade, generate_options
from .store import make_store
from .llm import get_llm
from .weather import get_weather, poll_forecasts
from .intelligence import apply_learning, cluster_passengers
from .passenger_assistant import answer as passenger_answer
from .graph import build_graph
from .notifications import make_notifications, greeting_templates
from .finance import savings_estimate, regulatory_exposure
from .controller_tools import crew_monitor, handover_template
from langgraph.types import Command
from collections import Counter
from contextlib import asynccontextmanager

ROOT = Path(__file__).resolve().parent.parent
lock = threading.RLock()
@asynccontextmanager
async def lifespan(app):
    stopped=threading.Event()
    if os.getenv('ENABLE_FORECAST','1')=='1':
        def loop():
            while not stopped.is_set():
                poll_watch_once()
                stopped.wait(900)
        threading.Thread(target=loop,daemon=True,name='forecast-watch').start()
    yield
    stopped.set()
app = FastAPI(title='Syncria AI',lifespan=lifespan)
app.mount('/static', StaticFiles(directory=ROOT/'static'), name='static')
store = make_store()
if store.name == 'supabase': store.refresh_schedule()
vehicles, passengers, crew = store.network_data()

def now(): return datetime.now(timezone.utc).isoformat()
def save(state): store.save(state)
def load(id):
    state = store.get(id)
    if state is None: raise HTTPException(404, 'Incident not found')
    return state
def log(state, agent, step, message):
    entry = {'agent': agent, 'step': step, 'message': message, 'at': now()}
    store.log(state['id'], entry)
    state['logs'].append(entry)

def impact_step(state):
    event=state['event']
    log(state, 'impact', 'start', 'Mapping the disruption across the network')
    current_vehicles,current_bookings,current_crew=store.network_data()
    impact=compute_cascade(current_vehicles, current_bookings, current_crew, event)
    state['impact']={k:v for k,v in impact.items() if k!='_bookings'}
    state['affected_bookings']=impact['_bookings']
    state['original_vehicles']=current_vehicles
    log(state,'impact','complete',f"{len(impact['affected_vehicles'])} services · {impact['pax_count']} bookings · {impact['missed_connections']} connections · {len(impact['crew_risks'])} crew at risk")
    save(state)

def planning_step(state, feedback=''):
    log(state, 'planner', 'start', 'Evaluating recovery strategies and operational constraints')
    impact={**state['impact'],'_bookings':state.get('affected_bookings',[])}
    if not feedback and state.get('decision'): feedback=state['decision'].get('feedback','')
    state['options']=generate_options(impact,state['event'],state.get('original_vehicles',vehicles),feedback)
    for option in state['options']:
        option['regulatory_exposure']=regulatory_exposure(state.get('affected_bookings',[]),state.get('original_vehicles',vehicles),state['event'],{**option,'_affected_vehicles':state['impact']['affected_vehicles']})
    log(state,'planner','compensation','Illustrative EU-style exposure (not legal advice): '+', '.join(f"{o['option_id']} €{o['regulatory_exposure']['total_eur']:,}" for o in state['options']))
    groups=cluster_passengers(state.get('affected_bookings',[]),state.get('original_vehicles',vehicles),state['impact']['affected_vehicles'])
    state['options'][1]['groups']=groups
    largest=max(groups,key=lambda g:g['size']) if groups else None
    if largest: log(state,'planner','grouping',f"Grouping: {sum(g['size'] for g in groups)} bookings → {len(groups)} clusters (largest {largest['size']} to {largest['destination']})")
    else: log(state,'planner','grouping','Grouping: no affected bookings')
    try:
        messages=apply_learning(state['options'],store.outcomes(state['event']['type']))
        if not messages: log(state,'planner','learning','Learning: fewer than three prior comparable outcomes; base confidence retained')
        for message in messages: log(state,'planner','learning','Learning: '+message)
    except Exception:
        log(state,'planner','learning','Historical learning unavailable; base confidence retained')
    state['plan_version']=state.get('plan_version',0)+1
    state['status']='awaiting_approval'
    store.persist_analysis(state)
    best=max(state['options'],key=lambda o:o['confidence'])
    log(state,'planner','complete',f"3 options ready · best {best['option_id']} ({best['confidence']}%, ${best['cost_usd']:,})")
    if state.get('previous_pax') is not None:
        log(state,'conductor','replan_complete',f"Repair 1h early · impact {state['previous_pax']} → {state['impact']['pax_count']} bookings · 3 options · best {best['option_id']} ({best['confidence']}%)")
        state['previous_pax']=None
    log(state,'conductor','pause','Paused at the human approval gate. No actions executed.')
    save(state)
    if any(os.getenv(k) for k in ('GEMINI_API_KEY','GROQ_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY')):
        threading.Thread(target=enrich_text,args=(state['id'],),daemon=True).start()
    if state['event']['type']=='storm' and os.getenv('ENABLE_WEATHER','1')=='1':
        threading.Thread(target=weather_context,args=(state['id'],),daemon=True).start()

def plan(state, feedback=''):
    # Legacy adapter for the original prototype tests.
    impact_step(state); planning_step(state,feedback)

def detect_step(state):
    # Detector runs at incident ingestion so the alert appears immediately.
    state['disruption']={'severity':state['event']['severity'],'expected_end':state['event']['expected_end'],'asset_id':state['event']['asset_id']}

def execute_step(state):
    option=next(o for o in state['options'] if o['option_id']==state['decision']['option_id'])
    log(state,'executor','start','Applying the approved plan atomically')
    result=store.execute_plan(state,option)
    state['executed']=result
    state['work_orders']=[result['work_order']] if result.get('work_order') else []
    state['status']='executing'
    state['savings']={**savings_estimate(state['impact']['pax_count'],state['event']['duration_h'],option['cost_usd']),'at':now(),'plan_version':state.get('plan_version',1),'option_id':option['option_id']}
    estimate=state['savings']
    log(state,'executor','savings',f"Estimated savings: manual baseline ${estimate['baseline_usd']:,} vs plan ${estimate['plan_cost_usd']:,} → ${estimate['savings_usd']:,} (not actual)")
    n=sum(bool(c['to_vehicle']) for c in result['changes']); rail=sum(c['mode']=='train' for c in result['changes'])
    tiers={b['booking_id']:b.get('tier','regular') for b in state.get('affected_bookings',[])}
    seated=Counter(tiers.get(c['booking_id'],'regular') for c in result['changes'] if c.get('to_vehicle'))
    log(state,'executor','priority',f"Priority rebooking: {seated['medic']} medic + {seated['gold']} gold seated; allocations considered in score order")
    log(state,'executor','complete',f"Canceled {len(result['cancelled'])} services · rebooked {n} bookings · {rail} to rail · {len(result['changes'])-n} need seats; inventory rechecked at execution")
    save(state)

def notify_step(state):
    log(state,'communicator','start','Preparing personalized traveler updates')
    vs,_,_=store.network_data()
    notes=make_notifications(state,state['affected_bookings'],state['original_vehicles'],vs,state['executed'])
    for note in notes: note['plan_version']=state.get('plan_version',1)
    store.add_notifications(state['id'],notes)
    state['notifications']=store.get_notifications(state['id']); counts=Counter(n['language'] for n in notes)
    log(state,'communicator','complete',f"{len(notes)} notifications ready ("+' · '.join(f"{k} {counts[k]}" for k in ('en','hi','ta','es'))+')')
    state['status']='done'; save(state)
    if any(os.getenv(k) for k in ('GEMINI_API_KEY','GROQ_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY')):
        threading.Thread(target=enrich_notifications,args=(state['id'],sorted(counts),{b['language'] for b in state.get('affected_bookings',[]) if b.get('tier') in ('gold','medic')}),daemon=True).start()

def enrich_notifications(id,languages,priority_languages=()):
    try:
        templates=greeting_templates(languages,incident_id=id,priority_languages=priority_languages)
        with lock:
            state=load(id)
            if state['status']!='done': return
            store.add_greetings(id,templates)
            state['notifications']=store.get_notifications(id)
            log(state,'communicator','text','Batched language greetings prepared')
            save(state)
    except Exception:
        return  # Original complete template notifications remain intact.

graph=build_graph(detect_step,impact_step,planning_step,execute_step,notify_step)

def graph_config(id): return {'configurable':{'thread_id':id}}

def start_graph(state):
    state['incident_id']=state['id']
    graph.invoke(state,config=graph_config(state['id']))


def weather_context(id):
    weather=get_weather()
    if not weather: return
    try:
        with lock:
            current=load(id)
            current['event']['corroborated_by_weather']=weather['storm']
            label='thunderstorm observed' if weather['storm'] else 'no thunderstorm observed'
            log(current,'detector','weather',f"Live weather: {label} · wind {weather['wind_kmh']} km/h. Simulation is unchanged.")
            save(current)
    except Exception:
        return

def enrich_text(id):
    # Runs after deterministic plans are visible. Failure can only affect prose.
    try:
        snapshot=load(id); i=snapshot['impact']
        impact_slice={k:i[k] for k in ('pax_count','pax_breakdown','missed_connections','crew_risks','affected_vehicles')}
        summary=get_llm('You are the Syncria impact analyst. Explain the operational impact in no more than 60 words, no preamble.', json.dumps(impact_slice), i['summary_md'], incident_id=id, agent='impact')
        opts=snapshot['options']; option_slice=[{k:o[k] for k in ('option_id','strategy','cost_usd','pax_affected','crew_safe','rules_compliant','confidence')} for o in opts]
        fallback='\n'.join(o['option_id']+': '+o['explanation'] for o in opts)
        explanations=get_llm('You are the Syncria recovery planner. Write exactly three lines, A:, B:, C:, one line each, total at most 60 words; name the cost and operational trade-offs. Do not change the numbers.', json.dumps(option_slice), fallback, incident_id=id, agent='planner')
        lines=explanations.splitlines()
        with lock:
            current=load(id)
            if current['status'] != 'awaiting_approval' or current['options'] != opts: return
            current['impact']['summary_md']=summary
            if len(lines)==3 and all(line.strip().startswith(label+':') for line,label in zip(lines,'ABC')):
                for o,line in zip(current['options'],lines): o['explanation']=o['explanation_md']=line.split(':',1)[1].strip()
            store.update_text(current)
            log(current,'planner','text','Optional text explanations updated')
            save(current)
    except Exception:
        # Templates already persisted; never interrupt the controller flow.
        return

def run_pipeline(state):
    try:
        start_graph(state)
    except Exception:
        # Keep failure visible to the controller without leaking credentials.
        state['status']='failed'
        try:
            log(state,'conductor','error','Analysis failed. Check backend logs and database connectivity.')
            save(state)
        except Exception:
            pass
        import logging
        logging.getLogger(__name__).exception('Incident pipeline failed')

class IncidentRequest(BaseModel): event: dict
class Decision(BaseModel): action: str; option_id: str = ''; feedback: str = ''; edits: dict = {}
class Login(BaseModel):
    email: str = ''
    password: str
class Rebooking(BaseModel):
    incident_id: str
    passenger_id: str
    vehicle_id: str
    booking_id: str = ''

# Offline tokens are process-local; Supabase sessions use verified access tokens.
tokens = set()
@app.post('/api/login')
def login(data: Login, response: Response):
    if store.name == 'sqlite' or os.getenv('ENABLE_LEGACY_FLOW') == '1':
        legacy=os.getenv('ENABLE_LEGACY_FLOW')=='1' and not data.email
        expected=os.getenv('CONTROLLER_PASSWORD','demo') if legacy else 'syncria123'
        if (not legacy and data.email.lower()!='controller@syncria.ai') or not secrets.compare_digest(data.password,expected): raise HTTPException(401,'Invalid credentials')
        token=secrets.token_urlsafe(32); tokens.add(token); response.set_cookie('syncria_session',token,httponly=True,samesite='strict',secure=os.getenv('SECURE_COOKIES')=='1'); return {'token':token,'email':data.email or 'controller@syncria.ai'}
    try:
        from supabase import create_client
        client=create_client(os.environ['SUPABASE_URL'],os.environ['SUPABASE_ANON_KEY'])
        result=client.auth.sign_in_with_password({'email':data.email,'password':data.password})
        if not result.user or result.user.email.lower()!=os.getenv('CONTROLLER_EMAIL','controller@syncria.ai').lower(): raise ValueError('Not controller')
        response.set_cookie('syncria_session',result.session.access_token,httponly=True,samesite='strict',secure=os.getenv('SECURE_COOKIES')=='1')
        return {'token':result.session.access_token,'email':result.user.email}
    except Exception: raise HTTPException(401,'Invalid credentials or authentication unavailable')

def authorize(authorization, missing_status=401):
    if not authorization or not authorization.startswith('Bearer '): raise HTTPException(missing_status,'Controller login required')
    token=authorization[7:]
    if store.name=='sqlite' or os.getenv('ENABLE_LEGACY_FLOW')=='1':
        if token not in tokens: raise HTTPException(401,'Controller login required')
        return 'controller@syncria.ai'
    try:
        from supabase import create_client
        client=create_client(os.environ['SUPABASE_URL'],os.environ['SUPABASE_ANON_KEY'])
        user=client.auth.get_user(token).user
        if not user or user.email.lower()!=os.getenv('CONTROLLER_EMAIL','controller@syncria.ai').lower(): raise ValueError('Not controller')
        return user.email
    except Exception: raise HTTPException(401,'Controller session expired')

@app.get('/api/session')
def session(authorization: str = Header(default='')): return {'email':authorize(authorization)}
@app.post('/api/logout')
def logout(response: Response, authorization: str = Header(default='')):
    response.delete_cookie('syncria_session')
    if authorization.startswith('Bearer '): tokens.discard(authorization[7:])
    return {'status':'signed_out'}

@app.middleware('http')
async def protect_dashboard(request: Request, call_next):
    path=request.url.path
    protected=(path in ('/api/schedule','/api/replan','/api/watches','/api/roi','/api/crew','/api/handover') or path.startswith('/api/work_orders') or path.startswith('/api/incidents'))
    # Passenger feed is a demo endpoint; authentication/PNR ownership is future work.
    if path.endswith('/passengers'): protected=False
    if protected and os.getenv('REQUIRE_CONTROLLER_LOGIN','1')=='1':
        token=request.headers.get('authorization') or ('Bearer '+request.cookies.get('syncria_session',''))
        try: authorize(token)
        except HTTPException as e: return JSONResponse({'detail':e.detail},status_code=e.status_code)
    return await call_next(request)

# Outermost middleware must handle unauthenticated browser preflights.
app.add_middleware(CORSMiddleware,
    allow_origins=[origin.strip().rstrip('/') for origin in os.getenv('ALLOWED_ORIGINS','').split(',') if origin.strip()],
    allow_credentials=False, allow_methods=['GET','POST','PATCH','OPTIONS'],
    allow_headers=['Authorization','Content-Type'])

@app.get('/')
def index(): return FileResponse(ROOT/'static/index.html')
@app.get('/api/health')
def health(): return {'status':'ok', 'storage':store.name, 'engine':'python'}

@app.get('/health')
def detailed_health(response: Response):
    try:
        if store.name=='sqlite':
            with store.connect() as db: db.execute('SELECT 1').fetchone()
        else: store.client.table('vehicles').select('id').limit(1).execute()
        db_status='reachable'
    except Exception:
        db_status='unreachable'; response.status_code=503
    # Configuration only; health checks never make provider calls or incur cost.
    providers={name:bool(os.getenv(key)) for name,key in [('gemini','GEMINI_API_KEY'),('groq','GROQ_API_KEY'),('openai','OPENAI_API_KEY'),('anthropic','ANTHROPIC_API_KEY')]}
    return {'status':'ok' if db_status=='reachable' else 'degraded','db':{'backend':store.name,'status':db_status},'llm':{'status':'configured' if any(providers.values()) else 'template_only','configured_providers':providers,'live_verified':False}}

def record_watch(watch):
    existing={w['id']:w for w in store.list_watches()}
    if existing.get(watch['id'])==watch: return
    store.save_watch(watch)
    store.log(None,{'agent':'detector','step':'watch','message':watch['message'],'at':now()})

def poll_watch_once():
    try:
        for watch in poll_forecasts(): record_watch(watch)
    except Exception: pass  # Advisory failure cannot block the operational flow.

@app.get('/api/watches')
def list_watches():
    try:
        current=datetime.now(timezone.utc)
        return [w for w in store.list_watches() if datetime.fromisoformat(w['expected_at'].replace('Z','+00:00'))>=current]
    except Exception: return []

@app.post('/api/watches')
def simulate_watch():
    at=datetime.now(timezone.utc)+timedelta(hours=8)
    watch={'id':'simulation-BLR','hub':'BLR','reason':'thunderstorm','wind_kmh':45,'expected_at':at.isoformat(),'hours':8,'source':'simulation','message':'Early Warning: simulated thunderstorm at BLR in ~8h (winds 45 km/h) — pre-position crews recommended'}
    record_watch(watch); return watch

@app.get('/api/roi')
def roi():
    month=datetime.now(timezone.utc).strftime('%Y-%m')
    # One latest executed estimate per incident; re-plans are not added together.
    estimates=[s['savings'] for s in store.all_states() if s.get('savings') and s['savings'].get('at','').startswith(month)]
    return {'month':month,'total_savings_usd':sum(e['savings_usd'] for e in estimates),'incident_count':len(estimates),'basis':'estimated, latest executed plan per incident; not actual realized savings'}

def log_controller_tool(agent,step,message):
    store.log(None,{'agent':agent,'step':step,'message':message,'at':now()})

@app.get('/api/crew')
def crew_legality():
    roster=crew_monitor(store.crew_roster())
    log_controller_tool('crew','legality',f"Crew duty snapshot: {len(roster)} members, {sum(bool(c.get('alert')) for c in roster)} alerts")
    return {'crew':roster,'basis':'simplified duty-end projection, not legal or regulatory advice'}

@app.post('/api/handover')
def handover():
    states=store.all_states(); crew=crew_monitor(store.crew_roster())
    orders=[w for w in store.list_orders() if str(w.get('status','')).lower() not in ('done','completed','closed')]
    decisions=[]
    for incident in states[:10]:
        for decision in store.audit_data(incident['id'])['decisions']:
            decisions.append({**decision,'incident_id':incident['id']})
    decisions.sort(key=lambda d:d.get('at') or '',reverse=True)
    template=handover_template(states,orders,crew,decisions[:10])
    # Input contains incident IDs and counts only; no passenger records or secrets.
    text=get_llm('Rewrite this synthetic transport shift handover clearly and concisely. Do not invent or omit any facts.',template,template,agent='conductor')
    log_controller_tool('conductor','handover',f"Shift handover generated: {len(states)} incidents reviewed, {len(orders)} open work orders")
    return {'text':text,'basis':'Demo snapshot; verify before operational use'}

@app.get('/api/schedule')
def schedule():
    current_vehicles,current_bookings,current_crew=store.network_data()
    return {'vehicles':current_vehicles,'passengers':len(current_bookings),'crew':current_crew}
@app.get('/api/incidents/latest')
def latest():
    active=[s for s in store.all_states() if s.get('status') in ('active','awaiting_approval','executing','replanning')]
    if not active: return None
    newest=max(active,key=lambda s:s.get('created_at',''))
    return load(newest['id'])
@app.get('/api/incidents')
def list_incidents():
    states=sorted(store.all_states(),key=lambda s:s.get('created_at',''),reverse=True)
    return [{'id':s['id'],'status':s['status'],'created_at':s['created_at'],'event_type':s.get('event',{}).get('type','unknown')} for s in states]

@app.get('/api/incidents/{id}')
def get_incident(id: str): return load(id)
@app.post('/api/incidents', status_code=202)
def create(data: IncidentRequest, background_tasks: BackgroundTasks):
    event = data.event.copy(); kind = event.get('type')
    if kind not in ('storm','signal','engine','crew_noshow'): raise HTTPException(400, 'Unknown event type')
    defaults = {'storm':('RW-17R',2), 'signal':('S-12',3), 'engine':('AK-204',4), 'crew_noshow':('AK-207',1.5)}
    event.setdefault('asset_id', defaults[kind][0]); event.setdefault('duration_h', defaults[kind][1]); event.setdefault('source','chaos_console')
    if not isinstance(event['duration_h'], (int,float)) or event['duration_h'] <= 0: raise HTTPException(400, 'Invalid duration')
    event['severity'] = 'medium' if kind == 'crew_noshow' else 'high'
    # Chaos events replay the seeded 06:00 UTC morning cohort, independent of wall clock.
    simulation_start = datetime.now(timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0)
    event['window_start'] = simulation_start.isoformat()
    event['expected_end'] = (simulation_start+timedelta(hours=event['duration_h'])).isoformat()
    state = {'id':'INC-'+uuid.uuid4().hex[:6].upper(), 'event':event, 'created_at':now(), 'status':'active', 'logs':[], 'notifications':[], 'work_orders':[], 'executed':[]}
    store.create(state)
    log(state, 'detector', 'start', 'Incoming disruption signal received')
    log(state, 'detector', 'complete', f"{kind.replace('_',' ').title()} detected on {event['asset_id']} · {event['severity'].upper()} · estimated recovery {event['expected_end']}")
    save(state)
    if store.name == 'supabase': background_tasks.add_task(run_pipeline, state)
    elif os.getenv('ENABLE_LEGACY_FLOW') == '1': plan(state)
    else: start_graph(state)
    return {'incident_id':state['id']}
def graph_decision(id, data, authorization):
    controller=authorize(authorization, missing_status=403)
    with lock:
        state=load(id)
        if state['status']!='awaiting_approval': raise HTTPException(409,'Incident is not awaiting approval')
        if data.action not in ('approve','reject'): raise HTTPException(400,'Invalid decision')
        if data.edits: raise HTTPException(400,'Edits not supported; reject with feedback')
        if data.action=='approve' and not any(o['option_id']==data.option_id for o in state['options']): raise HTTPException(400,'Select a valid option')
        if data.action=='reject' and state.get('reject_count',0)>=2: raise HTTPException(409,'Two rejections reached. Approve the highest-confidence option; nothing executes automatically.')
        if data.action=='approve' and state.get('reject_count',0)>=2:
            best=max(state['options'],key=lambda o:o['confidence'])['option_id']
            if data.option_id!=best: raise HTTPException(409,f'After two rejections, approve recommended option {best}.')
        decision=data.model_dump(); decision['decided_by']=controller; decision['plan_version']=state.get('plan_version',1)
        store.decision(id,decision,now())
        state['decision']=decision
        if data.action=='reject': state['reject_count']=state.get('reject_count',0)+1
        log(state,'conductor','decision',f"Controller {data.action}d option {data.option_id or 'set'}")
        save(state)
        config=graph_config(id)
        snapshot=graph.get_state(config)
        if not snapshot.values:
            # MemorySaver is ephemeral; reconstruct the pending interrupt from persisted state.
            restored={**state,'restore':True}
            graph.invoke(restored,config=config)
        # Refresh graph's shared state with the audited decision/count before resume.
        graph.update_state(config,{'reject_count':state.get('reject_count',0)})
        try:
            graph.invoke(Command(resume=decision),config=config)
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Decision pipeline failed')
            raise HTTPException(500,'Execution failed; see server logs. No automatic retry was attempted.')
        return load(id)

@app.post('/api/incidents/{id}/decision')
def decide(id: str, data: Decision, authorization: str = Header(default='')):
    if os.getenv('ENABLE_LEGACY_FLOW') != '1': return graph_decision(id,data,authorization)
    authorize(authorization)
    with lock:
        state = load(id)
        if state['status'] != 'awaiting_approval': raise HTTPException(409, 'Incident is not awaiting approval')
        if data.action not in ('approve','reject'): raise HTTPException(400, 'Invalid decision')
        option = next((o for o in state['options'] if o['option_id']==data.option_id), None)
        if data.action == 'approve' and not option: raise HTTPException(400, 'Select a valid option')
        if data.edits: raise HTTPException(400, 'Edits are not supported; reject with feedback to replan')
        store.decision(id, data.model_dump(), now())
        if data.action == 'reject':
            log(state,'conductor','decision','Controller rejected the plan; returning to planning')
            plan(state, data.feedback); return state
        state['status']='executing'; state['decision']=data.model_dump()
        log(state,'conductor','decision',f'Controller approved option {option["option_id"]}')
        log(state,'executor','start','Applying approved recovery actions')
        state['executed'] = option['actions'][:]
        # Rebooking is offered, not automatically claimed, because seat allocation must be explicit.
        log(state,'executor','complete',f'{len(state["executed"])} service actions recorded')
        log(state,'communicator','start','Preparing personalized traveler updates')
        affected = [p for p in passengers if p['id'] in state['impact']['passenger_ids']]
        state['notifications'] = []
        for p in affected:
            v = next(v for v in vehicles if v['id']==p['vehicle_id'])
            alternatives = [x for x in vehicles if x['origin']==v['origin'] and x['destination']==v['destination'] and x['id'] not in state['impact']['affected_vehicles'] and x['available_seats']>0][:3]
            cause = state['event']['type'].replace('_',' ')
            messages = {'en': f"Hi {p['name']}, your service {v['id']} is affected by {cause}. The controller approved {option['strategy']}; no replacement has been assigned yet. Choose an available alternative below. Estimated recovery: {state['event']['duration_h']} hours.", 'hi': f"नमस्ते {p['name']}, आपकी सेवा {v['id']} {cause} के कारण प्रभावित है। {option['strategy']} स्वीकृत है; अभी कोई नई सेवा आवंटित नहीं है। नीचे विकल्प चुनें। अनुमानित सुधार: {state['event']['duration_h']} घंटे।", 'ta': f"வணக்கம் {p['name']}, உங்கள் சேவை {v['id']} {cause} காரணமாக பாதிக்கப்பட்டுள்ளது. {option['strategy']} அங்கீகரிக்கப்பட்டது; மாற்று இன்னும் ஒதுக்கப்படவில்லை. கீழே தேர்வு செய்யவும். மீட்பு: {state['event']['duration_h']} மணி நேரம்.", 'es': f"Hola {p['name']}, su servicio {v['id']} está afectado por {cause}. Se aprobó {option['strategy']}; aún no tiene reemplazo. Elija una alternativa abajo. Recuperación estimada: {state['event']['duration_h']} horas."}
            state['notifications'].append({'passenger_id':p['id'],'name':p['name'],'language':p['language'],'pnr':p['pnr'],'vehicle_id':v['id'],'body':messages[p['language']], 'alternatives':[x['id'] for x in alternatives]})
        log(state,'communicator','complete',f"{len(affected)} personalized updates prepared")
        log(state,'triage','start','Assessing asset repair requirements')
        state['work_orders'].append({'id':'WO-'+id[-6:], 'asset':state['event']['asset_id'], 'diagnosis':f"Inspect and restore asset following {cause}", 'priority':'High', 'eta':f"{state['event']['duration_h']}h", 'status':'Open'})
        log(state,'triage','complete','Work order created for asset inspection')
        state['status']='done'; save(state); return state
@app.post('/api/replan')
def replan_without_incident():
    import logging
    logging.getLogger(__name__).info('No active incident to re-plan')
    return {'message':'No active incident to re-plan'}

@app.post('/api/incidents/{id}/replan')
def replan(id: str, authorization: str = Header(default='')):
    if os.getenv('ENABLE_LEGACY_FLOW') == '1':
        authorize(authorization)
        with lock:
            state=load(id)
            if state['status'] not in ('done','awaiting_approval'): raise HTTPException(409,'Cannot replan now')
            if state['event'].get('recovered_early'): raise HTTPException(409,'Early recovery already applied')
            state['event']['recovered_early']=True; state['event']['duration_h']=max(.5,state['event']['duration_h']-1)
            log(state,'conductor','replanning','Repair completed early · recalculating impact')
            plan(state); return state
    with lock:
        state=load(id)
        if state['status'] not in ('awaiting_approval','done'): raise HTTPException(409,'Cannot replan while analysis or execution is in progress')
        if state['event'].get('recovered_early'): raise HTTPException(409,'Early recovery already applied')
        event=state['event'].copy()
        old_end=datetime.fromisoformat(event['expected_end'].replace('Z','+00:00'))
        start=datetime.fromisoformat(event['window_start'].replace('Z','+00:00'))
        new_end=max(start,old_end-timedelta(hours=1))
        event.update({'expected_end':new_end.isoformat(),'duration_h':max(0,(new_end-start).total_seconds()/3600),'recovered_early':True,'replanning':True})
        state['event']=event; state['previous_pax']=state['impact']['pax_count']; state['status']='replanning'; state['decision']=None; state['reject_count']=0
        store.early_event(state)
        log(state,'conductor','re_planning','Repair finished early · re-entering impact analysis on the same incident thread')
        save(state)
        config=graph_config(id)
        try:
            snapshot=graph.get_state(config)
            if snapshot.values:
                graph.update_state(config,{'event':event,'status':'replanning','decision':None,'reject_count':0,'previous_pax':state['previous_pax'],'plan_version':state.get('plan_version',1)},as_node='detect')
                graph.invoke(None,config=config)
            else:
                # MemorySaver was lost across restart: rebuild the same incident thread.
                state['incident_id']=id; state['restore']=False
                graph.invoke(state,config=config)
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Re-planning failed')
            state=load(id); state['status']='replanning'; save(state)
            raise HTTPException(500,'Re-planning failed; no new plan was executed')
        return load(id)
def timeline_entries(logs):
    entries=[]
    for index,entry in enumerate(logs):
        duration=None
        if index+1<len(logs):
            try:
                start=datetime.fromisoformat(entry['at'].replace('Z','+00:00'))
                end=datetime.fromisoformat(logs[index+1]['at'].replace('Z','+00:00'))
                duration=max(0,round((end-start).total_seconds(),3))
            except (KeyError,ValueError,TypeError): pass
        entries.append({**entry,'seconds_to_next':duration})
    return entries

@app.get('/api/incidents/{id}/audit')
def incident_audit(id: str):
    state=load(id)
    log(state,'conductor','audit','Controller opened incident audit report')
    save(state)
    data=store.audit_data(id)
    # Notifications are read from their own table; they may lag execution.
    return {'incident_id':id,'created_at':state['created_at'],'status':state['status'],
            'event':state['event'],'impact':state.get('impact'),'current_options':state.get('options',[]),
            'savings':state.get('savings'),'timeline':timeline_entries(load(id).get('logs',[])),
            'notifications':store.get_notifications(id),**data}

@app.get('/api/incidents/{id}/passengers')
def passenger_view(id: str, passenger_id: str = ''):
    load(id)
    notes=store.get_notifications(id) if os.getenv('ENABLE_LEGACY_FLOW')!='1' else load(id)['notifications']
    return {'notifications':[n for n in notes if not passenger_id or n['passenger_id']==passenger_id]}
class WhatIfRequest(BaseModel):
    action: str
    vehicle_id: str = ''
    delay_hours: float = 2

@app.post('/api/incidents/{id}/what-if')
def what_if(id: str, data: WhatIfRequest):
    # Deliberate logging exception: even agent_log/LLM usage writes would violate
    # the controller's zero-write dry-run guarantee.
    if data.action not in ('cancel','delay'): raise HTTPException(400,'Choose cancel or delay')
    if not 0 < data.delay_hours <= 48: raise HTTPException(400,'Delay must be greater than zero and at most 48 hours')
    state=load(id); vs,bs,cs=copy.deepcopy(store.network_data()); event=copy.deepcopy(state['event'])
    if data.action=='delay':
        event['duration_h']=data.delay_hours
        start=datetime.fromisoformat(event['window_start'].replace('Z','+00:00'))
        event['expected_end']=(start+timedelta(hours=data.delay_hours)).isoformat()
    impact=compute_cascade(vs,bs,cs,event)
    if data.action=='cancel' and data.vehicle_id not in impact['affected_vehicles']: raise HTTPException(400,'Choose an affected service')
    options=generate_options(impact,event,vs,data.vehicle_id if data.action=='cancel' else '')
    try: apply_learning(options,store.outcomes(event['type']))
    except Exception: pass
    for option in options:
        option['regulatory_exposure']=regulatory_exposure(impact['_bookings'],vs,event,{**option,'_affected_vehicles':impact['affected_vehicles']})
    return {'dry_run':True,'action':data.action,'selected_option':'B' if data.action=='cancel' else 'A',
            'impact':{k:v for k,v in impact.items() if k!='_bookings'},'options':options,
            'disclaimer':'Projection only: no database writes, execution, notifications, or reservations.'}

class PassengerQuestion(BaseModel):
    passenger_id: str
    booking_id: str
    text: str

@app.post('/api/incidents/{id}/voice')
def voice_assistant(id: str, data: PassengerQuestion):
    if not data.text.strip() or len(data.text)>500: raise HTTPException(400,'Question must be 1–500 characters')
    with lock:
        state=load(id)
        note=next((n for n in store.get_notifications(id) if n.get('passenger_id')==data.passenger_id and n.get('booking_id')==data.booking_id),None)
        if not note: raise HTTPException(404,'Passenger booking not found')
        vs,bs,_=store.network_data(); booking=next((b for b in bs if b['booking_id']==data.booking_id and b['id']==data.passenger_id),None)
        if not booking: raise HTTPException(404,'Booking not found')
        vehicle=next((v for v in vs if v['id']==booking['vehicle_id']),{})
        lang=note.get('language','en'); intent,reply=passenger_answer(data.text,lang,booking,vehicle,state['event'])
    if intent=='unknown':
        # Do not send the stored booking or passenger identity to the provider.
        reply=get_llm('Answer a general transport passenger question briefly in language '+lang+'. Do not invent facts or bookings.',data.text,reply,incident_id=id,agent='passenger_assistant')
    with lock:
        state=load(id)
        log(state,'communicator','passenger_assistant',f'Passenger assistant answered {intent} intent ({lang})')
        save(state)
    return {'intent':intent,'answer':reply,'language':lang}

@app.get('/api/incidents/{id}/alternatives')
def alternatives(id: str, passenger_id: str, booking_id: str):
    state=load(id)
    if not any(n.get('passenger_id')==passenger_id and n.get('booking_id')==booking_id for n in store.get_notifications(id)):
        raise HTTPException(404,'Passenger booking not found')
    vs,bs,_=store.network_data(); booking=next((b for b in bs if b['booking_id']==booking_id and b['id']==passenger_id),None)
    if not booking: raise HTTPException(404,'Booking not found')
    current=next((v for v in vs if v['id']==booking['vehicle_id']),None)
    if not current: raise HTTPException(404,'Service not found')
    options=[v for v in vs if v['id']!=current['id'] and v['status']=='scheduled' and v['available_seats']>0 and (v['origin'],v['destination'])==(current['origin'],current['destination']) and v['departure']>current['departure']]
    return {'alternatives':sorted(options,key=lambda v:(v.get('arrival') or v['departure'],v['departure'],v['id']))}

@app.get('/api/work_orders')
def work_orders():
    return store.list_orders()

class WorkOrderUpdate(BaseModel): status: str
@app.patch('/api/work_orders/{order_id}')
def update_work_order(order_id: str, data: WorkOrderUpdate, authorization: str = Header(default='')):
    authorize(authorization)
    if data.status not in ('in_progress','done'): raise HTTPException(400,'Status must be in_progress or done')
    with lock:
        try: item=store.update_order(order_id,data.status)
        except ValueError as exc: raise HTTPException(409,str(exc))
        if item is None: raise HTTPException(404,'Work order not found')
        state=load(item['incident_id'])
        log(state,'triage','work_order',f"Work order {order_id}: repair {'started' if data.status=='in_progress' else 'completed'}")
        state['work_orders']=[item if w['id']==order_id else w for w in state.get('work_orders',[])]
        save(state)
        return item
@app.post('/api/rebooking')
def rebook(data: Rebooking):
    if os.getenv('ENABLE_LEGACY_FLOW') != '1':
        with lock:
            state=load(data.incident_id)
            if state['status']!='done': raise HTTPException(409,'Recovery not complete')
            if not data.booking_id: raise HTTPException(400,'booking_id required')
            try: result=store.rebook_real(data.incident_id,data.passenger_id,data.booking_id,data.vehicle_id)
            except Exception: raise HTTPException(409,'Alternative unavailable or booking not found')
            state['notifications']=store.get_notifications(data.incident_id)
            log(state,'communicator','rebook',f"Passenger confirmed alternative {data.vehicle_id}")
            save(state)
            return result
    with lock:
        state=load(data.incident_id)
        note=next((n for n in state['notifications'] if n['passenger_id']==data.passenger_id),None)
        if not note or data.vehicle_id not in note['alternatives']: raise HTTPException(400,'Alternative unavailable')
        if note.get('rebooked_to'): raise HTTPException(409,'Already rebooked')
        # Track capacity across persisted incident records under the same process lock.
        used=sum(n.get('rebooked_to')==data.vehicle_id for item in store.all_states() for n in item['notifications'])
        target=next(v for v in vehicles if v['id']==data.vehicle_id)
        if used >= target['available_seats']: raise HTTPException(409,'No seats remaining')
        note['rebooked_to']=data.vehicle_id; save(state); return {'status':'confirmed','vehicle_id':data.vehicle_id}
@app.get('/api/incidents/{id}/stream')
async def stream(id: str):
    load(id)
    async def events():
        last=''; seen=0; last_status=''; last_decision=''; last_options=''
        def event(name,payload): return f'event: {name}\ndata: {json.dumps(payload)}\n\n'
        while True:
            state=load(id); logs=state.get('logs',[])
            for entry in logs[seen:]:
                if entry['step']=='start': yield event('agent_start',entry)
                if entry['step']=='re_planning': yield event('re_planning',{'reason':entry['message']})
                yield event('agent_log',entry)
                if entry['agent']=='executor': yield event('executing',entry)
            seen=len(logs)
            decision=json.dumps(state.get('decision'),sort_keys=True)
            if state.get('decision') and decision!=last_decision: yield event('decision_applied',state['decision'])
            last_decision=decision
            payload=json.dumps(state)
            if payload!=last:
                yield f'event: state_update\ndata: {payload}\n\n'; last=payload
            options=json.dumps(state.get('options',[]),sort_keys=True)
            if state['status']=='awaiting_approval' and (state['status']!=last_status or options!=last_options):
                yield event('awaiting_approval',{'options':state.get('options',[])})
                last_options=options
            if state['status']!=last_status:
                if state['status']=='done':
                    yield event('notified',{'count':len(state.get('notifications',[])),'total':len(state.get('notifications',[]))})
                    yield event('completed',{'summary':'Recovery actions and notifications completed'})
                last_status=state['status']
            yield ': heartbeat\n\n'
            await asyncio.sleep(1)
    return StreamingResponse(events(), media_type='text/event-stream', headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
