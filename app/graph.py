"""Deterministic LangGraph conductor with an explicit human interrupt."""
import logging, os
from typing import TypedDict, Any
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from langgraph.checkpoint.memory import MemorySaver

class OpsState(TypedDict, total=False):
    incident_id: str
    id: str
    event: dict
    disruption: dict | None
    impact: dict | None
    options: list[dict]
    decision: dict | None
    executed: Any
    notifications: list[dict]
    work_order: dict | None
    agent_log: list[dict]
    logs: list[dict]
    status: str
    created_at: str
    work_orders: list[dict]
    affected_bookings: list[dict]
    original_vehicles: list[dict]
    reject_count: int
    plan_version: int
    previous_pax: int
    savings: dict
    restore: bool

_connection=None

def make_checkpointer():
    global _connection
    if os.getenv('CHECKPOINTER','memory').lower() == 'postgres':
        try:
            import psycopg
            from langgraph.checkpoint.postgres import PostgresSaver
            url=os.environ['DATABASE_URL']
            _connection=psycopg.connect(url,autocommit=True,connect_timeout=4,prepare_threshold=None)
            saver=PostgresSaver(_connection); saver.setup()
            return saver
        except Exception as e:
            logging.getLogger(__name__).warning('Postgres checkpoint unavailable (%s); using in-memory checkpoints', type(e).__name__)
    return MemorySaver()

def build_graph(detect, impact, plan, execute, notify):
    def detect_node(state):
        if not state.get('restore'): detect(state)
        return state
    def impact_node(state):
        if not state.get('restore'): impact(state)
        return state
    def plan_node(state):
        if not state.get('restore'): plan(state)
        state['restore']=False
        return state
    def approve_node(state):
        decision=interrupt({'options':state['options'],'incident_id':state['id']})
        state['decision']=decision
        return state
    def route(state): return 'execute' if state['decision']['action']=='approve' else 'plan'
    def execute_node(state): execute(state); return state
    def notify_node(state): notify(state); return state
    g=StateGraph(OpsState)
    for name,node in [('detect',detect_node),('impact',impact_node),('plan',plan_node),('approve',approve_node),('execute',execute_node),('notify',notify_node)]: g.add_node(name,node)
    g.add_edge(START,'detect');g.add_edge('detect','impact');g.add_edge('impact','plan');g.add_edge('plan','approve')
    g.add_conditional_edges('approve',route,{'execute':'execute','plan':'plan'})
    g.add_edge('execute','notify');g.add_edge('notify',END)
    return g.compile(checkpointer=make_checkpointer())
