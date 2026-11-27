-- Atomic priority-aware coordinated group execution. Run after phase6.sql.
CREATE OR REPLACE FUNCTION public.syncria_execute_plan(p_incident text, p_option text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE st jsonb; opt jsonb; cancel_ids text[]; affected text[]; v record; b record; target record; changes jsonb := '[]'::jsonb; result jsonb; event_asset text; event_duration numeric; work_order jsonb; version integer; current_status text; ordered_ids text[]; grp jsonb; group_no integer; group_ids text[]; group_seen boolean[]; group_whole boolean[]; proposed record; group_count integer; matched_count integer; moved_count integer; first_pos integer; last_pos integer;
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
 -- Freeze priority order before any bookings or seats are changed.
 SELECT coalesce(array_agg(q.id ORDER BY q.score DESC,q.id),ARRAY[]::text[]) INTO ordered_ids FROM (
   SELECT bk.id, (CASE bk.tier WHEN 'medic' THEN 100 WHEN 'gold' THEN 80 WHEN 'silver' THEN 60 ELSE 40 END
     + CASE WHEN EXISTS (SELECT 1 FROM public.vehicles cv WHERE cv.id=bk.connecting_vehicle_id AND cv.dep_at>=ve.arr_at AND cv.dep_at<ve.arr_at+interval '60 minutes') THEN 20 ELSE 0 END
     + CASE WHEN EXISTS (SELECT 1 FROM public.passengers p WHERE p.id=bk.passenger_id AND (p.family OR p.elderly OR coalesce(p.special,'') ~* '(family|elderly)')) THEN 10 ELSE 0 END) AS score
   FROM public.bookings bk JOIN public.vehicles ve ON ve.id=bk.vehicle_id WHERE bk.vehicle_id=ANY(cancel_ids) AND bk.status<>'canceled') q;
 IF EXISTS (SELECT 1 FROM (SELECT member,count(*) FROM jsonb_array_elements(coalesce(opt->'groups','[]'::jsonb)) g CROSS JOIN LATERAL jsonb_array_elements_text(coalesce(g->'booking_ids','[]'::jsonb)) member GROUP BY member HAVING count(*)>1) duplicates) THEN
   RAISE EXCEPTION 'A booking cannot belong to multiple groups';
 END IF;
 group_seen=array_fill(false,ARRAY[jsonb_array_length(coalesce(opt->'groups','[]'::jsonb))]);
 group_whole=array_fill(false,ARRAY[jsonb_array_length(coalesce(opt->'groups','[]'::jsonb))]);
 UPDATE public.vehicles SET status='canceled' WHERE id=ANY(cancel_ids);
 FOR b IN SELECT bk.id,bk.passenger_id,bk.vehicle_id,ve.mode,ve.from_hub,ve.to_hub,ve.dep_at FROM public.bookings bk JOIN public.vehicles ve ON ve.id=bk.vehicle_id WHERE bk.vehicle_id=ANY(cancel_ids) AND bk.status<>'canceled'
   ORDER BY (CASE bk.tier WHEN 'medic' THEN 100 WHEN 'gold' THEN 80 WHEN 'silver' THEN 60 ELSE 40 END
     + CASE WHEN EXISTS (SELECT 1 FROM public.vehicles cv WHERE cv.id=bk.connecting_vehicle_id AND cv.dep_at>=ve.arr_at AND cv.dep_at<ve.arr_at+interval '60 minutes') THEN 20 ELSE 0 END
     + CASE WHEN EXISTS (SELECT 1 FROM public.passengers p WHERE p.id=bk.passenger_id AND (p.family OR p.elderly OR coalesce(p.special,'') ~* '(family|elderly)')) THEN 10 ELSE 0 END) DESC,bk.id FOR UPDATE OF bk LOOP
   group_no=NULL; grp=NULL;
   SELECT g.value, g.ordinality::integer INTO grp,group_no
     FROM jsonb_array_elements(coalesce(opt->'groups','[]'::jsonb)) WITH ORDINALITY g(value,ordinality)
     WHERE coalesce(g.value->'booking_ids','[]'::jsonb) ? b.id ORDER BY g.ordinality LIMIT 1;
   IF group_no IS NOT NULL AND NOT group_seen[group_no] THEN
     group_seen[group_no]=true;
     SELECT coalesce(array_agg(x),ARRAY[]::text[]) INTO group_ids FROM jsonb_array_elements_text(coalesce(grp->'booking_ids','[]'::jsonb)) x;
     group_count=cardinality(group_ids);
     SELECT count(*), min(array_position(ordered_ids,x)),max(array_position(ordered_ids,x))
       INTO matched_count,first_pos,last_pos FROM unnest(group_ids) x WHERE array_position(ordered_ids,x) IS NOT NULL;
     IF group_count>0 AND matched_count=group_count AND last_pos-first_pos+1=group_count THEN
       -- The row lock holds the whole-group capacity check through all allocations.
       SELECT ve.id,ve.mode INTO proposed FROM public.vehicles ve
       WHERE ve.id=grp->>'vehicle_id' AND NOT (ve.id=ANY(affected)) AND ve.status='scheduled' AND ve.available_seats>=group_count
       AND NOT EXISTS (SELECT 1 FROM public.bookings bk JOIN public.vehicles orig ON orig.id=bk.vehicle_id
         WHERE bk.id=ANY(group_ids) AND (replace(ve.from_hub,'-R','')<>replace(orig.from_hub,'-R','') OR replace(ve.to_hub,'-R','')<>replace(orig.to_hub,'-R','') OR ve.dep_at<=orig.dep_at)) FOR UPDATE;
       IF FOUND THEN group_whole[group_no]=true; END IF;
     END IF;
   END IF;
   -- A split group still gets its suggested seats first, in global priority order.
   SELECT ve.id,ve.mode INTO target FROM public.vehicles ve WHERE group_no IS NOT NULL AND ve.id=grp->>'vehicle_id'
   AND NOT (ve.id=ANY(affected)) AND ve.status='scheduled' AND ve.available_seats>0
   AND replace(ve.from_hub,'-R','')=replace(b.from_hub,'-R','') AND replace(ve.to_hub,'-R','')=replace(b.to_hub,'-R','') AND ve.dep_at>b.dep_at FOR UPDATE;
   IF NOT FOUND THEN
     SELECT ve.id,ve.mode INTO target FROM public.vehicles ve WHERE NOT (ve.id=ANY(affected)) AND ve.status='scheduled' AND ve.available_seats>0
     AND replace(ve.from_hub,'-R','')=replace(b.from_hub,'-R','') AND replace(ve.to_hub,'-R','')=replace(b.to_hub,'-R','') AND ve.dep_at>b.dep_at
     ORDER BY (ve.mode=b.mode) DESC,ve.dep_at,ve.id LIMIT 1 FOR UPDATE;
   END IF;
   IF FOUND THEN
     UPDATE public.vehicles SET available_seats=available_seats-1 WHERE id=target.id;
     UPDATE public.bookings SET vehicle_id=target.id,status='rebooked',rebooked_to_vehicle_id=target.id WHERE id=b.id;
     changes=changes||jsonb_build_array(jsonb_build_object('booking_id',b.id,'passenger_id',b.passenger_id,'from_vehicle',b.vehicle_id,'to_vehicle',target.id,'mode',target.mode));
   ELSE
     UPDATE public.bookings SET status='canceled' WHERE id=b.id;
     changes=changes||jsonb_build_array(jsonb_build_object('booking_id',b.id,'passenger_id',b.passenger_id,'from_vehicle',b.vehicle_id,'to_vehicle',NULL,'mode',NULL));
   END IF;
 END LOOP;
 FOR grp,group_no IN SELECT g.value,g.ordinality::integer FROM jsonb_array_elements(coalesce(opt->'groups','[]'::jsonb)) WITH ORDINALITY g(value,ordinality) LOOP
   SELECT coalesce(array_agg(x),ARRAY[]::text[]) INTO group_ids FROM jsonb_array_elements_text(coalesce(grp->'booking_ids','[]'::jsonb)) x;
   SELECT count(*),count(*) FILTER (WHERE x->>'to_vehicle'=grp->>'vehicle_id') INTO matched_count,moved_count
     FROM jsonb_array_elements(changes) x WHERE x->>'booking_id'=ANY(group_ids);
   IF matched_count=0 THEN CONTINUE; END IF;
   IF group_whole[group_no] AND moved_count=cardinality(group_ids) THEN
     INSERT INTO public.agent_log(incident_id,agent,step,message)
       SELECT p_incident,'executor','group_moved',format('Group moved: %s bookings → %s → %s %s %s',moved_count,ve.to_hub,ve.mode,ve.id,ve.dep_at) FROM public.vehicles ve WHERE ve.id=grp->>'vehicle_id';
   ELSE
     INSERT INTO public.agent_log(incident_id,agent,step,message)
       VALUES(p_incident,'executor','group_split',format('Group split: %s of %s displaced bookings on suggested %s; remainder handled individually in priority order',moved_count,matched_count,coalesce(grp->>'vehicle_id','unavailable vehicle')));
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

