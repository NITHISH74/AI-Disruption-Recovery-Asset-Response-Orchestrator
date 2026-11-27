"""Non-blocking best-effort real weather context, never changes simulated impact."""
import httpx

def get_weather():
    try:
        r=httpx.get('https://api.open-meteo.com/v1/forecast',params={'latitude':13.1986,'longitude':77.7066,'current':'weather_code,wind_speed_10m'},timeout=2)
        r.raise_for_status()
        current=r.json()['current']; code=current['weather_code']; wind=current['wind_speed_10m']
        return {'code':code,'wind_kmh':wind,'storm':code in (95,96,99)}
    except Exception:
        return None

# Forecast advisories are separate from incidents and never trigger execution.
from datetime import datetime, timedelta, timezone
HUBS={'BLR':(13.1986,77.7066),'BOM':(19.0896,72.8656),'DEL':(28.5562,77.1)}

def forecast_watches(hub, payload, now=None):
    now=now or datetime.now(timezone.utc)
    data=payload.get('hourly',{}); watches=[]
    for time,code,wind in zip(data.get('time',[]),data.get('weather_code',[]),data.get('wind_speed_10m',[])):
        try:
            at=datetime.fromisoformat(time.replace('Z','+00:00'))
            if at.tzinfo is None: at=at.replace(tzinfo=timezone.utc)
            hours=(at-now).total_seconds()/3600
            if not 0<=hours<=12: continue
            reason='thunderstorm' if code in (95,96,99) else 'heavy rain' if code in (65,67,82) else 'strong wind' if wind is not None and wind>40 else None
            if reason:
                watches.append({'id':hub,'hub':hub,'reason':reason,'wind_kmh':wind,'expected_at':at.isoformat(),'hours':round(hours,1),'source':'forecast','message':f"Early Warning: {reason} forecast at {hub} in ~{round(hours)}h (winds {wind} km/h) — pre-position crews recommended"})
        except (ValueError,TypeError): continue
    return watches[:1]

def poll_forecasts():
    watches=[]
    for hub,(lat,lon) in HUBS.items():
        try:
            response=httpx.get('https://api.open-meteo.com/v1/forecast',params={'latitude':lat,'longitude':lon,'hourly':'weather_code,wind_speed_10m','forecast_days':2,'timezone':'UTC'},timeout=2)
            response.raise_for_status(); watches.extend(forecast_watches(hub,response.json()))
        except Exception: continue
    return watches
