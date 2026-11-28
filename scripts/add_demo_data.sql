-- Optional additive synthetic data for the Syncria demo Supabase project.
-- Reserved DEMO7-* identifiers make repeated runs safe; no existing rows are updated.
-- Run only on an isolated synthetic-data project, never on real passenger data.
BEGIN;
INSERT INTO public.vehicles (id,mode,no,from_hub,to_hub,dep_at,arr_at,capacity,available_seats,asset_id)
SELECT 'DEMO7-SVC-'||lpad(n::text,3,'0'), CASE WHEN n<=20 THEN 'flight' ELSE 'train' END,
 'DEMO7-SVC-'||lpad(n::text,3,'0'),
 (CASE n%3 WHEN 1 THEN 'BLR' WHEN 2 THEN 'DEL' ELSE 'BOM' END)||CASE WHEN n>20 THEN '-R' ELSE '' END,
 (CASE n%3 WHEN 1 THEN 'DEL' WHEN 2 THEN 'BOM' ELSE 'BLR' END)||CASE WHEN n>20 THEN '-R' ELSE '' END,
 (date_trunc('day',now() AT TIME ZONE 'UTC')+interval '6 hours'+(n-1)*interval '20 minutes') AT TIME ZONE 'UTC',
 (date_trunc('day',now() AT TIME ZONE 'UTC')+interval '8 hours'+(n-1)*interval '20 minutes') AT TIME ZONE 'UTC',
 180,160, CASE WHEN n<=8 THEN 'RW-17R' WHEN n<=20 THEN 'RW-35L' WHEN n<=25 THEN 'S-12' ELSE 'TS-09' END
FROM generate_series(1,30) n ON CONFLICT (id) DO NOTHING;

INSERT INTO public.passengers (id,name,tier,language)
SELECT 'DEMO7-PAX-'||lpad(n::text,4,'0'), 'Synthetic Traveler '||n,
 CASE WHEN n%50=0 THEN 'medic' WHEN n%10=0 THEN 'gold' WHEN n%5=0 THEN 'silver' ELSE 'regular' END,
 (ARRAY['en','hi','ta','es'])[1+(n%4)]
FROM generate_series(1,600) n ON CONFLICT (id) DO NOTHING;

INSERT INTO public.bookings (id,pnr,passenger_id,vehicle_id,seat,fare,tier)
SELECT 'DEMO7-BK-'||lpad(n::text,4,'0'), 'DEMO7PNR'||lpad(n::text,4,'0'),
 p.id,'DEMO7-SVC-'||lpad((1+(n-1)/20)::text,3,'0'), 'D'||(1+(n-1)%20), 150+(n%200),p.tier
FROM generate_series(1,600) n JOIN public.passengers p ON p.id='DEMO7-PAX-'||lpad(n::text,4,'0')
ON CONFLICT DO NOTHING;

INSERT INTO public.crew (id,name,role,duty_start,duty_limit_hours)
SELECT 'DEMO7-CREW-'||lpad(n::text,3,'0'), 'Synthetic Crew '||n,
 CASE WHEN n<=20 THEN 'pilot' ELSE 'train_driver' END,
 (date_trunc('day',now() AT TIME ZONE 'UTC')+interval '4 hours') AT TIME ZONE 'UTC',12
FROM generate_series(1,30) n ON CONFLICT (id) DO NOTHING;
INSERT INTO public.crew_assignments (crew_id,vehicle_id,role)
SELECT 'DEMO7-CREW-'||lpad(n::text,3,'0'), 'DEMO7-SVC-'||lpad(n::text,3,'0'),
 CASE WHEN n<=20 THEN 'pilot' ELSE 'train_driver' END
FROM generate_series(1,30) n ON CONFLICT DO NOTHING;
COMMIT;
