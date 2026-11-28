# Syncria AI

Syncria is a transport-disruption control-room **demo** using synthetic data. It includes a Python/FastAPI backend and a browser frontend served by the same local process. It is not a production transport system; do not use real passenger data.

## Agent architecture and technical approach

### Architecture at a glance

Syncria uses a LangGraph workflow inside one backend application. Detector, impact analyst and planner now make optional LLM-assisted structured assessments from deterministic tool evidence; the validated planner recommendation is advisory. They are not independent services, and the LLM does not autonomously choose or call tools. Python rules and database transactions remain authoritative for operational calculations and execution.

```text
  Controller browser (HTML / CSS / JavaScript)
       |  REST: events, decisions, passengers, work orders
       |  SSE: incident state and agent activity
       v
  FastAPI backend  <---- optional Open-Meteo weather forecasts
       |
       +--> Detector: simulated disruption / WATCH advisory
       |
       +--> LangGraph thread, identified by incident ID
              detect --> impact --> plan --> APPROVAL INTERRUPT
                                       ^          |
                                       | reject   | approve
                                       +----------+--> execute --> notify --> end
                                                     |
                                                     v
                          SQLite transaction OR Supabase PostgreSQL RPC
                          (bookings, seats, work orders, outcomes, logs)

  Detector / impact / planner: deterministic tool evidence --> LLM JSON --> validation --> fallback
  Repair finished early: same incident --> impact --> new plan --> approval again
```

The frontend talks to the FastAPI API. The API and incident graph read and write through the storage adapter; the browser does **not** receive the Supabase service-role key. WATCH advisories are separate from the incident graph and do not execute recovery actions.

### Agent responsibilities

| Role / activity label | Responsibility | Implementation |
|---|---|---|
| **Detector** (`detector`) | Records a simulated disruption and its expected recovery; can record a simulated or forecast weather WATCH. A WATCH is advisory only. | Event ingestion in `app/main.py`; optional Open-Meteo polling in `app/weather.py`. |
| **Impact analyst** (`impact`) | Identifies affected services and bookings, missed connections, passenger tiers and crew-duty risks. | Deterministic `compute_cascade()` in `app/engine.py`. |
| **Recovery planner** (`planner`) | Produces three recovery options, estimates costs and confidence, groups affected bookings, and applies an optional historical confidence bonus. | `generate_options()` in `app/engine.py`; clustering and learning in `app/intelligence.py`. |
| **Conductor** (`conductor`) | Coordinates incident state, pauses for controller approval, routes rejection back to planning, and handles early-repair re-planning. | LangGraph in `app/graph.py` and orchestration endpoints in `app/main.py`. |
| **Executor** (`executor`) | Applies the approved option, rechecks seats, cancels or delays services, rebooks eligible bookings in priority order, handles eligible groups, and creates a work order where applicable. | SQLite transaction in `app/store.py` or service-role-only PostgreSQL RPC in `phase61.sql`. |
| **Communicator** (`communicator`) | Creates in-app multilingual passenger notifications and handles the passenger assistant's rule-based answers and optional general-text fallback. | `app/notifications.py`, `app/passenger_assistant.py`, and endpoints in `app/main.py`. |
| **Work-order activity** (`triage`) | Records work-order status changes when a controller starts or completes a repair. | Work-order API and storage adapter; **not** a separate LangGraph node. |

Agent activity is stored in `agent_log` and shown in the activity panel. The graph itself has six nodes: `detect`, `impact`, `plan`, `approve`, `execute`, and `notify`. Approval and work-order activity are application steps, not autonomous agents.

### How an incident moves through the system

1. **Detect:** A controller uses the Chaos Console to inject a synthetic storm, signal failure, engine fault, or crew absence. The API records an event and creates an incident; no recovery action has yet been executed.
2. **Analyze:** The impact step reads the current synthetic network and calculates the affected services, bookings, connections and crew risks. The result is stored in the incident state.
3. **Plan:** The planner calculates three deterministic options: **A** delay services, **B** cancel and rebook a subset, and **C** use alternate routing/staging. Cards show estimated cost, passenger impact, rule/crew checks, confidence, and actions. An LLM may recommend a validated crew-safe, rules-compliant option; otherwise the planner recommends the highest-confidence safe option (or highest-confidence option when none is safe). The two-rejection rule still requires the highest-confidence option.
4. **Human approval:** LangGraph pauses using `interrupt()`. The controller explicitly approves or rejects the proposal. A rejection returns to planning; after two rejections the controller must approve the highest-confidence option, but execution is never automatic.
5. **Execute:** The selected plan is executed in a single SQLite transaction or PostgreSQL RPC transaction. The executor checks current seat availability again, applies service and booking changes, records the execution result and outcome, and creates a work order where relevant. Execution is idempotent for an incident and plan version.
6. **Communicate:** The communicator creates in-app notifications using deterministic language templates and stores them after execution. Travelers in the demo can view messages and choose an available alternative. This is a synthetic demo, not actual email/SMS delivery.
7. **Re-plan when needed:** If repair finishes early, the recovery time and event are updated. The same incident thread re-enters impact analysis, creates a new plan version, supersedes older options and pauses for a new approval. Already executed actions are not rolled back.

