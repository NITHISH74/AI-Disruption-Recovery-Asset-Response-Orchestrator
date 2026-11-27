import os
import tempfile
os.environ['SYNCRIA_DB'] = tempfile.mktemp(suffix='.db')
os.environ['CONTROLLER_PASSWORD'] = 'test-secret'
os.environ['DB_BACKEND'] = 'sqlite'
os.environ['ENABLE_WEATHER'] = '0'
os.environ['REQUIRE_CONTROLLER_LOGIN'] = '0'
os.environ['ENABLE_LEGACY_FLOW'] = '1'
from fastapi.testclient import TestClient
from app.main import app
from app.engine import seed, compute_cascade, generate_options

client = TestClient(app)

def test_deterministic_core():
    vehicles, passengers, crew = seed()
    event = {'type':'storm','duration_h':2}
    first = compute_cascade(vehicles, passengers, crew, event)
    assert first == compute_cascade(vehicles, passengers, crew, event)
    assert len(first['affected_vehicles']) == 6 and first['pax_count'] > 0
    assert len(first['crew_risks']) == 2
    assert generate_options(first, event, vehicles) == generate_options(first, event, vehicles)

def test_story_twice():
    token = client.post('/api/login', json={'password':'test-secret'}).json()['token']
    auth = {'Authorization':'Bearer '+token}
    for _ in range(2):
        response = client.post('/api/incidents',json={'event':{'type':'storm'}})
        assert response.status_code == 202
        incident = response.json()['incident_id']
        state = client.get('/api/incidents/'+incident).json()
        assert state['status'] == 'awaiting_approval' and len(state['options']) == 3
        assert not state['notifications'] and not state['executed']
        assert client.post('/api/incidents/'+incident+'/decision',json={'action':'approve','option_id':'B'}).status_code == 401
        response = client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'reject','feedback':'cancel AK-201'})
        assert response.status_code == 200
        assert response.json()['options'][1]['cancel_ids'] == ['AK-201']
        response = client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'approve','option_id':'B'})
        assert response.status_code == 200
        state = response.json()
        assert state['status'] == 'done' and state['notifications'] and state['work_orders']
        assert client.post('/api/incidents/'+incident+'/decision',headers=auth,json={'action':'approve','option_id':'B'}).status_code == 409
        note = next(n for n in state['notifications'] if n['alternatives'])
        assert client.post('/api/rebooking',json={'incident_id':incident,'passenger_id':note['passenger_id'],'vehicle_id':note['alternatives'][0]}).status_code == 200
        assert client.post('/api/rebooking',json={'incident_id':incident,'passenger_id':note['passenger_id'],'vehicle_id':note['alternatives'][0]}).status_code == 409
        response = client.post('/api/incidents/'+incident+'/replan',headers=auth)
        assert response.status_code == 200
        assert response.json()['status'] == 'awaiting_approval'
        assert len(response.json()['impact']['affected_vehicles']) == 3
