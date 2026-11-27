import json
from datetime import datetime, timezone
from app.store import SQLiteStore

def scenario(tmp_path,monkeypatch,tiers,first_seats,second_seats,groups):
    monkeypatch.setenv('SYNCRIA_DB',str(tmp_path/'groups.db'))
    for k in ('GEMINI_API_KEY','GROQ_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY'): monkeypatch.delenv(k,raising=False)
    store=SQLiteStore(); vs,_,_=store.network_data(); source=vs[0]
    alternatives=[v for v in vs if v['origin']==source['origin'] and v['destination']==source['destination'] and v['departure']>source['departure']]
    first,second=alternatives[:2]
    with store.connect() as db:
        db.execute('DELETE FROM domain_bookings')
        for i,tier in enumerate(tiers):
            b={'booking_id':f'B{i}','id':f'P{i}','vehicle_id':source['id'],'tier':tier}
            db.execute('INSERT INTO domain_bookings VALUES (?,?)',(b['booking_id'],json.dumps(b)))
        for v in vs:
            v['available_seats']=first_seats if v['id']==first['id'] else second_seats if v['id']==second['id'] else 0
            db.execute('UPDATE domain_vehicles SET data=? WHERE id=?',(json.dumps(v),v['id']))
    state={'id':'group-test','created_at':datetime.now(timezone.utc).isoformat(),'impact':{'affected_vehicles':[source['id']]},'event':{'type':'storm','asset_id':source['asset_id'],'duration_h':2,'expected_end':source['departure']}}
    option={'option_id':'B','cancel_ids':[source['id']],'cost_usd':100,'groups':[{'booking_ids':members,'vehicle_id':first['id']} for members in groups]}
    return store,state,option,first,second

def test_fitting_cluster_moves_together_and_is_idempotent(tmp_path,monkeypatch):
    store,state,option,first,_=scenario(tmp_path,monkeypatch,['medic','gold','regular'],3,0,[['B0','B1','B2']])
    result=store.execute_plan(state,option)
    assert [c['to_vehicle'] for c in result['changes']]==[first['id']]*3
    assert store.execute_plan(state,option)==result
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM agent_log WHERE step='group_moved'").fetchone()[0]==1
        vehicle=json.loads(db.execute('SELECT data FROM domain_vehicles WHERE id=?',(first['id'],)).fetchone()[0]);assert vehicle['available_seats']==0
        assert all(json.loads(row[0])['status']=='rebooked' for row in db.execute('SELECT data FROM domain_bookings'))

def test_oversized_cluster_splits_and_preserves_global_priority(tmp_path,monkeypatch):
    store,state,option,first,second=scenario(tmp_path,monkeypatch,['regular','medic','gold','silver'],2,2,[['B0','B1','B2'],['B3']])
    result=store.execute_plan(state,option); changes=result['changes']
    assert [c['booking_id'] for c in changes]==['B1','B2','B3','B0']
    assert [c['to_vehicle'] for c in changes]==[first['id'],first['id'],second['id'],second['id']]
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM agent_log WHERE step='group_split'").fetchone()[0]==2
        assert all(json.loads(r[0])['available_seats']>=0 for r in db.execute('SELECT data FROM domain_vehicles'))

def test_interleaved_groups_do_not_jump_priority(tmp_path,monkeypatch):
    store,state,option,first,second=scenario(tmp_path,monkeypatch,['medic','regular','gold'],2,1,[['B0','B1'],['B2']])
    result=store.execute_plan(state,option)
    assert [c['booking_id'] for c in result['changes']]==['B0','B2','B1']
    assert [c['to_vehicle'] for c in result['changes']]==[first['id'],first['id'],second['id']]
    with store.connect() as db: assert db.execute("SELECT count(*) FROM agent_log WHERE step='group_split'").fetchone()[0]>=1
