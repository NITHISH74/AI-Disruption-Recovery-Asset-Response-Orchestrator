-- Phase 6 priority, historical outcomes, and non-incident watches.
ALTER TABLE public.passengers ADD COLUMN IF NOT EXISTS family boolean NOT NULL DEFAULT false;
ALTER TABLE public.passengers ADD COLUMN IF NOT EXISTS elderly boolean NOT NULL DEFAULT false;
ALTER TABLE public.agent_log ALTER COLUMN incident_id DROP NOT NULL;
CREATE TABLE IF NOT EXISTS public.incident_outcomes (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, incident_id text NOT NULL REFERENCES public.incidents(id), plan_version integer NOT NULL DEFAULT 1,
 event_type text NOT NULL, chosen_option text NOT NULL, estimated_cost numeric NOT NULL,
 actual_pax_moved integer NOT NULL, resolution_min numeric NOT NULL, success boolean NOT NULL, at timestamptz NOT NULL DEFAULT now(), UNIQUE(incident_id,plan_version));
CREATE INDEX IF NOT EXISTS incident_outcomes_learning_idx ON public.incident_outcomes(event_type,chosen_option);
CREATE TABLE IF NOT EXISTS public.weather_watches(id text PRIMARY KEY, data jsonb NOT NULL);
ALTER TABLE public.incident_outcomes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.weather_watches ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION public.syncria_execute_plan(p_incident text, p_option text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE st jsonb; opt jsonb; cancel_ids text[]; affected text[]; v record; b record; target record; changes jsonb := '[]'::jsonb; result jsonb; event_asset text; event_duration numeric; work_order jsonb; version integer; current_status text;
BEGIN
 SELECT i.state,i.status INTO st,current_status FROM public.incidents i WHERE i.id=p_incident FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Incident not found'; END IF;
 version=coalesce((st->>'plan_version')::integer,1);
 SELECT er.result INTO result FROM public.execution_results er WHERE er.incident_id=p_incident AND er.plan_version=version;
 IF FOUND THEN RETURN result; END IF;
 IF current_status<>'awaiting_approval' THEN RAISE EXCEPTION 'Incident is not awaiting approval'; END IF;
 IF NOT EXISTS (SELECT 1 FROM public.decisions d WHERE d.incident_id=p_incident AND d.action='approve' AND d.option_id=p_option AND d.plan_version=version) THEN RAISE EXCEPTION 'Approval required for this version'; END IF;
 SELECT x INTO opt FROM jsonb_array_elements(st->'options') x WHERE x->>'option_id'=p_option LIMIT 1;
 IF opt IS NULL THEN RAISE EXCEPTION 'Unknown option'; END IF;
 SELECT COALESCE(array_agg(x),ARRAY[]::text[]) INTO cancel_ids FROM jsonb_array_elements_text(COALESCE(opt->'cancel_ids','[]'::jsonb)) x;
 SELECT COALESCE(array_agg(x),ARRAY[]::text[]) INTO affected FROM jsonb_array_elements_text(st->'impact'->'affected_vehicles') x;
 IF NOT cancel_ids <@ affected THEN RAISE EXCEPTION 'Cancellation not in impact'; END IF;
 UPDATE public.vehicles SET status='canceled' WHERE id=ANY(cancel_ids);
 FOR b IN SELECT bk.id,bk.passenger_id,bk.vehicle_id,ve.mode,ve.from_hub,ve.to_hub,ve.dep_at FROM public.bookings bk JOIN public.vehicles ve ON ve.id=bk.vehicle_id WHERE bk.vehicle_id=ANY(cancel_ids) AND bk.status<>'canceled'
   ORDER BY (CASE bk.tier WHEN 'medic' THEN 100 WHEN 'gold' THEN 80 WHEN 'silver' THEN 60 ELSE 40 END
     + CASE WHEN EXISTS (SELECT 1 FROM public.vehicles cv WHERE cv.id=bk.connecting_vehicle_id AND cv.dep_at>=ve.arr_at AND cv.dep_at<ve.arr_at+interval '60 minutes') THEN 20 ELSE 0 END
     + CASE WHEN EXISTS (SELECT 1 FROM public.passengers p WHERE p.id=bk.passenger_id AND (p.family OR p.elderly OR coalesce(p.special,'') ~* '(family|elderly)')) THEN 10 ELSE 0 END) DESC,bk.id FOR UPDATE OF bk LOOP
   SELECT ve.id,ve.mode INTO target FROM public.vehicles ve WHERE NOT (ve.id=ANY(affected)) AND ve.status='scheduled' AND ve.available_seats>0
   AND replace(ve.from_hub,'-R','')=replace(b.from_hub,'-R','') AND replace(ve.to_hub,'-R','')=replace(b.to_hub,'-R','') AND ve.dep_at>b.dep_at
   ORDER BY (ve.mode=b.mode) DESC,ve.dep_at,ve.id LIMIT 1 FOR UPDATE;
   IF FOUND THEN
     UPDATE public.vehicles SET available_seats=available_seats-1 WHERE id=target.id;
     UPDATE public.bookings SET vehicle_id=target.id,status='rebooked',rebooked_to_vehicle_id=target.id WHERE id=b.id;
     changes=changes||jsonb_build_array(jsonb_build_object('booking_id',b.id,'passenger_id',b.passenger_id,'from_vehicle',b.vehicle_id,'to_vehicle',target.id,'mode',target.mode));
   ELSE
     UPDATE public.bookings SET status='canceled' WHERE id=b.id;
     changes=changes||jsonb_build_array(jsonb_build_object('booking_id',b.id,'passenger_id',b.passenger_id,'from_vehicle',b.vehicle_id,'to_vehicle',NULL,'mode',NULL));
   END IF;
 END LOOP;
 IF p_option='C' THEN
   UPDATE public.vehicles SET
     arr_at=arr_at + CASE WHEN st->'event'->>'type'='signal' THEN interval '45 minutes' WHEN array_position(affected,id)>(cardinality(affected)+1)/2 THEN interval '1 hour' ELSE interval '0' END,
     dep_at=dep_at + CASE WHEN st->'event'->>'type'='signal' THEN interval '45 minutes' WHEN array_position(affected,id)>(cardinality(affected)+1)/2 THEN interval '1 hour' ELSE interval '0' END,
     asset_id=CASE WHEN st->'event'->>'type'='storm' AND mode='flight' THEN 'RW-35L' ELSE asset_id END,
     status='delayed' WHERE id=ANY(affected) AND status='scheduled';
 ELSE
   UPDATE public.vehicles SET arr_at=arr_at+GREATEST((st->'event'->>'expected_end')::timestamptz-dep_at,interval '0'), dep_at=GREATEST(dep_at,(st->'event'->>'expected_end')::timestamptz), status='delayed' WHERE id=ANY(affected) AND status='scheduled';
 END IF;
 event_asset=st->'event'->>'asset_id'; event_duration=(st->'event'->>'duration_h')::numeric;
 IF EXISTS (SELECT 1 FROM public.assets WHERE id=event_asset) THEN
   INSERT INTO public.work_orders(id,incident_id,asset_id,title,diagnosis,priority,window_start,window_end,eta_hours,status)
   VALUES ('WO-'||p_incident,p_incident,event_asset,'Inspect affected asset','Inspect and restore following simulated disruption','high',now(),now()+event_duration*interval '1 hour',event_duration,'open') ON CONFLICT(id) DO NOTHING;
   work_order=jsonb_build_object('id','WO-'||p_incident,'incident_id',p_incident,'asset',event_asset,'diagnosis','Inspect and restore following simulated disruption','priority','High','eta',event_duration||'h','status','Open');
 END IF;
 result=jsonb_build_object('cancelled',to_jsonb(cancel_ids),'changes',changes,'work_order',work_order);
 INSERT INTO public.execution_results (incident_id,plan_version,result) VALUES(p_incident,version,result);
 INSERT INTO public.incident_outcomes(incident_id,plan_version,event_type,chosen_option,estimated_cost,actual_pax_moved,resolution_min,success)
 SELECT p_incident,version,st->'event'->>'type',p_option,coalesce((opt->>'cost_usd')::numeric,0),
 (SELECT count(*) FROM jsonb_array_elements(changes) x WHERE x->>'to_vehicle' IS NOT NULL),
 greatest(0,extract(epoch from (now()-(st->>'created_at')::timestamptz))/60),
 NOT EXISTS (SELECT 1 FROM jsonb_array_elements(changes) x WHERE x->>'to_vehicle' IS NULL)
 ON CONFLICT(incident_id,plan_version) DO NOTHING;
 UPDATE public.recovery_plans SET selected=(option_id=p_option) WHERE incident_id=p_incident AND plan_version=version;
 UPDATE public.incidents SET status='executing',state=jsonb_set(state,'{status}','"executing"'::jsonb) WHERE id=p_incident;
 RETURN result;
END $$;
REVOKE ALL ON FUNCTION public.syncria_execute_plan(text,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.syncria_execute_plan(text,text) TO service_role;

