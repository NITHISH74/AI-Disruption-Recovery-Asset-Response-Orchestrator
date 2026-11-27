-- Versioned recovery plans and execution; preserves all previous records.
ALTER TABLE public.recovery_plans ADD COLUMN IF NOT EXISTS plan_version integer NOT NULL DEFAULT 1;
ALTER TABLE public.recovery_plans ADD COLUMN IF NOT EXISTS superseded boolean NOT NULL DEFAULT false;
ALTER TABLE public.recovery_plans DROP CONSTRAINT IF EXISTS recovery_plans_incident_id_option_id_key;
CREATE UNIQUE INDEX IF NOT EXISTS recovery_plans_version_unique ON public.recovery_plans(incident_id,plan_version,option_id);
ALTER TABLE public.decisions ADD COLUMN IF NOT EXISTS plan_version integer NOT NULL DEFAULT 1;
ALTER TABLE public.execution_results ADD COLUMN IF NOT EXISTS plan_version integer NOT NULL DEFAULT 1;
ALTER TABLE public.execution_results DROP CONSTRAINT IF EXISTS execution_results_pkey;
ALTER TABLE public.execution_results ADD PRIMARY KEY (incident_id,plan_version);
ALTER TABLE public.notifications ADD COLUMN IF NOT EXISTS plan_version integer NOT NULL DEFAULT 1;
DROP INDEX IF EXISTS public.notifications_initial_unique;
CREATE UNIQUE INDEX IF NOT EXISTS notifications_initial_version_unique ON public.notifications(incident_id,passenger_id,plan_version) WHERE notification_kind='initial';
ALTER TABLE public.work_orders ADD COLUMN IF NOT EXISTS assigned_crew text;

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
 INSERT INTO public.execution_results (incident_id,plan_version,result) VALUES(p_incident,version,result);
 UPDATE public.recovery_plans SET selected=(option_id=p_option) WHERE incident_id=p_incident AND plan_version=version;
 UPDATE public.incidents SET status='executing',state=jsonb_set(state,'{status}','"executing"'::jsonb) WHERE id=p_incident;
 RETURN result;
END $$;
REVOKE ALL ON FUNCTION public.syncria_execute_plan(text,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.syncria_execute_plan(text,text) TO service_role;

-- Persist the early-repair feed event and recovery ETA atomically.
CREATE OR REPLACE FUNCTION public.syncria_replan_event(p_incident text,p_expected_end timestamptz)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE st jsonb; incident_status text; asset text; old_end timestamptz; window_start timestamptz;
BEGIN
 SELECT state,status INTO st,incident_status FROM public.incidents WHERE id=p_incident FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Incident not found'; END IF;
 IF incident_status NOT IN ('awaiting_approval','done') THEN RAISE EXCEPTION 'Incident not ready for replanning'; END IF;
 asset=st->'event'->>'asset_id'; old_end=(st->'event'->>'expected_end')::timestamptz; window_start=(st->'event'->>'window_start')::timestamptz;
 IF p_expected_end>old_end OR p_expected_end<window_start THEN RAISE EXCEPTION 'Invalid repair time'; END IF;
 INSERT INTO public.events(id,type,asset_id,severity,source,started_at,expected_end,status)
 VALUES(p_incident||'-early','repair_early',asset,'low','chaos_console',now(),p_expected_end,'recovered') ON CONFLICT(id) DO NOTHING;
 UPDATE public.events SET expected_end=p_expected_end WHERE id=p_incident;
 UPDATE public.assets SET expected_repair_end=p_expected_end WHERE id=asset;
 RETURN true;
END $$;
REVOKE ALL ON FUNCTION public.syncria_replan_event(text,timestamptz) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.syncria_replan_event(text,timestamptz) TO service_role;
