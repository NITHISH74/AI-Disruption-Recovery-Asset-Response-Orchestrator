"""Simple controller projections; not a certified crew legality system."""
from datetime import datetime, timedelta, timezone

def crew_monitor(roster,now=None):
    now=now or datetime.now(timezone.utc); result=[]
    for c in roster:
        try:
            start=datetime.fromisoformat(c['duty_start'].replace('Z','+00:00'))
            if start.tzinfo is None: start=start.replace(tzinfo=timezone.utc)
            limit=float(c['duty_limit_hours']); end=start+timedelta(hours=limit)
        except (KeyError,ValueError,TypeError):
            result.append({**c,'used_h':None,'remaining_h':None,'alert':'Unknown duty data','projected_violations':[]});continue
        used=max(0,(now-start).total_seconds()/3600); remaining=(end-now).total_seconds()/3600
        violations=[]
        for a in c.get('assignments',[]):
            try:
                finish=datetime.fromisoformat((a.get('arrival') or a['departure']).replace('Z','+00:00'))
                if finish.tzinfo is None: finish=finish.replace(tzinfo=timezone.utc)
                if finish>=now and finish>end: violations.append({'vehicle_id':a['vehicle_id'],'projected_end':finish.isoformat()})
            except (KeyError,TypeError,ValueError): continue
        alert='Duty limit reached' if remaining<=0 else 'Less than 2 hours remaining' if remaining<2 else 'Projected violation' if violations else None
        result.append({**c,'used_h':round(used,2),'remaining_h':round(remaining,2),'duty_end':end.isoformat(),'alert':alert,'projected_violations':violations})
    return result

def handover_template(states,orders,crew,recent_decisions=None):
    active=[s for s in states if s.get('status') in ('active','awaiting_approval','executing','replanning')]
    recent=states[:10]; warnings=[c for c in crew if c.get('alert')]
    lines=['SHIFT HANDOVER — synthetic demo',f'Active incidents: {len(active)}.']
    lines.extend(f"- {s['id']}: {s['status']} ({s.get('event',{}).get('type','unknown')})" for s in active)
    lines.append('Recent decisions:')
    decisions=[f"- {d['incident_id']}: {d.get('action','unknown')} option {d.get('option_id','—')} ({d.get('at','time unknown')})" for d in (recent_decisions or [])]
    if recent_decisions is None: decisions=[f"- {s['id']}: {s['decision'].get('action','unknown')} option {s['decision'].get('option_id','—')}" for s in recent if isinstance(s.get('decision'),dict)]
    lines.extend(decisions or ['- None recorded in recent incident snapshots.'])
    lines.append(f"Pending approvals: {sum(s.get('status')=='awaiting_approval' for s in active)}.")
    lines.append(f"Crew warnings: {len(warnings)} (simplified duty projection).")
    lines.append(f"Open work orders: {len(orders)}.")
    return '\n'.join(lines)
