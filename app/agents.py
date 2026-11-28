"""LLM-assisted incident roles with deterministic tool evidence and fail-closed validation.

LLMs never execute tools directly: each role invokes its allowlisted read/calculation
functions, then proposes bounded JSON. Only validated outputs enter incident state.
"""
import json, os
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from . import llm

class StrictOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)

class DetectorOutput(StrictOutput):
    event_type: Literal['storm','signal','engine','crew_noshow']
    severity: Literal['medium','high']
    affected_asset: str = Field(max_length=80)
    confidence: float = Field(ge=0,le=1)
    reasoning: Literal['reported_disruption','schedule_impact']

class ImpactOutput(StrictOutput):
    affected_vehicles: list[str]
    pax_at_risk: int = Field(ge=0)
    missed_connections: int = Field(ge=0)
    crew_risks: list[str]
    priority_pax: list[str]
    cascade_narrative: Literal['connections_and_crew','passenger_disruption','no_impact']

class PlannerOutput(StrictOutput):
    recommended_option: Literal['A','B','C']
    reasoning: Literal['minimize_cost','protect_passengers','operational_resilience']
    trade_offs: list[Literal['cost','passengers','delay','crew']] = Field(max_length=4)
    risks: list[Literal['capacity','crew','delay','none']] = Field(max_length=4)
    confidence_adjustment: int = Field(ge=-10,le=10)

class BaseAgent:
    def __init__(self,name,role,tools,output_schema,validator):
        self.name=name; self.role=role; self.tools=tools; self.output_schema=output_schema; self.validator=validator
    def validate(self,raw,context):
        try:
            result=self.output_schema.model_validate_json(raw)
            return self.validator(result,context)
        except (ValueError,TypeError,KeyError):
            return False
    def run(self,inputs,incident_id):
        # No model call or accounting writes are needed when providers are absent.
        if not any(os.getenv(key) for key in ('GEMINI_API_KEY','GROQ_API_KEY')): return None
        # The same exact evidence is checked before caching and again before use.
        system=('You are the '+self.role+'. Analyze the supplied deterministic tool results. '
                'Return exactly one JSON object, no markdown, following this JSON schema: '
                +json.dumps(self.output_schema.model_json_schema(),separators=(',',':')))
        raw=llm.get_llm(system,json.dumps(inputs,sort_keys=True,separators=(',',':')), '',
                        incident_id=incident_id,agent=self.name,
                        validator=lambda value:self.validate(value,inputs),free_only=True)
        if not raw or not self.validate(raw,inputs): return None
        return self.output_schema.model_validate_json(raw)


def detector_agent():
    def valid(o,c):
        # The synthetic feed fixes event type, asset and severity. Never let the
        # model change the source event or override these rule-derived values.
        return (o.event_type==c['event_type'] and o.affected_asset==c['asset']
                and c['asset_exists'] and o.severity==c['severity']
                and (o.reasoning!='schedule_impact' or c['services_in_window']>0))
    return BaseAgent('detector','disruption detector',('get_asset_status','get_schedule_window'),DetectorOutput,valid)


def impact_agent():
    def valid(o,c):
        g=c['ground_truth']
        return (o.affected_vehicles==g['affected_vehicles'] and o.pax_at_risk==g['pax_at_risk']
                and o.missed_connections==g['missed_connections'] and o.crew_risks==g['crew_risks']
                and o.priority_pax==g['priority_pax']
                and (o.cascade_narrative!='no_impact' or not (o.pax_at_risk or o.affected_vehicles))
                and (o.cascade_narrative!='passenger_disruption' or o.pax_at_risk>0)
                and (o.cascade_narrative!='connections_and_crew' or (o.missed_connections>0 and bool(o.crew_risks))))
    return BaseAgent('impact','impact analyst',('compute_cascade','get_bookings','get_crew','get_connections'),ImpactOutput,valid)


def planner_agent():
    def valid(o,c):
        chosen=next((v for v in c['options'] if v['option_id']==o.recommended_option),None)
        if not (chosen and chosen['crew_safe'] and chosen['rules_compliant']): return False
        if o.reasoning=='minimize_cost' and chosen['cost_usd'] != min(v['cost_usd'] for v in c['options'] if v['crew_safe'] and v['rules_compliant']): return False
        return True
    return BaseAgent('planner','recovery planner',('generate_options','cost_options','check_crew_legality','check_seats'),PlannerOutput,valid)

# Explicit allowlist. These functions run in Python, never as arbitrary LLM code.
from .engine import compute_cascade, generate_options, cost_options

def get_asset_status(event,vehicles):
    asset=event['asset_id']
    matching=[v for v in vehicles if v.get('asset_id')==asset or v.get('id')==asset]
    return {'asset_exists':bool(matching),'matching_services':len(matching)}

def get_schedule_window(event,vehicles):
    from datetime import datetime
    start=datetime.fromisoformat(event['window_start'].replace('Z','+00:00'))
    end=datetime.fromisoformat(event['expected_end'].replace('Z','+00:00'))
    return sum(start<=datetime.fromisoformat(v['departure'].replace('Z','+00:00'))<=end for v in vehicles if v.get('status')!='canceled')

def get_bookings(bookings): return bookings
def get_crew(crew): return crew
def get_connections(impact): return impact['missed_connections']
def check_crew_legality(option): return bool(option['crew_safe'])
def check_seats(option): return bool(option['rules_compliant'])
TOOL_REGISTRY={'get_asset_status':get_asset_status,'get_schedule_window':get_schedule_window,
               'compute_cascade':compute_cascade,'get_bookings':get_bookings,'get_crew':get_crew,
               'get_connections':get_connections,'generate_options':generate_options,'cost_options':cost_options,
               'check_crew_legality':check_crew_legality,'check_seats':check_seats}
