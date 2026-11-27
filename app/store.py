"""Storage adapters. Supabase is server-side only; service keys never reach browsers."""
import json, os, sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .engine import seed
from .intelligence import priority_order

ROOT = Path(__file__).resolve().parent.parent

class SQLiteStore:
    name = 'sqlite'
    def __init__(self):
        self.path = os.getenv('SYNCRIA_DB', str(ROOT/'syncria.db'))
        with self.connect() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, state TEXT NOT NULL); CREATE TABLE IF NOT EXISTS agent_log (id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT, agent TEXT, step TEXT, message TEXT, at TEXT); CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT, data TEXT, at TEXT); CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, incident_id TEXT, data TEXT NOT NULL); CREATE TABLE IF NOT EXISTS impacts (incident_id TEXT PRIMARY KEY, data TEXT NOT NULL); CREATE TABLE IF NOT EXISTS recovery_plans (incident_id TEXT, option_id TEXT, data TEXT NOT NULL, PRIMARY KEY (incident_id, option_id));')
        self.network = seed()
        with self.connect() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS domain_vehicles (id TEXT PRIMARY KEY, data TEXT NOT NULL); CREATE TABLE IF NOT EXISTS domain_bookings (id TEXT PRIMARY KEY, data TEXT NOT NULL); CREATE TABLE IF NOT EXISTS domain_notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL, passenger_id TEXT NOT NULL, booking_id TEXT, data TEXT NOT NULL); CREATE TABLE IF NOT EXISTS domain_work_orders (id TEXT PRIMARY KEY, data TEXT NOT NULL); CREATE TABLE IF NOT EXISTS executions (incident_id TEXT PRIMARY KEY, data TEXT NOT NULL); CREATE TABLE IF NOT EXISTS execution_versions (incident_id TEXT, plan_version INTEGER, data TEXT NOT NULL, PRIMARY KEY (incident_id,plan_version)); CREATE TABLE IF NOT EXISTS recovery_plan_versions (incident_id TEXT, plan_version INTEGER, option_id TEXT, data TEXT NOT NULL, superseded INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (incident_id,plan_version,option_id)); CREATE TABLE IF NOT EXISTS domain_assets (id TEXT PRIMARY KEY, expected_repair_end TEXT); CREATE TABLE IF NOT EXISTS incident_outcomes (incident_id TEXT, plan_version INTEGER, event_type TEXT, chosen_option TEXT, estimated_cost REAL, actual_pax_moved INTEGER, resolution_min REAL, success INTEGER, at TEXT, PRIMARY KEY (incident_id,plan_version)); CREATE TABLE IF NOT EXISTS weather_watches (id TEXT PRIMARY KEY, data TEXT NOT NULL);')
            db.execute('INSERT OR IGNORE INTO execution_versions SELECT incident_id,1,data FROM executions')
            db.execute('INSERT OR IGNORE INTO recovery_plan_versions SELECT incident_id,1,option_id,data,0 FROM recovery_plans')
            db.executemany('INSERT OR IGNORE INTO domain_vehicles VALUES (?,?)', [(v['id'],json.dumps(v)) for v in self.network[0]])
            db.executemany('INSERT OR IGNORE INTO domain_bookings VALUES (?,?)', [(b['booking_id'],json.dumps(b)) for b in self.network[1]])
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10); db.row_factory=sqlite3.Row; return db
    def network_data(self):
        with self.connect() as db:
            vs=[json.loads(r['data']) for r in db.execute('SELECT data FROM domain_vehicles ORDER BY id')]
            bs=[json.loads(r['data']) for r in db.execute('SELECT data FROM domain_bookings ORDER BY id')]
        return vs,bs,self.network[2]
    def execute_plan(self, state, option):
        # The entire plan, including the idempotency marker, is one SQLite transaction.
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            version=state.get('plan_version',1)
            previous=db.execute('SELECT data FROM execution_versions WHERE incident_id=? AND plan_version=?',(state['id'],version)).fetchone()
            if previous: return json.loads(previous['data'])
            vehicles={r['id']:json.loads(r['data']) for r in db.execute('SELECT id,data FROM domain_vehicles')}
            bookings=[json.loads(r['data']) for r in db.execute('SELECT data FROM domain_bookings ORDER BY id')]
            canceled=option.get('cancel_ids',[]); affected=set(state['impact']['affected_vehicles'])
            for id in canceled:
                if id not in affected or id not in vehicles: raise ValueError('Invalid cancellation')
                vehicles[id]['status']='canceled'
            changes=[]
            ordered_bookings=[b for b in priority_order(bookings,vehicles) if b['vehicle_id'] in canceled and b.get('status')!='canceled']
            groups=option.get('groups') or []
            memberships={}
            for index,group in enumerate(groups):
                for booking_id in group.get('booking_ids',[]):
                    if booking_id in memberships: raise ValueError('Booking appears in multiple groups')
                    memberships[booking_id]=index
            positions={b['booking_id']:i for i,b in enumerate(ordered_bookings)}
            attempted={}
            def eligible(v,original):
                return v and v['id'] not in affected and v['status']=='scheduled' and v['origin']==original['origin'] and v['destination']==original['destination'] and v['departure']>original['departure']
            for booking in ordered_bookings:
                source=booking['vehicle_id']; original=vehicles[source]
                group_index=memberships.get(booking['booking_id']); target=None
                if group_index is not None:
                    group=groups[group_index]; proposed=vehicles.get(group.get('vehicle_id'))
                    if group_index not in attempted:
                        ids=group.get('booking_ids',[])
                        indexes=[positions[i] for i in ids if i in positions]
                        # Reserve whole groups only when doing so cannot jump any higher-priority booking.
                        contiguous=bool(indexes) and len(set(ids))==len(ids)==len(indexes) and max(indexes)-min(indexes)+1==len(indexes)
                        fits=contiguous and proposed is not None and proposed['available_seats']>=len(ids) and all(eligible(proposed,vehicles[ordered_bookings[i]['vehicle_id']]) for i in indexes)
                        attempted[group_index]=bool(fits)
                    if eligible(proposed,original) and proposed['available_seats']>0: target=proposed
                if target is None:
                    candidates=sorted((v for v in vehicles.values() if eligible(v,original) and v['available_seats']>0),key=lambda v:(v['mode']!=original['mode'],v['departure'],v['id']))
                    target=candidates[0] if candidates else None
                if target:
                    target['available_seats']-=1; booking['vehicle_id']=target['id']; booking['status']='rebooked'; booking['rebooked_to']=target['id']
                else: booking['status']='canceled'
                db.execute('UPDATE domain_bookings SET data=? WHERE id=?',(json.dumps(booking),booking['booking_id']))
                changes.append({'booking_id':booking['booking_id'],'passenger_id':booking['id'],'from_vehicle':source,'to_vehicle':target['id'] if target else None,'mode':target['mode'] if target else None})
            by_booking={c['booking_id']:c for c in changes}
            for index,group in enumerate(groups):
                member_changes=[by_booking[b] for b in group.get('booking_ids',[]) if b in by_booking]
                if not member_changes: continue
                proposed=vehicles.get(group.get('vehicle_id'))
                moved=sum(c['to_vehicle']==group.get('vehicle_id') and c['to_vehicle'] is not None for c in member_changes)
                whole=attempted.get(index,False) and moved==len(group.get('booking_ids',[]))
                if whole:
                    message=f"Group moved: {moved} bookings → {proposed['destination']} → {proposed['mode']} {proposed['id']} {proposed['departure']}"
                    step='group_moved'
                else:
                    message=f"Group split: {moved} of {len(member_changes)} displaced bookings on suggested {group.get('vehicle_id') or 'unavailable vehicle'}; remainder handled individually in priority order"
                    step='group_split'
                db.execute('INSERT INTO agent_log (incident_id,agent,step,message,at) VALUES (?,?,?,?,?)',(state['id'],'executor',step,message,datetime.now(timezone.utc).isoformat()))
            ordered=state['impact']['affected_vehicles']
            for idx,id in enumerate(ordered):
                if id not in vehicles or vehicles[id]['status']!='scheduled': continue
                v=vehicles[id]; dep=datetime.fromisoformat(v['departure'].replace('Z','+00:00'))
                if option['option_id']=='C':
                    delay=timedelta(minutes=45) if state['event']['type']=='signal' else timedelta(hours=1 if idx >= (len(ordered)+1)//2 else 0)
                    if state['event']['type']=='storm' and v['mode']=='flight': v['asset_id']='RW-35L'
                else:
                    end=datetime.fromisoformat(state['event']['expected_end'].replace('Z','+00:00'))
                    delay=max(timedelta(0),end-dep)
                v['departure']=(dep+delay).isoformat()
                if v.get('arrival'): v['arrival']=(datetime.fromisoformat(v['arrival'].replace('Z','+00:00'))+delay).isoformat()
                v['status']='delayed'
            for v in vehicles.values(): db.execute('UPDATE domain_vehicles SET data=? WHERE id=?',(json.dumps(v),v['id']))
            wo=None
            if state['event']['type']!='crew_noshow':
                wo={'id':'WO-'+state['id'],'incident_id':state['id'],'asset':state['event']['asset_id'],'diagnosis':'Inspect and restore affected asset','priority':'High','eta':str(state['event']['duration_h'])+'h','status':'Open'}
                db.execute('INSERT OR IGNORE INTO domain_work_orders VALUES (?,?)',(wo['id'],json.dumps(wo)))
                wo=json.loads(db.execute('SELECT data FROM domain_work_orders WHERE id=?',(wo['id'],)).fetchone()['data'])
            result={'cancelled':canceled,'changes':changes,'work_order':wo}
            db.execute('INSERT INTO execution_versions VALUES (?,?,?)',(state['id'],version,json.dumps(result)))
            at=datetime.now(timezone.utc); created=datetime.fromisoformat(state['created_at'].replace('Z','+00:00'))
            db.execute('INSERT OR IGNORE INTO incident_outcomes VALUES (?,?,?,?,?,?,?,?,?)',(state['id'],version,state['event']['type'],option['option_id'],option['cost_usd'],sum(bool(c['to_vehicle']) for c in changes),max(0,(at-created).total_seconds()/60),int(all(c['to_vehicle'] for c in changes)),at.isoformat()))
            return result
    def add_notifications(self, incident_id, notes):
        with self.connect() as db:
            for n in notes:
                exists=db.execute('SELECT 1 FROM domain_notifications WHERE incident_id=? AND passenger_id=? AND json_extract(data,"$.kind")="initial" AND coalesce(json_extract(data,"$.plan_version"),1)=?',(incident_id,n['passenger_id'],n.get('plan_version',1))).fetchone()
                if not exists: db.execute('INSERT INTO domain_notifications (incident_id,passenger_id,booking_id,data) VALUES (?,?,?,?)',(incident_id,n['passenger_id'],n.get('booking_id'),json.dumps(n)))
    def add_greetings(self,incident_id,greetings):
        with self.connect() as db:
            for row in db.execute('SELECT id,data FROM domain_notifications WHERE incident_id=?',(incident_id,)).fetchall():
                note=json.loads(row['data'])
                if note.get('kind')!='initial' or note.get('llm_greeting'): continue
                greeting=greetings.get(note['language'])
                if greeting:
                    note['llm_greeting']=greeting.replace('{name}',note['name'])
                    note['body']=note['llm_greeting']+' '+note['body']
                    db.execute('UPDATE domain_notifications SET data=? WHERE id=?',(json.dumps(note),row['id']))
    def get_notifications(self,incident_id):
        with self.connect() as db: return [json.loads(r['data']) for r in db.execute('SELECT data FROM domain_notifications WHERE incident_id=? ORDER BY id',(incident_id,))]
    def rebook_real(self, incident_id, passenger_id, booking_id, vehicle_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT data FROM domain_bookings WHERE id=?',(booking_id,)).fetchone()
            target=db.execute('SELECT data FROM domain_vehicles WHERE id=?',(vehicle_id,)).fetchone()
            if not row or not target: raise ValueError('Booking or service not found')
            booking=json.loads(row['data']); dest=json.loads(target['data'])
            notes=[json.loads(r['data']) for r in db.execute('SELECT data FROM domain_notifications WHERE incident_id=? AND passenger_id=? AND booking_id=?',(incident_id,passenger_id,booking_id))]
            if booking['id']!=passenger_id or not notes: raise ValueError('Booking does not belong to passenger or incident')
            origin_row=db.execute('SELECT data FROM domain_vehicles WHERE id=?',(booking['vehicle_id'],)).fetchone(); origin=json.loads(origin_row['data'])
            if dest['id']==origin['id'] or dest['status']!='scheduled' or dest['available_seats']<=0 or (dest['origin'],dest['destination'])!=(origin['origin'],origin['destination']) or dest['departure']<=origin['departure']: raise ValueError('Alternative unavailable')
            if booking.get('status')=='rebooked': origin['available_seats']+=1; db.execute('UPDATE domain_vehicles SET data=? WHERE id=?',(json.dumps(origin),origin['id']))
            dest['available_seats']-=1; booking['vehicle_id']=dest['id']; booking['rebooked_to']=dest['id']; booking['status']='rebooked'
            db.execute('UPDATE domain_vehicles SET data=? WHERE id=?',(json.dumps(dest),dest['id']))
            db.execute('UPDATE domain_bookings SET data=? WHERE id=?',(json.dumps(booking),booking_id))
            note={**notes[-1], 'kind':'confirmation', 'body':f"{booking['name']}: rebooking confirmed on {dest['id']} at {dest['departure']}. Boarding pass updated.", 'vehicle_id':dest['id'],'rebooked_to':dest['id'],'alternatives':[]}
            db.execute('INSERT INTO domain_notifications (incident_id,passenger_id,booking_id,data) VALUES (?,?,?,?)',(incident_id,passenger_id,booking_id,json.dumps(note)))
            return {'status':'confirmed','vehicle_id':dest['id']}

    def create(self,state):
        with self.connect() as db: db.execute('INSERT INTO events VALUES (?,?,?)',(state['id'],state['id'],json.dumps(state['event'])))
        self.save(state)
    def save(self,state):
        with self.connect() as db: db.execute('INSERT OR REPLACE INTO incidents VALUES (?,?)',(state['id'],json.dumps(state)))
    def get(self,id):
        with self.connect() as db:
            row=db.execute('SELECT state FROM incidents WHERE id=?',(id,)).fetchone()
            if not row: return None
            logs=db.execute('SELECT agent,step,message,at FROM agent_log WHERE incident_id=? ORDER BY id',(id,)).fetchall()
        state=json.loads(row['state']); state['logs']=[dict(entry) for entry in logs]; return state
    def all_states(self):
        with self.connect() as db: rows=db.execute('SELECT state FROM incidents ORDER BY rowid DESC').fetchall()
        return [json.loads(r['state']) for r in rows]
    def log(self,id,entry):
        with self.connect() as db: db.execute('INSERT INTO agent_log (incident_id,agent,step,message,at) VALUES (?,?,?,?,?)',(id,entry['agent'],entry['step'],entry['message'],entry['at']))
    def decision(self,id,data,at):
        with self.connect() as db: db.execute('INSERT INTO decisions (incident_id,data,at) VALUES (?,?,?)',(id,json.dumps(data),at))
    def persist_analysis(self,state):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO impacts VALUES (?,?)',(state['id'],json.dumps(state['impact'])))
            version=state.get('plan_version',1)
            db.execute('UPDATE recovery_plan_versions SET superseded=1 WHERE incident_id=? AND plan_version<?',(state['id'],version))
            for o in state['options']:
                db.execute('INSERT OR REPLACE INTO recovery_plans VALUES (?,?,?)',(state['id'],o['option_id'],json.dumps(o)))
                db.execute('INSERT OR REPLACE INTO recovery_plan_versions VALUES (?,?,?,?,0)',(state['id'],version,o['option_id'],json.dumps(o)))
    def early_event(self,state):
        import uuid
        with self.connect() as db:
            db.execute('INSERT INTO events VALUES (?,?,?)',(uuid.uuid4().hex,state['id'],json.dumps({'type':'repair_early','asset_id':state['event']['asset_id'],'expected_end':state['event']['expected_end']})))
            db.execute('INSERT OR REPLACE INTO domain_assets VALUES (?,?)',(state['event']['asset_id'],state['event']['expected_end']))
    def crew_roster(self):
        vehicles={v['id']:v for v in self.network_data()[0]}
        original={v['id']:v for v in self.network[0]}
        result=[]
        for member in self.network[2]:
            v=vehicles.get(member['vehicle_id']); seed_vehicle=original.get(member['vehicle_id'])
            # The synthetic duty start is derived from the original schedule,
            # not shifted when execution delays or cancels the service.
            dep=datetime.fromisoformat(seed_vehicle['departure'].replace('Z','+00:00')) if seed_vehicle else None
            end=dep+timedelta(hours=member.get('remaining_hours',12)) if dep else None
            assignments=[{'vehicle_id':v['id'],'departure':v['departure'],'arrival':v.get('arrival')}] if v and v.get('status')!='canceled' else []
            result.append({'id':member['name'],'name':member['name'],'role':'Unknown (synthetic)',
                           'duty_start':(end-timedelta(hours=12)).isoformat() if end else None,'duty_limit_hours':12,'assignments':assignments})
        return result
    def audit_data(self,id):
        with self.connect() as db:
            plans=[{'plan_version':r['plan_version'],'superseded':bool(r['superseded']),**json.loads(r['data'])} for r in db.execute('SELECT plan_version,superseded,data FROM recovery_plan_versions WHERE incident_id=? ORDER BY plan_version,option_id',(id,))]
            decisions=[{**json.loads(r['data']),'at':r['at']} for r in db.execute('SELECT data,at FROM decisions WHERE incident_id=? ORDER BY id',(id,))]
            executions=[{'plan_version':r['plan_version'],**json.loads(r['data'])} for r in db.execute('SELECT plan_version,data FROM execution_versions WHERE incident_id=? ORDER BY plan_version',(id,))]
        return {'plans':plans,'decisions':decisions,'executions':executions}
    def outcomes(self,event_type):
        with self.connect() as db: return [dict(r) for r in db.execute('SELECT * FROM incident_outcomes WHERE event_type=? ORDER BY at',(event_type,))]
    def save_watch(self,watch):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO weather_watches VALUES (?,?)',(watch['id'],json.dumps(watch)))
    def list_watches(self):
        with self.connect() as db: return [json.loads(r['data']) for r in db.execute('SELECT data FROM weather_watches ORDER BY id')]
    def list_orders(self):
        with self.connect() as db: return [json.loads(r['data']) for r in db.execute('SELECT data FROM domain_work_orders ORDER BY id DESC')]
    def update_order(self,id,status):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT data FROM domain_work_orders WHERE id=?',(id,)).fetchone()
            if not row: return None
            item=json.loads(row['data']); current=item['status'].lower()
            if status!=current and (current,status) not in [('open','in_progress'),('scheduled','in_progress'),('in_progress','done')]: raise ValueError('Invalid status transition')
            item['status']=status; db.execute('UPDATE domain_work_orders SET data=? WHERE id=?',(json.dumps(item),id)); return item
    def update_text(self,state): pass

class SupabaseStore:
    name='supabase'
    def __init__(self, client=None):
        if client is None:
            from supabase import create_client
            url=os.getenv('SUPABASE_URL'); key=os.getenv('SUPABASE_SERVICE_ROLE_KEY')
            if not url or not key: raise RuntimeError('SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required')
            client=create_client(url,key)
        self.client=client
    def rows(self,table,columns='*'):
        result=[]; page=0; size=500
        while True:
            rows=self.client.table(table).select(columns).order('id').range(page*size,(page+1)*size-1).execute().data
            result.extend(rows)
            if len(rows)<size: return result
            page+=1
    def refresh_schedule(self):
        # Preserve schedule times and dates relative to one another. Update only
        # synthetic schedule/roster fields; never delete incidents or bookings.
        vs=self.rows('vehicles'); cs=self.rows('crew')
        if not vs: raise RuntimeError('Supabase schedule is empty; apply seed.sql first')
        today=datetime.now(timezone.utc).date()
        for v in vs:
            dep=datetime.fromisoformat(v['dep_at'].replace('Z','+00:00')); arr=datetime.fromisoformat(v['arr_at'].replace('Z','+00:00'))
            shift=(today-dep.date()).days
            if shift: self.client.table('vehicles').update({'dep_at':(dep+timedelta(days=shift)).isoformat(),'arr_at':(arr+timedelta(days=shift)).isoformat()}).eq('id',v['id']).execute()
        # Duty start is seeded relative to the first departure, including prior day.
        first=min(datetime.fromisoformat(v['dep_at'].replace('Z','+00:00')).date() for v in vs)
        shift=(today-first).days
        if shift:
            for c in cs:
                start=datetime.fromisoformat(c['duty_start'].replace('Z','+00:00'))
                self.client.table('crew').update({'duty_start':(start+timedelta(days=shift)).isoformat()}).eq('id',c['id']).execute()
    def network_data(self):
        vs=self.rows('vehicles'); ps=self.rows('passengers'); bs=self.rows('bookings'); cs=self.rows('crew')
        # Assignments have a composite key rather than an id.
        assignments=[]; offset=0
        while True:
            part=self.client.table('crew_assignments').select('*').order('crew_id').order('vehicle_id').range(offset,offset+499).execute().data
            assignments.extend(part)
            if len(part)<500: break
            offset+=500
        people={p['id']:p for p in ps}; crew_by_id={c['id']:c for c in cs}
        vehicles=[{'id':v['id'],'mode':v['mode'],'origin':v['from_hub'].removesuffix('-R'),'destination':v['to_hub'].removesuffix('-R'),'departure':v['dep_at'],'arrival':v['arr_at'],'capacity':v['capacity'],'available_seats':v['available_seats'],'status':v['status'],'asset_id':v['asset_id']} for v in vs]
        lookup={v['id']:v for v in vehicles}
        bookings=[]
        for b in bs:
            p=people[b['passenger_id']]
            bookings.append({'id':p['id'],'name':p['name'],'tier':b['tier'],'language':p['language'],'booking_id':b['id'],'vehicle_id':b['vehicle_id'],'pnr':b['pnr'],'fare':float(b['fare']),'status':b.get('status','confirmed'),'rebooked_to':b.get('rebooked_to_vehicle_id'),'special':p.get('special'),'family':p.get('family',False),'elderly':p.get('elderly',False),'connection':bool(b['connecting_vehicle_id']),'connecting_vehicle_id':b['connecting_vehicle_id']})
        crew=[]
        for a in assignments:
            c=crew_by_id[a['crew_id']]; v=lookup[a['vehicle_id']]
            start=datetime.fromisoformat(c['duty_start'].replace('Z','+00:00')); departure=datetime.fromisoformat(v['departure'].replace('Z','+00:00'))
            remaining=(start+timedelta(hours=float(c['duty_limit_hours']))-departure).total_seconds()/3600
            crew.append({'name':c['name'],'vehicle_id':v['id'],'remaining_hours':remaining})
        return vehicles,bookings,crew
    def create(self,state):
        e=state['event']; created=state['created_at']; end=(datetime.fromisoformat(created)+timedelta(hours=e['duration_h'])).isoformat()
        self.client.table('events').insert({'id':state['id'],'type':e['type'],'asset_id':e['asset_id'],'severity':e['severity'],'source':e['source'],'started_at':e.get('window_start',created),'expected_end':e.get('expected_end',end),'status':'active'}).execute()
        self.client.table('incidents').insert({'id':state['id'],'event_id':state['id'],'status':state['status'],'created_at':created,'state':state}).execute()
    def save(self,state): self.client.table('incidents').update({'state':state,'status':state['status']}).eq('id',state['id']).execute()
    def get(self,id):
        rows=self.client.table('incidents').select('state').eq('id',id).limit(1).execute().data
        if not rows: return None
        state=rows[0]['state']; logs=[]; offset=0
        while True:
            part=self.client.table('agent_log').select('agent,step,message,at').eq('incident_id',id).order('id').range(offset,offset+499).execute().data
            logs.extend(part)
            if len(part)<500: break
            offset+=500
        state['logs']=logs
        return state
    def all_states(self): return [r['state'] for r in sorted(self.rows('incidents','id,state,created_at'), key=lambda row:row['created_at'], reverse=True)]
    def log(self,id,entry): self.client.table('agent_log').insert({'incident_id':id,**entry}).execute()
    def decision(self,id,data,at): self.client.table('decisions').insert({'incident_id':id,'option_id':data.get('option_id'),'action':data['action'],'edits':data.get('edits'),'feedback':data.get('feedback'),'decided_by':data.get('decided_by'),'plan_version':data.get('plan_version',1),'at':at}).execute()
    def persist_analysis(self,state):
        i=state['impact']; public={k:i[k] for k in ('affected_vehicles','pax_count','pax_breakdown','missed_connections','crew_risks','summary_md')}; public['priority_pax']=[f"{p['name']} ({p['tier']}, {p['vehicle_id']})" for p in i['priority_pax'][:5]]
        self.client.table('impacts').insert({'incident_id':state['id'],'affected_vehicle_ids':i['affected_vehicles'],'pax_count':i['pax_count'],'missed_connections':i['missed_connections'],'crew_risks':i['crew_risks'],'summary_md':i['summary_md'],'raw':public}).execute()
        version=state.get('plan_version',1)
        self.client.table('recovery_plans').update({'superseded':True}).eq('incident_id',state['id']).lt('plan_version',version).execute()
        plans=[{'incident_id':state['id'],'plan_version':version,'superseded':False,'option_id':o['option_id'],'strategy':o['strategy'],'cost_usd':o['cost_usd'],'pax_affected':o['pax_affected'],'crew_safe':o['crew_safe'],'rules_compliant':o['rules_compliant'],'confidence':o['confidence'],'explanation_md':o['explanation_md'],'actions':o['actions']} for o in state['options']]
        self.client.table('recovery_plans').upsert(plans,on_conflict='incident_id,plan_version,option_id').execute()
    def update_text(self,state):
        latest=self.client.table('impacts').select('id').eq('incident_id',state['id']).order('id',desc=True).limit(1).execute().data
        if latest: self.client.table('impacts').update({'summary_md':state['impact']['summary_md']}).eq('id',latest[0]['id']).execute()
        for option in state['options']:
            self.client.table('recovery_plans').update({'explanation_md':option['explanation_md']}).eq('incident_id',state['id']).eq('option_id',option['option_id']).eq('plan_version',state.get('plan_version',1)).execute()
    def execute_plan(self,state,option):
        return self.client.rpc('syncria_execute_plan',{'p_incident':state['id'],'p_option':option['option_id']}).execute().data
    def add_notifications(self,incident_id,notes):
        existing={(n['passenger_id'],n.get('plan_version',1)) for n in self.get_notifications(incident_id) if n.get('kind','initial')=='initial'}
        rows=[{'incident_id':incident_id,'passenger_id':n['passenger_id'],'booking_id':n['booking_id'],'channel':'in_app','lang':n['language'],'body':n['body'],'status':'sent','sent_at':datetime.now(timezone.utc).isoformat(),'metadata':n,'notification_kind':'initial','plan_version':n.get('plan_version',1)} for n in notes if (n['passenger_id'],n.get('plan_version',1)) not in existing]
        for i in range(0,len(rows),200): self.client.table('notifications').insert(rows[i:i+200]).execute()
    def add_greetings(self,incident_id,greetings):
        rows=[]; offset=0
        while True:
            page=self.client.table('notifications').select('*').eq('incident_id',incident_id).eq('notification_kind','initial').order('id').range(offset,offset+499).execute().data
            for row in page:
                note=row.get('metadata') or {}; greeting=greetings.get(note.get('language'))
                if greeting and not note.get('llm_greeting'):
                    note['llm_greeting']=greeting.replace('{name}',note.get('name',''))
                    note['body']=note['llm_greeting']+' '+note['body']
                    row['body']=note['body']; row['metadata']=note; rows.append(row)
            if len(page)<500: break
            offset+=500
        for i in range(0,len(rows),200): self.client.table('notifications').upsert(rows[i:i+200],on_conflict='id').execute()
    def get_notifications(self,incident_id):
        result=[]; offset=0
        while True:
            rows=self.client.table('notifications').select('*').eq('incident_id',incident_id).order('id').range(offset,offset+499).execute().data
            for r in rows:
                note=r.get('metadata') or {}; note.update({'passenger_id':r['passenger_id'],'booking_id':r.get('booking_id'),'language':r['lang'],'body':r['body'],'kind':r.get('notification_kind','initial'),'plan_version':r.get('plan_version',1)}); result.append(note)
            if len(rows)<500: return result
            offset+=500
    def rebook_real(self,incident_id,passenger_id,booking_id,vehicle_id):
        return self.client.rpc('syncria_rebook',{'p_incident':incident_id,'p_passenger':passenger_id,'p_booking':booking_id,'p_vehicle':vehicle_id}).execute().data
    def early_event(self,state):
        self.client.rpc('syncria_replan_event',{'p_incident':state['id'],'p_expected_end':state['event']['expected_end']}).execute()
    def crew_roster(self):
        crew=self.rows('crew'); vs={v['id']:v for v in self.network_data()[0]}; assignments=[]; offset=0
        while True:
            page=self.client.table('crew_assignments').select('*').order('crew_id').order('vehicle_id').range(offset,offset+499).execute().data
            assignments.extend(page)
            if len(page)<500: break
            offset+=500
        by_crew={}
        for assignment in assignments:
            v=vs.get(assignment['vehicle_id'])
            if v and v.get('status')!='canceled': by_crew.setdefault(assignment['crew_id'],[]).append({'vehicle_id':v['id'],'departure':v['departure'],'arrival':v.get('arrival')})
        return [{'id':c['id'],'name':c['name'],'role':c['role'],'duty_start':c['duty_start'],'duty_limit_hours':float(c['duty_limit_hours']),'assignments':by_crew.get(c['id'],[])} for c in crew]
    def audit_data(self,id):
        def rows(table,order):
            result=[]; offset=0
            while True:
                part=self.client.table(table).select('*').eq('incident_id',id).order(order).range(offset,offset+499).execute().data
                result.extend(part)
                if len(part)<500: return result
                offset+=500
        executions=[{'plan_version':r['plan_version'],**r['result']} for r in rows('execution_results','plan_version')]
        return {'plans':rows('recovery_plans','id'),'decisions':rows('decisions','id'),'executions':executions}
    def outcomes(self,event_type):
        result=[]; offset=0
        while True:
            rows=self.client.table('incident_outcomes').select('*').eq('event_type',event_type).order('id').range(offset,offset+499).execute().data
            result.extend(rows)
            if len(rows)<500: return result
            offset+=500
    def save_watch(self,watch):
        self.client.table('weather_watches').upsert({'id':watch['id'],'data':watch},on_conflict='id').execute()
    def list_watches(self): return [r['data'] for r in self.rows('weather_watches')]
    def list_orders(self):
        return [{'id':w['id'],'incident_id':w['incident_id'],'asset':w['asset_id'],'diagnosis':w['diagnosis'],'priority':w['priority'],'status':w['status'],'assigned_crew':w.get('assigned_crew'),'eta':f"{w['eta_hours']}h" if w.get('eta_hours') is not None else '—','window_start':w.get('window_start'),'window_end':w.get('window_end')} for w in self.rows('work_orders')]
    def update_order(self,id,status):
        rows=self.client.table('work_orders').select('*').eq('id',id).limit(1).execute().data
        if not rows: return None
        w=rows[0]; current=w['status'].lower()
        if status!=current and (current,status) not in [('open','in_progress'),('scheduled','in_progress'),('in_progress','done')]: raise ValueError('Invalid status transition')
        if status!=current:
            result=self.client.table('work_orders').update({'status':status}).eq('id',id).eq('status',w['status']).execute().data
            if not result: raise ValueError('Work order changed; retry')
        return next((item for item in self.list_orders() if item['id']==id),None)

def make_store():
    backend=os.getenv('DB_BACKEND','supabase').lower()
    if backend=='sqlite': return SQLiteStore()
    if backend=='supabase': return SupabaseStore()
    raise RuntimeError('DB_BACKEND must be sqlite or supabase')
