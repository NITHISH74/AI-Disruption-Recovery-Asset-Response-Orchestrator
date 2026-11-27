-- Syncria AI phase 1/2 schema. Synthetic demo data only.
-- RLS protects the public API; the backend connects directly as a trusted DB role.
CREATE TABLE IF NOT EXISTS public.hubs (id text PRIMARY KEY, name text NOT NULL, city text NOT NULL, type text NOT NULL, x double precision, y double precision);
CREATE TABLE IF NOT EXISTS public.assets (id text PRIMARY KEY, hub_id text REFERENCES public.hubs(id), name text NOT NULL, kind text NOT NULL, status text NOT NULL DEFAULT 'operational', expected_repair_end timestamptz);
CREATE TABLE IF NOT EXISTS public.vehicles (id text PRIMARY KEY, mode text NOT NULL, no text NOT NULL, from_hub text REFERENCES public.hubs(id), to_hub text REFERENCES public.hubs(id), dep_at timestamptz NOT NULL, arr_at timestamptz NOT NULL, capacity integer NOT NULL, available_seats integer NOT NULL CHECK (available_seats >= 0), status text NOT NULL DEFAULT 'scheduled', asset_id text REFERENCES public.assets(id), alternate_no text);
CREATE TABLE IF NOT EXISTS public.passengers (id text PRIMARY KEY, name text NOT NULL, tier text NOT NULL, language text NOT NULL, contact text, special text);
CREATE TABLE IF NOT EXISTS public.bookings (id text PRIMARY KEY, pnr text NOT NULL UNIQUE, passenger_id text NOT NULL REFERENCES public.passengers(id), vehicle_id text NOT NULL REFERENCES public.vehicles(id), seat text, fare numeric NOT NULL, tier text NOT NULL, status text NOT NULL DEFAULT 'confirmed', rebooked_to_vehicle_id text REFERENCES public.vehicles(id), connecting_vehicle_id text REFERENCES public.vehicles(id));
CREATE TABLE IF NOT EXISTS public.crew (id text PRIMARY KEY, name text NOT NULL, role text NOT NULL, duty_start timestamptz NOT NULL, duty_limit_hours numeric NOT NULL);
CREATE TABLE IF NOT EXISTS public.crew_assignments (crew_id text REFERENCES public.crew(id), vehicle_id text REFERENCES public.vehicles(id), role text NOT NULL, PRIMARY KEY (crew_id, vehicle_id));
CREATE TABLE IF NOT EXISTS public.events (id text PRIMARY KEY, type text NOT NULL, asset_id text, severity text NOT NULL, source text NOT NULL, started_at timestamptz NOT NULL, expected_end timestamptz NOT NULL, status text NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS public.incidents (id text PRIMARY KEY, event_id text REFERENCES public.events(id), status text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), state jsonb NOT NULL DEFAULT '{}'::jsonb);
CREATE TABLE IF NOT EXISTS public.impacts (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, incident_id text NOT NULL REFERENCES public.incidents(id), affected_vehicle_ids text[] NOT NULL, pax_count integer NOT NULL, missed_connections integer NOT NULL, crew_risks jsonb NOT NULL, summary_md text NOT NULL, raw jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS public.recovery_plans (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, incident_id text NOT NULL REFERENCES public.incidents(id), option_id text NOT NULL, strategy text NOT NULL, cost_usd numeric NOT NULL, pax_affected integer NOT NULL, crew_safe boolean NOT NULL, rules_compliant boolean NOT NULL, confidence integer NOT NULL, explanation_md text NOT NULL, actions jsonb NOT NULL, selected boolean NOT NULL DEFAULT false, UNIQUE(incident_id, option_id));
CREATE TABLE IF NOT EXISTS public.decisions (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, incident_id text REFERENCES public.incidents(id), option_id text, action text NOT NULL, edits jsonb, feedback text, decided_by text, at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS public.notifications (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, incident_id text REFERENCES public.incidents(id), passenger_id text REFERENCES public.passengers(id), channel text NOT NULL, lang text NOT NULL, body text NOT NULL, status text NOT NULL, sent_at timestamptz);
CREATE TABLE IF NOT EXISTS public.work_orders (id text PRIMARY KEY, incident_id text REFERENCES public.incidents(id), asset_id text REFERENCES public.assets(id), title text NOT NULL, diagnosis text NOT NULL, priority text NOT NULL, window_start timestamptz, window_end timestamptz, eta_hours numeric, status text NOT NULL);
CREATE TABLE IF NOT EXISTS public.agent_log (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, incident_id text NOT NULL REFERENCES public.incidents(id), agent text NOT NULL, step text NOT NULL, message text NOT NULL, at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS agent_log_incident_idx ON public.agent_log (incident_id, id);
CREATE INDEX IF NOT EXISTS bookings_vehicle_idx ON public.bookings (vehicle_id);
ALTER TABLE public.hubs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.assets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.vehicles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.passengers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.bookings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.crew ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.crew_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.incidents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.impacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.recovery_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.work_orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_log ENABLE ROW LEVEL SECURITY;

-- LLM cache and accounting. Only the server service role may read/write them.
CREATE TABLE IF NOT EXISTS public.llm_cache (cache_key text PRIMARY KEY, response text NOT NULL, model text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS public.llm_usage (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, incident_id text NOT NULL, agent text NOT NULL, model text NOT NULL, prompt_tokens integer, completion_tokens integer, cost_usd numeric, cache_hit boolean NOT NULL DEFAULT false, at timestamptz NOT NULL DEFAULT now(), outcome text NOT NULL DEFAULT 'live', reserved_tokens integer NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS llm_usage_incident_idx ON public.llm_usage (incident_id, id);
ALTER TABLE public.llm_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.llm_usage ENABLE ROW LEVEL SECURITY;
CREATE OR REPLACE FUNCTION public.syncria_llm_reserve(p_incident text,p_agent text,p_model text,p_tokens integer,p_max_calls integer,p_max_tokens integer) RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE calls bigint; used bigint; row_id bigint;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended(p_incident,0));
 SELECT count(*),coalesce(sum(CASE WHEN outcome IN ('pending','unknown') THEN reserved_tokens ELSE coalesce(prompt_tokens,0)+coalesce(completion_tokens,0) END),0) INTO calls,used FROM public.llm_usage WHERE incident_id=p_incident AND outcome IN ('pending','live','unknown','failed');
 IF calls>=p_max_calls OR used+p_tokens>p_max_tokens THEN RETURN NULL; END IF;
 INSERT INTO public.llm_usage(incident_id,agent,model,cache_hit,outcome,reserved_tokens) VALUES(p_incident,p_agent,p_model,false,'pending',p_tokens) RETURNING id INTO row_id;
 RETURN row_id;
END $$;
REVOKE ALL ON FUNCTION public.syncria_llm_reserve(text,text,text,integer,integer,integer) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.syncria_llm_reserve(text,text,text,integer,integer,integer) TO service_role;