The incident ID is the LangGraph `thread_id`. `MemorySaver` is the default checkpoint store; optional `PostgresSaver` uses `DATABASE_URL`. Pending approvals can be reconstructed from persisted incident state after an in-memory restart, but automatic recovery from a crash during execution or notification is not implemented.

### Technical approach and design choices

- **Deterministic operational core:** Impact analysis, option generation, cost estimates, confidence scoring, passenger priority, K-Means grouping, and execution use Python/SQL rules—not an LLM. Given the same input data, the planning calculations are reproducible. Execution rechecks live inventory, so its result can differ from a proposed plan if seats changed.
- **Priority and coordinated groups:** Rebooking scores start at medic 100, gold 80, silver 60 and regular 40, with bonuses for a tight connection and family/elderly flags. Bookings are processed in global score order. Pure-Python K-Means groups affected bookings by route, departure time and priority-related features. A group moves together when its suggested vehicle can fit all eligible members without jumping the priority queue; otherwise the executor logs a split and handles displaced bookings individually. Bookings on services that were not canceled are not moved.
- **Learning from outcomes:** Executions record the option, estimated cost, number moved, elapsed time and whether all passengers needing replacement seats obtained one. After at least three comparable outcomes for an event type and option, the planner adds a bounded confidence bonus. This is a simple historical rule, not model training or a guarantee of future success.
- **LLM-assisted roles:** Detector, impact and planner use strict JSON/Pydantic outputs, verified against deterministic evidence before caching or use. Planner may recommend a safe candidate; the controller remains responsible for approval. Invalid responses or unavailable providers fall back to rules. The model cannot alter operational data or execute tools. To avoid unvalidated free text, the handover, passenger answers and notifications use templates in the active workflow. The model is not guaranteed to return usable structured output.
- **Real-time visibility:** The backend persists state and agent logs and exposes an SSE stream. The browser consumes state updates and logs to update the dashboard and activity panel; the stream reads the stored state in a loop rather than using a separate message broker.
- **Weather as context:** Optional Open-Meteo forecasts for BLR/BOM/DEL can raise a 12-hour WATCH banner. Forecast failures are ignored, and a WATCH does not create a disruption or change the deterministic recovery plan. Current-weather context for a simulated storm is also optional.
- **Persistence and security:** `DB_BACKEND=sqlite` uses a local file; `DB_BACKEND=supabase` uses the server-side Supabase client over HTTPS and PostgreSQL RPCs for execution. Supabase Auth verifies controller sessions. This is a demo: the passenger endpoints do not verify passenger ownership, demo credentials are known, and the application must not be used with real passenger data.

### Technology and code map

| Layer | Technology | Main files |
|---|---|---|
| Browser dashboard | HTML, CSS, vanilla JavaScript, fetch/SSE; optional Web Speech APIs | `static/index.html`, `static/style.css`, `static/app.js` |
| API and orchestration | Python, FastAPI, LangGraph, Uvicorn | `app/main.py`, `app/graph.py` |
| Deterministic intelligence | Python rules, scoring, priority ranking and pure-Python K-Means | `app/engine.py`, `app/intelligence.py` |
| Messaging and assistant | Language templates and rule-based intents | `app/notifications.py`, `app/passenger_assistant.py`, `app/llm.py` |
| Storage and execution | SQLite or Supabase/PostgreSQL; atomic SQL functions | `app/store.py`, `schema.sql`, `phase3.sql`, `phase4.sql`, `phase6.sql`, `phase61.sql` |
| Testing and demo readiness | pytest, pre-demo checks, runbook | `tests/`, `scripts/pre_demo_check.py`, `DEMO.md` |

The optional crew/gate auto-allocation, Bayesian negotiator and camera-based crowd detection are **not implemented**; the dashboard uses synthetic network and booking data.

## Local setup: start here

Choose the guide for your computer:

