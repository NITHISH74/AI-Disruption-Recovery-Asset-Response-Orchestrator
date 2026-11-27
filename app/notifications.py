"""Deterministic multilingual messages. One notification per affected passenger."""
from collections import Counter
from .intelligence import priority_ranks

CAUSES = {
 'en': {'storm':'a storm','signal':'a signal failure','engine':'an engine fault','crew_noshow':'a crew absence'},
 'hi': {'storm':'तूफ़ान','signal':'सिग्नल खराबी','engine':'इंजन खराबी','crew_noshow':'कर्मीदल की अनुपस्थिति'},
 'ta': {'storm':'புயல்','signal':'சிக்னல் கோளாறு','engine':'இயந்திரக் கோளாறு','crew_noshow':'பணியாளர் வராதது'},
 'es': {'storm':'una tormenta','signal':'una avería de señal','engine':'una avería del motor','crew_noshow':'una ausencia de tripulación'} }

def make_notifications(state, original_bookings, original_vehicles, current_vehicles, result):
    originals={v['id']:v for v in original_vehicles}; current={v['id']:v for v in current_vehicles}
    changes={c['booking_id']:c for c in result['changes']}; seen=set(); notes=[]
    ranks=priority_ranks(original_bookings,original_vehicles)
    # Prioritize canceled/unassigned journeys if a traveler has multiple bookings.
    ordered=sorted(original_bookings,key=lambda b:(b['booking_id'] not in changes, bool(changes.get(b['booking_id'],{}).get('to_vehicle')), b['booking_id']))
    for b in ordered:
        if b['vehicle_id'] not in state['impact']['affected_vehicles'] or b['id'] in seen: continue
        seen.add(b['id']); old=originals[b['vehicle_id']]; change=changes.get(b['booking_id']); target=current.get(change.get('to_vehicle')) if change else None
        candidates=sorted((v for v in current_vehicles if v['id'] not in state['impact']['affected_vehicles'] and v['status']=='scheduled' and v['available_seats']>0 and v['origin']==old['origin'] and v['destination']==old['destination'] and v['departure']>(target or old)['departure']),key=lambda v:(v['departure'],v['id']))
        # Ensure a rail choice is visible when one exists.
        chosen=candidates[:3]; rail=next((v for v in candidates if v['mode']=='train'),None)
        if rail and rail not in chosen: chosen=chosen[:2]+[rail]
        alternatives=[v['id'] for v in chosen]; alt=', '.join(f"{v['id']} ({v['mode']}, {v['departure']})" for v in chosen) or '—'
        lang=b.get('language','en'); lang=lang if lang in CAUSES else 'en'; cause=CAUSES[lang][state['event']['type']]; eta=state['event']['expected_end']
        if lang=='hi':
            outcome=(f"आपको {target['id']} पर {target['departure']} के लिए पुनः बुक किया गया है।" if target else ('अभी कोई सीट आवंटित नहीं हुई है।' if change else 'आपकी सेवा के लिए देरी की योजना स्वीकृत है।'))
            parts=[f"नमस्ते {b['name']}, आपकी सेवा {old['id']} {'रद्द है' if change else 'विलंबित है'}।",f"कारण: {cause}।",outcome,f"आप स्वयं विकल्प चुन सकते हैं (रेल सहित यदि उपलब्ध): {alt}।",f"अनुमानित सुधार: {eta}।"]
        elif lang=='ta':
            outcome=(f"உங்களுக்கு {target['id']} {target['departure']} சேவையில் மாற்றுப் பதிவு செய்யப்பட்டது." if target else ('இதுவரை இருக்கை ஒதுக்கப்படவில்லை.' if change else 'தாமதத் திட்டம் அங்கீகரிக்கப்பட்டுள்ளது.'))
            parts=[f"வணக்கம் {b['name']}, உங்கள் சேவை {old['id']} {'ரத்து செய்யப்பட்டது' if change else 'தாமதமானது'}.",f"காரணம்: {cause}.",outcome,f"நீங்கள் மாற்றைத் தேர்வு செய்யலாம் (ரயில் இருந்தால் உட்பட): {alt}.",f"மீட்பு மதிப்பீடு: {eta}."]
        elif lang=='es':
            outcome=(f"Le hemos reubicado en {target['id']} a las {target['departure']}." if target else ('Todavía no tiene un asiento asignado.' if change else 'Se aprobó un plan de demora para su servicio.'))
            parts=[f"Hola {b['name']}, su servicio {old['id']} está {'cancelado' if change else 'retrasado'}.",f"La causa es {cause}.",outcome,f"Puede elegir otra opción, incluido tren si está disponible: {alt}.",f"Recuperación estimada: {eta}."]
        else:
            outcome=(f"We rebooked you on {target['id']} at {target['departure']}." if target else ('No replacement seat has been assigned yet.' if change else 'A delay plan was approved for your service.'))
            parts=[f"Hi {b['name']}, your service {old['id']} is {'canceled' if change else 'delayed'}.",f"The cause is {cause}.",outcome,f"You can choose another option, including rail when available: {alt}.",f"Estimated recovery: {eta}."]
        notes.append({'kind':'initial','priority':ranks[b['booking_id']],'passenger_id':b['id'],'booking_id':b['booking_id'],'name':b['name'],'pnr':b['pnr'],'language':lang,'vehicle_id':old['id'],'rebooked_to':target['id'] if target else None,'body':' '.join(parts),'elements':{'what':parts[0],'why':parts[1],'done':parts[2],'choice':parts[3],'when':parts[4]},'alternatives':alternatives})
    return notes

GREETINGS={'en':'Hello {name}.','hi':'नमस्ते {name}।','ta':'வணக்கம் {name}.','es':'Hola {name}.'}

def greeting_templates(languages, incident_id=None, priority_languages=()):
    """One text-only model call per language, no traveler data or operational facts."""
    from concurrent.futures import ThreadPoolExecutor
    from .llm import get_llm
    def generate(lang):
        fallback=GREETINGS[lang]
        args=('You write short greetings for a transport notification. Return only a greeting, in the requested language, using the literal placeholder {name}; no other placeholders or facts.', f'Language: {lang}. Maximum 12 words.',fallback)
        if incident_id is None: result=get_llm(*args).strip()
        else: result=get_llm(*args,incident_id=incident_id,agent='communicator',priority=lang in priority_languages).strip()
        if result.count('{name}')!=1 or '{' in result.replace('{name}','') or '}' in result.replace('{name}','') or '<' in result or '>' in result or len(result.split())>12 or '\n' in result: return lang,fallback
        return lang,result
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(generate,sorted(set(languages) & set(GREETINGS))))
