import os, subprocess, sys
from pathlib import Path
from fastapi.testclient import TestClient
from app import main

ROOT=Path(__file__).resolve().parents[1]

def test_health_reports_database_and_llm_configuration(monkeypatch):
    for key in ('GEMINI_API_KEY','GROQ_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY'):
        monkeypatch.delenv(key,raising=False)
    response=TestClient(main.app).get('/health')
    assert response.status_code==200
    assert response.json()['db']['status']=='reachable'
    assert response.json()['llm']['status']=='template_only'
    assert response.json()['llm']['live_verified'] is False
    monkeypatch.setenv('GEMINI_API_KEY','example')
    response=TestClient(main.app).get('/health')
    assert response.json()['llm']['status']=='configured'
    assert 'example' not in response.text

def test_cors_preflight_and_auth_failure_are_cors_accessible(tmp_path):
    env={**os.environ,'DB_BACKEND':'sqlite','SYNCRIA_DB':str(tmp_path/'cors.db'),'ALLOWED_ORIGINS':'https://demo.example','ENABLE_WEATHER':'0','REQUIRE_CONTROLLER_LOGIN':'1'}
    program='''from fastapi.testclient import TestClient
from app.main import app
c=TestClient(app)
r=c.options('/api/incidents',headers={'Origin':'https://demo.example','Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'authorization,content-type'})
assert r.status_code==200 and r.headers['access-control-allow-origin']=='https://demo.example'
r=c.get('/api/incidents/latest',headers={'Origin':'https://demo.example'})
assert r.status_code==401 and r.headers['access-control-allow-origin']=='https://demo.example'
r=c.options('/api/incidents',headers={'Origin':'https://evil.example','Access-Control-Request-Method':'POST'})
assert r.status_code==400 and 'access-control-allow-origin' not in r.headers
'''
    subprocess.run([sys.executable,'-c',program],cwd=ROOT,env=env,check=True,capture_output=True)

def test_offline_pre_demo_marks_live_checks_incomplete():
    result=subprocess.run([sys.executable,'scripts/pre_demo_check.py'],cwd=ROOT,text=True,capture_output=True)
    assert result.returncode==2
    assert 'PASS: Cache hit' in result.stdout and 'PASS: Budget guard' in result.stdout
    assert 'PASS: Controller login (offline)' in result.stdout and 'SKIP: Gemini live' in result.stdout
