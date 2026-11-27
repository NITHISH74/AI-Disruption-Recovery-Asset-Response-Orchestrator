-- Deterministic demo seed. Run once before a demo on an empty Syncria project.
-- Non-destructive for existing records: do not truncate historical incidents/bookings.
INSERT INTO hubs(id,name,city,type) VALUES
 ('BLR','Kempegowda International Airport','Bengaluru','airport'),('BOM','Mumbai Airport','Mumbai','airport'),('DEL','Delhi Airport','Delhi','airport'),
 ('BLR-R','Bengaluru City Station','Bengaluru','station'),('BOM-R','Mumbai Central','Mumbai','station'),('DEL-R','Delhi Station','Delhi','station')
ON CONFLICT(id) DO NOTHING;
INSERT INTO assets(id,hub_id,name,kind) VALUES
 ('RW-17R','BLR','Runway 17R','runway'),('RW-35L','BLR','Runway 35L','runway'),('RW-02L','BOM','Runway 02L','runway'),('S-12','DEL-R','Signal box S-12','signal'),('AK-204','BLR','Aircraft AK-204','aircraft'),('TS-09','BLR-R','Train set TS-09','trainset'),('B7','BLR','Gate B7','gate')
ON CONFLICT(id) DO NOTHING;
WITH base AS (
 SELECT n, n < 40 AS flight, CASE WHEN n < 40 THEN n ELSE n-40 END AS idx FROM generate_series(0,59) n
), records AS (
 SELECT n, flight, idx, CASE WHEN flight THEN 'AK-'||(201+idx) ELSE 'TR-'||(1201+idx) END AS vehicle_id,
 (date_trunc('day', now() AT TIME ZONE 'UTC') + interval '6 hours' + idx * interval '24 minutes') AT TIME ZONE 'UTC' AS dep
 FROM base
)
INSERT INTO vehicles(id,mode,no,from_hub,to_hub,dep_at,arr_at,capacity,available_seats,asset_id)
SELECT vehicle_id, CASE WHEN flight THEN 'flight' ELSE 'train' END, vehicle_id,
 (CASE WHEN NOT flight AND idx<6 THEN CASE WHEN idx%2=0 THEN 'BLR' ELSE 'DEL' END ELSE CASE idx%4 WHEN 0 THEN 'BLR' WHEN 1 THEN 'BLR' WHEN 2 THEN 'DEL' ELSE 'BOM' END END)||CASE WHEN flight THEN '' ELSE '-R' END,
 (CASE WHEN NOT flight AND idx<6 THEN CASE WHEN idx%2=0 THEN 'DEL' ELSE 'BLR' END ELSE CASE idx%4 WHEN 0 THEN 'DEL' WHEN 1 THEN 'BOM' ELSE 'BLR' END END)||CASE WHEN flight THEN '' ELSE '-R' END,
 dep, dep + interval '2 hours', CASE WHEN flight THEN 180 ELSE 240 END, CASE WHEN flight THEN 35 ELSE 60 END,
 CASE WHEN flight THEN CASE WHEN idx<6 THEN 'RW-17R' ELSE 'RW-35L' END ELSE CASE WHEN idx<6 THEN 'S-12' ELSE 'TS-09' END END
FROM records ON CONFLICT(id) DO UPDATE SET dep_at=EXCLUDED.dep_at, arr_at=EXCLUDED.arr_at, from_hub=EXCLUDED.from_hub, to_hub=EXCLUDED.to_hub;
INSERT INTO passengers(id,name,tier,language)
SELECT 'P'||lpad(n::text,4,'0'), (ARRAY['Aarav Sharma','Priya Patel','Ananya Rao','Rohan Mehta','Kavya Nair','Arjun Singh','Meera Iyer','Ishaan Gupta','Sofia Torres','Neha Kapoor','Dev Kumar','Sara Khan'])[1+(n%12)],
 CASE WHEN n%100<90 THEN 'regular' WHEN n%100<96 THEN 'silver' WHEN n%100<99 THEN 'gold' ELSE 'medic' END,
 CASE WHEN (n*37)%100<70 THEN 'en' WHEN (n*37)%100<85 THEN 'hi' WHEN (n*37)%100<95 THEN 'ta' ELSE 'es' END
FROM generate_series(1,1100) n ON CONFLICT(id) DO NOTHING;
INSERT INTO bookings(id,pnr,passenger_id,vehicle_id,seat,fare,tier,connecting_vehicle_id)
SELECT 'BK'||lpad(n::text,5,'0'), 'SY'||(100000+n), p.id, v.id, 'S'||(1+(n%150)), 200+(n%250), p.tier,
 CASE WHEN n%20<3 THEN (SELECT vv.id FROM vehicles vv WHERE vv.id <> v.id AND vv.from_hub = v.to_hub AND vv.dep_at > v.arr_at ORDER BY vv.dep_at, vv.id LIMIT 1) ELSE NULL END
FROM generate_series(1,1500) n
JOIN passengers p ON p.id='P'||lpad((1+(n-1)%1100)::text,4,'0')
JOIN vehicles v ON v.id=CASE WHEN n%5=0 THEN 'TR-'||(1201+(n/5)%20) ELSE 'AK-'||(201+((n-1)%40)) END
ON CONFLICT(id) DO NOTHING;
INSERT INTO crew(id,name,role,duty_start,duty_limit_hours)
SELECT 'C'||lpad(n::text,3,'0'), CASE WHEN n=1 THEN 'Capt. Rao' WHEN n=2 THEN 'Capt. Sharma' ELSE 'Crew '||n END,
 (ARRAY['pilot','copilot','steward','train_driver','conductor'])[1+(n-1)%5],
 (date_trunc('day',now() AT TIME ZONE 'UTC') + interval '6 hours' - CASE WHEN n<=2 THEN interval '11 hours' ELSE interval '4 hours' END) AT TIME ZONE 'UTC', 12
FROM generate_series(1,30) n ON CONFLICT(id) DO UPDATE SET duty_start=EXCLUDED.duty_start;
INSERT INTO crew_assignments(crew_id,vehicle_id,role)
SELECT 'C'||lpad(n::text,3,'0'), CASE WHEN n<=2 THEN 'AK-'||(200+n) WHEN n%5 IN (4,0) THEN 'TR-'||(1201+(n%20)) ELSE 'AK-'||(201+(n%40)) END,
 (ARRAY['pilot','copilot','steward','train_driver','conductor'])[1+(n-1)%5]
FROM generate_series(1,30) n ON CONFLICT DO NOTHING;
