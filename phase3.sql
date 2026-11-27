-- Phase 3: atomic execution and rebooking. Run once in the Supabase SQL editor.
ALTER TABLE public.notifications ADD COLUMN IF NOT EXISTS booking_id text REFERENCES public.bookings(id);
ALTER TABLE public.notifications ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE public.notifications ADD COLUMN IF NOT EXISTS notification_kind text NOT NULL DEFAULT 'initial';
CREATE UNIQUE INDEX IF NOT EXISTS notifications_initial_unique ON public.notifications(incident_id,passenger_id) WHERE notification_kind='initial';
CREATE TABLE IF NOT EXISTS public.execution_results (incident_id text PRIMARY KEY REFERENCES public.incidents(id), result jsonb NOT NULL);
ALTER TABLE public.execution_results ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION public.syncria_execute_plan(p_incident text, p_option text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE st jsonb; opt jsonb; cancel_ids text[]; affected text[]; v record; b record; target record; changes jsonb := '[]'::jsonb; result jsonb; event_asset text; event_duration numeric; work_order jsonb;
BEGIN
 SELECT er.result INTO result FROM public.execution_results er WHERE er.incident_id=p_incident;
 IF FOUND THEN RETURN result; END IF;
 SELECT i.state INTO st FROM public.incidents i WHERE i.id=p_incident AND i.status='awaiting_approval' FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Incident is not awaiting approval'; END IF;
 -- Recheck after the incident lock: a concurrent execution may have completed.
 SELECT er.result INTO result FROM public.execution_results er WHERE er.incident_id=p_incident;
 IF FOUND THEN RETURN result; END IF;
 IF NOT EXISTS (SELECT 1 FROM public.decisions d WHERE d.incident_id=p_incident AND d.action='approve' AND d.option_id=p_option) THEN RAISE EXCEPTION 'Approval required'; END IF;
 SELECT x INTO opt FROM jsonb_array_elements(st->'options') x WHERE x->>'option_id'=p_option LIMIT 1;
 IF opt IS NULL THEN RAISE EXCEPTION 'Unknown option'; END IF;
 SELECT COALESCE(array_agg(x),ARRAY[]::text[]) INTO cancel_ids FROM jsonb_array_elements_text(COALESCE(opt->'cancel_ids','[]'::jsonb)) x;
 SELECT COALESCE(array_agg(x),ARRAY[]::text[]) INTO affected FROM jsonb_array_elements_text(st->'impact'->'affected_vehicles') x;
 IF NOT cancel_ids <@ affected THEN RAISE EXCEPTION 'Cancellation not in impact'; END IF;
 UPDATE public.vehicles SET status='canceled' WHERE id=ANY(cancel_ids);
 FOR b IN SELECT bk.id,bk.passenger_id,bk.vehicle_id,ve.mode,ve.from_hub,ve.to_hub,ve.dep_at FROM public.bookings bk JOIN public.vehicles ve ON ve.id=bk.vehicle_id WHERE bk.vehicle_id=ANY(cancel_ids) AND bk.status<>'canceled' ORDER BY bk.id FOR UPDATE OF bk LOOP
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
 INSERT INTO public.execution_results VALUES(p_incident,result);
 UPDATE public.recovery_plans SET selected=(option_id=p_option) WHERE incident_id=p_incident;
 UPDATE public.incidents SET status='executing',state=jsonb_set(state,'{status}','"executing"'::jsonb) WHERE id=p_incident;
 RETURN result;
END $$;
REVOKE ALL ON FUNCTION public.syncria_execute_plan(text,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.syncria_execute_plan(text,text) TO service_role;

CREATE OR REPLACE FUNCTION public.syncria_rebook(p_incident text,p_passenger text,p_booking text,p_vehicle text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE b record; oldv record; newv record; n record;
BEGIN
 SELECT * INTO n FROM public.notifications WHERE incident_id=p_incident AND passenger_id=p_passenger AND booking_id=p_booking ORDER BY id DESC LIMIT 1;
 IF NOT FOUND THEN RAISE EXCEPTION 'Booking not associated with passenger and incident'; END IF;
 SELECT * INTO b FROM public.bookings WHERE id=p_booking AND passenger_id=p_passenger FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Booking not found'; END IF;
 -- Lock vehicles in stable id order to avoid opposing rebook deadlocks.
 PERFORM 1 FROM public.vehicles WHERE id IN (b.vehicle_id,p_vehicle) ORDER BY id FOR UPDATE;
 SELECT * INTO oldv FROM public.vehicles WHERE id=b.vehicle_id;
 SELECT * INTO newv FROM public.vehicles WHERE id=p_vehicle;
 IF newv.id IS NULL OR newv.id=oldv.id OR newv.status<>'scheduled' OR newv.available_seats<=0 OR replace(newv.from_hub,'-R','')<>replace(oldv.from_hub,'-R','') OR replace(newv.to_hub,'-R','')<>replace(oldv.to_hub,'-R','') OR newv.dep_at<=oldv.dep_at THEN RAISE EXCEPTION 'Alternative unavailable'; END IF;
 IF b.status='rebooked' THEN UPDATE public.vehicles SET available_seats=available_seats+1 WHERE id=oldv.id; END IF;
 UPDATE public.vehicles SET available_seats=available_seats-1 WHERE id=newv.id;
 UPDATE public.bookings SET vehicle_id=newv.id,status='rebooked',rebooked_to_vehicle_id=newv.id WHERE id=b.id;
 INSERT INTO public.notifications(incident_id,passenger_id,booking_id,channel,lang,body,status,sent_at,notification_kind)
 VALUES(p_incident,p_passenger,p_booking,'in_app',n.lang,'Rebooking confirmed on '||newv.id||' at '||newv.dep_at::text||'.','sent',now(),'confirmation');
 RETURN jsonb_build_object('status','confirmed','vehicle_id',newv.id);
END $$;
REVOKE ALL ON FUNCTION public.syncria_rebook(text,text,text,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.syncria_rebook(text,text,text,text) TO service_role;
