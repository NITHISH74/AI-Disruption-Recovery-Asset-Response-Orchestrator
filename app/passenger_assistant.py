"""Rule-based answers in supported passenger languages; no operational LLM decisions."""
MESSAGES={
 'en':{'where':'Your current booking is on {vehicle}, departing {departure} from {origin} to {destination}.','change':'Use Change my flight / train below to view available seats.','fixed':'Estimated recovery is {eta}.','what':'The disruption is {event}. Check your notifications for the recovery plan.','unknown':'I can help with your booking, alternatives, the disruption, and estimated recovery.'},
 'hi':{'where':'आपकी वर्तमान बुकिंग {vehicle} पर है, {origin} से {destination} के लिए प्रस्थान {departure} है।','change':'उपलब्ध सीटें देखने के लिए नीचे उड़ान या ट्रेन बदलें चुनें।','fixed':'अनुमानित सुधार समय {eta} है।','what':'व्यवधान का कारण {event} है। योजना के लिए अपनी सूचनाएं देखें।','unknown':'मैं बुकिंग, विकल्प, व्यवधान और सुधार समय में मदद कर सकता हूँ।'},
 'ta':{'where':'உங்கள் தற்போதைய பதிவு {vehicle}, {origin} முதல் {destination} வரை புறப்படும் நேரம் {departure}.','change':'கிடைக்கும் இருக்கைகளைக் காண கீழே விமானம் அல்லது ரயிலை மாற்று என்பதைத் தேர்ந்தெடுக்கவும்.','fixed':'மீட்பு மதிப்பீடு {eta}.','what':'இடையூறு {event}. மீட்புத் திட்டத்திற்கு அறிவிப்புகளைப் பார்க்கவும்.','unknown':'உங்கள் பதிவு, மாற்றுகள், இடையூறு மற்றும் மீட்பு நேரம் பற்றி உதவ முடியும்.'},
 'es':{'where':'Su reserva actual es {vehicle}, salida {departure} de {origin} a {destination}.','change':'Use Cambiar mi vuelo o tren abajo para ver asientos disponibles.','fixed':'La recuperación estimada es {eta}.','what':'La incidencia es {event}. Consulte sus notificaciones para ver el plan.','unknown':'Puedo ayudarle con su reserva, alternativas, incidencia y recuperación.'}}

def intent_for(text):
    q=text.lower().strip()
    if any(x in q for x in ('where is','where\'s','where my','कहाँ','எங்கே','dónde','donde')): return 'where'
    if any(x in q for x in ('change','rebook','alternative','बदल','மாற்ற','cambiar','alternativa')): return 'change'
    if any(x in q for x in ('fixed','repair','recover','when','कब','சரி','எப்போது','cuándo','cuando','recuper')): return 'fixed'
    if any(x in q for x in ('what happened','why','cause','क्या हुआ','क्यों','என்ன நடந்த','ஏன்','qué pasó','que paso','por qué','por que')): return 'what'
    return 'unknown'

def answer(text,language,booking,vehicle,event):
    language=language if language in MESSAGES else 'en'; intent=intent_for(text)
    data={'vehicle':booking.get('vehicle_id','—'),'departure':vehicle.get('departure','—'),'origin':vehicle.get('origin','—'),'destination':vehicle.get('destination','—'),'eta':event.get('expected_end','—'),'event':event.get('type','disruption').replace('_',' ')}
    return intent,MESSAGES[language][intent].format(**data)