- [Linux local setup](#linux-local-setup)
- [Windows local setup (PowerShell)](#windows-local-setup-powershell)
- [Optional: use Supabase instead of SQLite](#optional-use-supabase-instead-of-sqlite)
- [Verify, stop, and restart](#verify-stop-and-restart)

The quickest way to try the app is **SQLite mode**: no Supabase account, API key, database server, Node.js, or separate frontend server is needed. The first start automatically creates and seeds a local SQLite database.

### Prerequisites

| Requirement | Needed for | How to check / obtain it |
|---|---|---|
| Python **3.11 or newer**, with `pip` and virtual environment support | Required to run the backend and frontend | Linux: `python3 --version`. Windows PowerShell: `py --version`. On Windows, install Python from [python.org](https://www.python.org/downloads/windows/) and enable the Python launcher when prompted. |
| Git | Cloning the repository; optional if you download and extract its ZIP instead | Run `git --version`. Install from [git-scm.com](https://git-scm.com/downloads) or your Linux package manager. |
| A current browser | Viewing the dashboard | Chrome, Edge, or Firefox. Speech input depends on browser support and microphone permission; typing remains available. |
| Internet connection | Downloading Python packages during setup | Not needed for the SQLite demo after installation when live weather and LLM keys are disabled. |
| Node.js **18+** and npm | **Optional**, only for a separate frontend production build | `node --version` and `npm --version`; not needed to run the app locally. |
| Supabase project and credentials | **Optional**, for the shared database mode | Not needed for the local SQLite demo. See [Supabase setup](#optional-use-supabase-instead-of-sqlite). |
| Gemini/Groq API keys | **Optional**, for live generated text | Leave blank for deterministic template text. Live providers may have quotas or charges depending on your account. |
| `cloudflared` | **Optional**, only for an external tunnel | Not needed for localhost. See the [tunnel section](#phase-5-laptop-demo-and-optional-vercel-frontend). |

**Important:** The `.env.example` file defaults to Supabase. For the no-account SQLite demo, you **must change** `DB_BACKEND` to `sqlite` as shown below. Keep `.env` private; it is ignored by Git and must not be committed.

### Linux local setup

These steps use a terminal and Bash. For Ubuntu/Debian, install the prerequisites (other distributions use their own package manager):

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git nano
python3 --version     # must be 3.11 or newer
```

If your distribution's `python3` is older than 3.11, install a supported Python version and its matching `venv` package first. If you already have a checkout, skip `git clone` and `cd` into its repository root instead.

1. **Get the code:**

   ```bash
   git clone https://github.com/NITHISH74/AI-Disruption-Recovery-Asset-Response-Orchestrator.git
   cd AI-Disruption-Recovery-Asset-Response-Orchestrator
   ```

   Use the branch or revision containing these instructions if it has not yet been merged into the repository's default branch. Confirm `requirements.txt` and `app/main.py` exist in this directory.

2. **Create the virtual environment and install dependencies:**

   ```bash
   python3 -m venv .venv
   .venv/bin/python -m pip install --upgrade pip
   .venv/bin/python -m pip install -r requirements.txt
   ```

   These commands use the virtual environment directly; activation is not required.

3. **Configure the local demo:** If `.env` already exists, back it up and edit it instead of overwriting it. Otherwise:

   ```bash
   cp .env.example .env
   nano .env
   ```

   In the editor, **replace** the existing values with the following; do not add duplicate `DB_BACKEND` lines. Leave the key values empty for offline templates:

   ```dotenv
   DB_BACKEND=sqlite
   CHECKPOINTER=memory
   ENABLE_WEATHER=0
   ENABLE_FORECAST=0
   REQUIRE_CONTROLLER_LOGIN=1
   SECURE_COOKIES=0
   ```

   Save and close the file. In nano: `Ctrl+O`, `Enter`, then `Ctrl+X`.

4. **Start the app** from the repository root:

   ```bash
   .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

5. Open **http://localhost:8000**. Sign in with the demo credentials **`controller@syncria.ai` / `syncria123`**. Keep the terminal running while using the app. Click **Storm** to create a simulated incident.

### Windows local setup (PowerShell)

1. Install **Python 3.11 or newer** from [python.org](https://www.python.org/downloads/windows/) and Git from [git-scm.com](https://git-scm.com/download/win), if they are not installed. During Python installation, enable the Python launcher; adding Python to PATH is also helpful. Open a **new PowerShell** window and check:

   ```powershell
   py --version       # must be 3.11 or newer
   git --version
   ```

2. **Get the code:**

   ```powershell
   git clone https://github.com/NITHISH74/AI-Disruption-Recovery-Asset-Response-Orchestrator.git
   cd AI-Disruption-Recovery-Asset-Response-Orchestrator
   ```

   If you already downloaded the repository, use `cd` to enter its folder. Use the branch or revision containing these instructions if it has not yet been merged into the default branch; `requirements.txt` and `app\main.py` should be present.

3. **Create the virtual environment and install dependencies:**

   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install --upgrade pip
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

   These commands call the virtual environment directly, so PowerShell activation and execution-policy changes are not needed. If `py -3` selects an older version, specify an installed version such as `py -3.11 -m venv .venv` instead.

4. **Configure the local demo:** If `.env` already exists, back it up and edit it instead of overwriting it. Otherwise:

   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```

   In Notepad, **replace** the existing values with the following and save the file; do not add duplicate `DB_BACKEND` lines. Leave provider keys empty for offline templates:

   ```dotenv
   DB_BACKEND=sqlite
   CHECKPOINTER=memory
   ENABLE_WEATHER=0
   ENABLE_FORECAST=0
   REQUIRE_CONTROLLER_LOGIN=1
   SECURE_COOKIES=0
   ```

5. **Start the app** from the repository folder:

   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

6. Open **http://localhost:8000**. Sign in with **`controller@syncria.ai` / `syncria123`**. Keep PowerShell open while using the app. Click **Storm** to create a simulated incident.

### Verify, stop, and restart

- With the app running, visit **http://localhost:8000/health**. In SQLite mode it should report `db.status` as `reachable` and, without API keys, `llm.status` as `template_only`. The health endpoint checks configuration, not a live LLM call.
- Stop the server with **Ctrl+C** in its terminal. To restart, return to the repository folder and repeat the platform's Uvicorn command above. Reinstalling packages is not normally necessary.
- SQLite data persists in `syncria.db` in the repository root by default. To reset **only the synthetic demo data**, stop the server first and delete that database file; the next start creates a fresh one. Do not delete your repository or a database containing data you need. An optional `SYNCRIA_DB` setting changes the database path.
- If port 8000 is already in use, replace `--port 8000` with e.g. `--port 8001` and open `http://localhost:8001` instead. If package installation fails, confirm Python is 3.11+ and that you are in the repository root with internet access. If login fails, confirm `.env` says `DB_BACKEND=sqlite` and restart the server.
- The demo credentials are intentionally public and are **not production security**. Do not expose the server publicly or use real passenger data.

### Optional: use Supabase instead of SQLite

Use this only if you already have access to a Supabase project and want its shared database. It requires internet access; the local SQLite instructions above do not. Install the application dependencies using the Linux or Windows guide first.

1. For an **empty, separate demo project**, run the SQL files in the Supabase SQL Editor in this exact order: `schema.sql`, `seed.sql`, `phase3.sql`, `phase4.sql`, `phase6.sql`, then `phase61.sql`. The supplied project used during development already has these migrations applied. Do not run seed/reset operations on a shared project without checking its data first.
2. Edit the local `.env` file and replace the corresponding values with your project's settings:

   ```dotenv
   DB_BACKEND=supabase
   SUPABASE_URL=https://YOUR-PROJECT.supabase.co
   SUPABASE_ANON_KEY=YOUR_ANON_KEY
   SUPABASE_SERVICE_ROLE_KEY=YOUR_SERVICE_ROLE_KEY
   CONTROLLER_EMAIL=controller@syncria.ai
   REQUIRE_CONTROLLER_LOGIN=1
   CHECKPOINTER=memory
   SECURE_COOKIES=0
   ```

   Obtain the URL and keys from your project's Supabase settings. The service-role key is server-side only: never put it in frontend code, share it in chat, or commit `.env`. Leave `DATABASE_URL` empty when using the default MemorySaver. PostgresSaver is optional: set `CHECKPOINTER=postgres` and a valid `DATABASE_URL` only if you have an accessible Postgres connection; otherwise the app falls back to MemorySaver. Pending approval can be reconstructed after a MemorySaver restart; in-flight execution recovery is not automatic.
3. Create the confirmed demo controller once, using a trusted machine with the service-role key configured:

   ```bash
   # Linux
   .venv/bin/python scripts/create_demo_controller.py
   ```

   ```powershell
   # Windows PowerShell
   .\.venv\Scripts\python.exe scripts/create_demo_controller.py
   ```

   The script creates `controller@syncria.ai` / `syncria123` if missing. It does **not** reset an existing account's password. If that account already exists with a different password, use or reset that password in Supabase Auth.
4. Start the app with your platform's Uvicorn command above, then open `http://localhost:8000/health` and sign in. The server uses Supabase for persistent data. Startup rolls seeded schedule dates forward; it does not undo previous executions or restore a fresh dataset. Use an isolated synthetic demo project.

### Laptop live-verification checklist

- [ ] In the server log, confirm whether **PostgresSaver** connected or the explicit MemorySaver fallback was used; with Postgres selected, pause for approval, restart the backend, log in again, and approve the same incident via the API using its incident ID. With MemorySaver, repeat to verify reconstruction from persisted state. In-flight execution/notification recovery is not covered by this test.
  For the restart test, save the incident ID shown in the dashboard. After restarting and signing in, an example API resume (replace the ID and option) is:

  ```sh
  TOKEN=$(curl -s -X POST http://localhost:8000/api/login -H 'Content-Type: application/json' -d '{"email":"controller@syncria.ai","password":"syncria123"}' | python -c 'import sys,json; print(json.load(sys.stdin)["token"])')
  curl -X POST http://localhost:8000/api/incidents/INC-REPLACE/decision \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"action":"approve","option_id":"B"}'
  ```

- [ ] Verify `/api/health`, log in with the demo account, reject a bad password, refresh the browser and confirm the session survives. Sign out and confirm dashboard APIs are blocked.
- [ ] Open browser DevTools; press **Storm** and confirm the red banner, impact, activity and three cards appear within three seconds. Press **Signal failure** and check rail services. This latency has not been measured against live Supabase from this environment.
- [ ] Approve B, then query `incidents`, `decisions`, `vehicles`, `bookings`, `execution_results`, `notifications`, `work_orders` and `agent_log` in Supabase. Confirm canceled services, rebooked bookings, decremented available seats, and one initial notification per affected passenger. Select a passenger, change their service, and verify the new booking, seat counts, and confirmation notification. The seeded network has around 190 storm bookings, not the illustrative 480.
- [ ] Temporarily clear both LLM keys and verify the complete flow still uses templates. With a valid Gemini key, verify a live `get_llm` call; then set `GEMINI_API_KEY` empty and verify Groq independently. Example:

   ```sh
   python -c "from dotenv import load_dotenv; load_dotenv(); from app.llm import get_llm; print(get_llm('Reply briefly','Say hello','TEMPLATE FALLBACK'))"
   GEMINI_API_KEY='' python -c "from dotenv import load_dotenv; load_dotenv(); from app.llm import get_llm; print(get_llm('Reply briefly','Say hello','TEMPLATE FALLBACK'))"
   ```

- [ ] Check DevTools for console errors and SSE events, and rehearse on disposable data. The sandbox's direct HTTPS handshake fails, so real Supabase Auth/PostgREST, external LLMs, and end-to-end browser latency remain laptop checks.

## Offline demo and tests

Set `DB_BACKEND=sqlite`, `CHECKPOINTER=memory`, `ENABLE_WEATHER=0` in `.env`, then run the same `uvicorn` command. The local demo login accepts the displayed credentials. SQLite persists vehicles, bookings, seat inventory, incidents, decision audit, work orders, and notifications in `syncria.db` (override with `SYNCRIA_DB`). Delete/reset the **demo database file**, never the repository, only when you intentionally want fresh synthetic data.

Install the test dependencies and run the suite from the repository root:

```bash
# Linux
.venv/bin/python -m pip install pytest httpx
.venv/bin/python -m pytest -q
```

```powershell
# Windows PowerShell
.\.venv\Scripts\python.exe -m pip install pytest httpx
.\.venv\Scripts\python.exe -m pytest -q
```

Offline tests cover the prior flow, the interrupt/reject/approve path, SQLite transactional mutations, rechecking vanished seats, five-part multilingual notifications, self-service rebooking, login and protected routes, SSE event generation, MemorySaver restart reconstruction, and a mocked Supabase adapter. A DOM smoke test exercised the login, plans, and passenger picker without reported jsdom errors; a real browser console still needs checking.

## Semantics and limitations

- The chaos feed replays a 06:00 UTC synthetic schedule so the demo is deterministic, regardless of actual wall time. Costs, duty checks, and alternate routing are simplified estimates. No real GDS, flight, rail, or weather-dependent booking integration exists.
- LangGraph uses `interrupt()` and `thread_id=incident_id`. Two rejections are allowed; after that the highest-confidence plan is required, **but a human must still explicitly approve it**. The decision is audited before the graph resumes. Approval is never automatic.
- The default in-memory LangGraph checkpointer is process-local and retains checkpoint history in memory for the life of the process; it is not a bounded-memory production store. The application lock is also process-local, so multi-worker or overlapping-incident coordination is not guaranteed. Optional PostgresSaver persists checkpoints, but its live connection and restart behavior should be verified in the target environment.
- Execution updates services, seats, bookings and a work order in one SQLite transaction or one Postgres RPC transaction. If no matching seat remains, the affected booking is canceled and its notification says no seat has been assigned. The RPC is idempotent per incident and plan version. Notifications are persisted separately after execution; recovery from a crash between execution and notification needs further work.
- Initial messages use four deterministic language templates and contain what, why, action, self-service choices and recovery time. The active workflow uses deterministic message templates; LLM-personalized notifications are not implemented. A confirmed rebooking appends a notification. No actual email/SMS is sent.
- Controller dashboard APIs require a session by default. The demo passenger feed/rebooking API is **not passenger-authenticated**; IDs/booking IDs are not proof of identity. Add passenger authentication and authorization before using any real data. The SQLite demo account, local session tokens and synthetic records are not production security.
- Early repair starts dynamic re-planning and requires a fresh approval; previously executed actions are not reversed. The older simulated execution path exists solely for the original tests behind `ENABLE_LEGACY_FLOW=1`; do not enable it for the demo.

## LLM caching, accounting, and production configuration

All model requests use the `get_llm()` wrapper. Successful model responses are cached for 24 hours by a SHA-256 hash of the **model, system prompt, and exact input slice**; incident IDs are not part of the key. Equivalent sequential requests therefore reuse the result across incidents. If execution changes bookings or other inputs, the next incident's slice is different and a cache miss is correct. Templates are not cached. Calls without an incident context (for example a manual CLI test) are recorded under `__unscoped__`.

The backend creates `llm_cache` and `llm_usage` automatically in SQLite. On Supabase, the tables and service-only `syncria_llm_reserve` RPC are in `schema.sql` and have been applied to the supplied project. The RPC atomically reserves per-incident call/token budget across processes. Cache hits and template fallbacks have zero tokens and cost; live responses record provider-reported token counts and the reference cost from `app/llm_prices.py`. A timed-out response or missing provider usage is logged as `unknown` with **NULL cost** and its conservative reservation retained; it is not represented as free. Stale pending reservations are conservative and are not automatically released. If accounting is unavailable, the wrapper fails closed to templates rather than sending an unmetered request.

Default limits are 12 live **attempts** (including retries) and 25,000 tokens per incident; `LLM_MAX_CALLS` and `LLM_MAX_TOKENS` can lower but not raise them. Before a request, the guard reserves UTF-8 input bytes plus framing and maximum output, a conservative estimate rather than a provider-enforced token limit. Actual provider accounting may include overhead not reflected by the estimate, so these are application safeguards, not an absolute provider spending guarantee. A budget refusal writes one `agent_log` warning. A fresh cache hit is allowed before the guard, even after the budget is used up. SQLite uses an immediate transaction; Supabase uses a Postgres advisory transaction lock. Cache single-flight is process-local: simultaneous cache misses on different workers can still duplicate requests, although their budget reservations remain coordinated.

Configure `LLM_MODEL`, `LLM_FALLBACK`, and optionally `LLM_MODEL_PRIORITY` in `.env`. Supported names and reference USD/1K rates are in `app/llm_prices.py`. Examples: `gpt-4o-mini` with `OPENAI_API_KEY`, or `claude-3-5-haiku-20241022` with `ANTHROPIC_API_KEY`; the active structured agents only use the free-tier-priced aliases; other models are skipped for these agents. The default `gemini-2.0-flash` and `llama-3.3-70b-versatile` aliases assume **free-tier billing** and are tracked at zero. If those providers will bill the account, explicitly select `gemini-2.0-flash-paid` or `llama-3.3-70b-versatile-paid` instead. These aliases send the underlying provider model name but use nonzero reference rates. Prices, free-tier eligibility, cache discounts, taxes, regions and model availability change; verify the current provider prices and update the table before using a paid key. The table is an estimate, not an invoice. Unknown models are skipped rather than billed without a known price.

Cached responses are stored as plaintext in the server-side database with RLS enabled. Avoid putting sensitive personal data into model prompts; a hash key is not anonymization. Automatic background text enrichment is only launched when a provider key is configured; the wrapper itself checks cache before keys, so direct callers can retrieve a cached response offline. Live-provider and Supabase-client metering still require laptop verification in the restricted sandbox.

## Phase 4: earlier repair and work orders

The repair button updates the recovery ETA, records an event, and restarts the incident graph at impact with a new version of the plan. Previously selected actions are not undone; a new plan requires a fresh approval. Work orders are listed across incidents and can be started or completed by an authenticated controller. `phase4.sql` is required after `phase3.sql` on a fresh Supabase setup; both Phase 4 migrations have been applied to the supplied project. SQLite migrates existing execution and option records into version 1. If a re-plan fails after the ETA is saved, the incident remains in `replanning` for operator investigation rather than executing an old plan.

## Phase 5: laptop demo and optional Vercel frontend

The FastAPI-served frontend is the lowest-risk laptop demo; follow the [Linux or Windows local setup](#local-setup-start-here) above. Node 18+ is only needed to build the static frontend, and `cloudflared` is only needed for the optional tunnel. No deployment or new account is required for the laptop-only demo.

For an optional external Vercel frontend and an existing Cloudflare quick tunnel, keep that server running and open a second terminal:

```sh
cloudflared tunnel --url http://localhost:8000
```

Copy the **HTTPS** `https://…trycloudflare.com` origin printed by cloudflared. It changes on each quick-tunnel restart. In your existing Vercel project set the **only frontend env var** `NEXT_PUBLIC_API_URL` to that tunnel origin, with no trailing path, and build/redeploy. The static build reads it at build time, so redeploy after the tunnel URL changes. Set the backend `.env` `ALLOWED_ORIGINS` to a comma-separated list of exact browser origins, e.g. `https://your-project.vercel.app,https://your-tunnel.trycloudflare.com`, then restart uvicorn (the cloudflared process may remain running). Include every exact Vercel preview hostname you use; no wildcard is needed. `ALLOWED_ORIGINS` is **backend-only**. Confirm `https://YOUR-TUNNEL/health` reports a reachable database. Configure Vercel's build command as `npm run build` and output directory as `dist` (also supplied in `vercel.json`). Build locally with:

```sh
NEXT_PUBLIC_API_URL=https://YOUR-TUNNEL.trycloudflare.com npm run build
```

`NEXT_PUBLIC_API_URL` is public; put no keys, paths or credentials in it. An unset value builds same-origin assets for a backend that serves the API at the same origin; an unset value on a standalone Vercel frontend will not reach the backend. REST and the authenticated streaming request use the configured origin and bearer token; streaming does not rely on third-party cookies. Both the dashboard and tunnel are demo-only: public exposure provides a known demo password, and the passenger rebooking endpoint is not passenger-authenticated. Do not use real data or treat this as production security. `SECURE_COOKIES=1` is appropriate for HTTPS, although cross-origin streaming uses bearer authentication rather than the cookie. The tunnel must remain running throughout the demo.

### Pre-demo checks

```sh
python scripts/pre_demo_check.py
# After starting the backend and filling .env with free-tier keys:
python scripts/pre_demo_check.py --live --api-url http://localhost:8000
```

The default checks use an isolated temporary SQLite database and mocked provider; live Supabase, Gemini, Groq, and running-backend login are explicitly skipped. `--live` opts into real free-tier Gemini and Groq calls, checks Supabase seed counts, and tests the running backend's demo login. It forces Gemini failure locally when testing the real Groq fallback, and uses temporary SQLite accounting for that check. No Supabase rows are intentionally written by the script. Check current free-tier eligibility and configured provider billing before running it. Exit **0** means all checks passed; **1** means failure; **2** means at least one skipped check (not an all-clear). The health endpoint tests database reachability and reports which LLM keys are configured, not a live LLM call or provider availability. See [DEMO.md](DEMO.md) for the full timed script and fallbacks.

## Phase 6 intelligence (demo scope)

Apply `phase6.sql` after `phase4.sql` for a new Supabase installation. The supplied project has the corresponding migration applied. The SQL executor now orders displaced bookings by tier, tight connection (scheduled connecting departure within 60 minutes after arrival), and family/elderly flags. Ties break by booking ID. A tight connection adds 20, a family or elderly flag adds 10 once; a tight connection is only counted when it is nonnegative and scheduled times are known. The displayed rank is among the affected **bookings**, not all passengers in the network. Executors still recheck actual seats.

Executed plan versions write an outcome atomically. For the learning bonus, “success” means all passengers who needed replacement seats obtained one; a plan with no displaced passengers counts as successful under that specific definition, not as a claim of overall incident success. Three comparable event/option samples are required; the bonus is rounded from 0–8 and confidence is capped at 98. Sample counts and the definition are visible via plan data.

`ENABLE_FORECAST=1` (default) polls Open-Meteo every 15 minutes for BLR/BOM/DEL. API errors are ignored and watches never create incidents or execute plans. `ENABLE_FORECAST=0` disables polling; the Chaos Console's simulated WATCH still works. Watches are forecasts, not guarantees. Data and log rows persist, and expired watches are omitted from the banner.

K-means cluster suggestions are deterministic and reserve simulated whole-group capacity in the plan; unavailable groups are explicitly marked. During execution, the executor rechecks and locks actual inventory. A cluster whose displaced bookings are contiguous in global priority order and whose suggested vehicle fits the entire cluster is moved together. If seats have changed, members are on unaffected services, or moving the full group would jump another booking in priority order, it is split and displaced bookings are handled in global priority order. Existing bookings on services not canceled by the chosen option are never moved. Each group result is logged transactionally. Apply `phase61.sql` after `phase6.sql` for Supabase group execution; the migration has been applied to the supplied project and exercised in rolled-back SQL tests.

The passenger assistant has typed input and, where supported, browser SpeechRecognition/speechSynthesis. The rule-based answers query current booking data; general questions use a deterministic fallback, not free-form model answers. Recognition and available language voices depend on browser/device, microphone permission, and HTTPS; the typed fallback remains available. The existing demo passenger endpoints are not ownership-authenticated and must not be used for real passenger data.

The optional priority-3 crew/gate allocation, Bayesian negotiation and crowd estimator have not been implemented. Planner cost components and passenger alternatives ordered by arrival are displayed; capacity is rechecked when a passenger confirms.

## Phase 7: business-value estimates and controller tools

These tools use **synthetic demo data** and are not financial, legal, or operational advice. They do not integrate a GDS, payroll system, legal jurisdiction database, or actual claims data.

- **Estimated ROI:** for each executed plan, the manual baseline is `affected bookings × ($60 extra compensation + $25 × (disruption hours + 2 extra manual hours) + $20 extra cancellation exposure)`. Estimated savings are baseline minus the chosen plan's modeled USD cost; they may be negative and are not verified, realized savings. Monthly totals use the latest executed estimate per incident in the UTC month; multiple versions of one incident are **not** summed. Previous incidents without an estimate are excluded. Modeled EUR regulatory exposure is shown separately and not converted or added to the USD estimate.
- **Illustrative compensation:** an EU261-style teaching model applies €250 for distances up to 1,500 km, €400 up to 3,500 km, and €600 above that, for a canceled flight or an assumed delay of at least three hours. Flights only; each passenger is counted at most once per option using the largest estimated amount. Seeded BLR/BOM/DEL airport coordinates provide approximate great-circle distance when a distance is not supplied; unknown distances are omitted and counted separately. Delay uses the estimated departure delay as a proxy for arrival delay, not an actual arrival outcome. **The Indian synthetic routes are not established as EU261-covered.** Jurisdiction, extraordinary circumstances, notices, re-routing, reductions and other legal requirements are not assessed. The displayed exposure is a hypothetical scenario, not compensation owed; obtain qualified legal advice for real claims.
- **Incident report and timeline:** the Incident report view reads persisted logs, historical plan versions, decisions, execution results and notifications. Timeline durations are the intervals to the next recorded log, not measured task runtimes. Export uses the browser's print dialog. Reports can contain passenger notification content: treat exports as sensitive. Regulatory totals shown in the report are for the current options only; historical exposures are not reconstructed in Supabase.
- **What-if simulator:** the incident dashboard supports canceling one affected service or changing the disruption duration. It runs deterministic impact and plan generation on an in-memory copy and reads existing outcome history; it does not reserve seats, call an LLM, or write any DB rows. It intentionally does not write an `agent_log` row: logging would violate the strict zero-write dry-run guarantee. The result is a projection, not an approval or execution.
- **Crew duty monitor:** the roster view estimates used and remaining duty time from each duty start and configured limit, flags remaining duty below two hours and future assignments whose scheduled arrival exceeds the duty end. It is not a certified legality calculation and does not model rest, time-zone or jurisdiction-specific regulations. SQLite demo roster start times are inferred from synthetic data; roles may be unknown.
- **Shift handover:** the crew view generates a copyable summary of active incidents, pending approvals, recent stored decisions, duty warnings, and open work orders. Handover uses a deterministic template; free-form LLM handovers are disabled until grounded validation is available. Review it before operational use.

The optional Rust/PyO3 engine was not built: this sandbox has no Rust toolchain, and Python remains the active deterministic engine. Standalone triage, automatic crew/gate allocation, Bayesian negotiation, crowd estimation, webhooks and A2A are not included in this increment.

### Additional synthetic demo data

The optional `scripts/add_demo_data.sql` adds 30 flights/trains, 600 synthetic passengers and bookings, and 30 crew members with assignments to an existing seeded Supabase demo project. Its `DEMO7-` identifiers distinguish this batch, and inserts do not overwrite existing rows. Run it only on an isolated demo project after the normal seed and migrations; do not run it against real passenger data. Executing plans can subsequently change these records. The dashboard's scheduled-services table pages through the loaded network rather than showing only six rows; passenger notifications and incident activity still appear only after their respective workflows run. This batch is Supabase-specific and does not modify SQLite seed data.

### Structured LLM agent increment and limitations

Detector, impact and planner in `app/agents.py` each receive an allowlisted deterministic evidence slice, request JSON via the existing budgeted `get_llm()` wrapper, and validate with Pydantic and role-specific checks before a response may be cached or persisted. Impact numbers and IDs must match tool results exactly; recommendations must pass the existing crew/rules checks. Reasoning is represented by bounded categories, not arbitrary unverifiable prose. Detector passes a validated assessment to impact; impact passes one to planner. Executor remains deterministic and approval remains mandatory. LLM confidence adjustments are advisory and do not change deterministic confidence. The live call limit is 12 attempts per incident, including retries. Set `LLM_MAX_CALLS=0` to disable live calls. Check provider free-tier eligibility: zero-price aliases are accounting assumptions, not a billing guarantee.

This is a **partial** multi-agent increment: tools are called by application code in a fixed sequence, not dynamically selected via native model tool calls. Detector severity remains the existing feed rule rather than a new duration/traffic rule. Communicator, triage, and an LLM supervisor were not converted. In particular there is no validated duty-legal crew assignment. Malformed, ungrounded or unavailable model results fall back to deterministic behavior. Synthetic IDs and operational summaries may be sent to configured providers; never use real passenger data. Live provider, Supabase runtime and real-browser console verification remain outstanding.
