# Syncria AI — five-minute demo runbook

## Before the timer

- Complete the setup and run `python scripts/pre_demo_check.py --live --api-url http://localhost:8000`. Exit 0 means every check passed; exit 1 means a failure and exit 2 means something was skipped. Use free-tier keys only. Open `/health` and DevTools Console. If the tunnel or LLM is unavailable, use the laptop URL and template text; core impact, planning and execution do not require an LLM.
- Use a fresh, isolated synthetic demo dataset with available seats. Execution changes bookings and inventory; rerunning the seed does not restore previously executed states. Rehearse self-rebooking and identify a passenger with an available later service. Do not use real passenger data.
- The dashboard is protected **before** disruption injection: sign in on the opening screen as `controller@syncria.ai` / `syncria123` before starting the clock. This is a demo account, not production security. Keep the session active throughout.

## The five minutes

**0:00 — Intro.** Open **Command center**. Say: “Syncria AI — the AI control room for transport disruptions.” Point to the network status, chaos console and Agent activity panel.

**0:30 — Detect and analyze.** Click **Storm** in the Chaos console. Follow the live Agent activity lines from detection to impact. Point to affected services and travelers; read missed connections from the impact summary/activity line and click **Crew warnings** for duty risks. Say: “The deterministic engine maps the ripple effects across the network.”

**1:30 — Compare options.** Scroll to the three **Recovery options** cards. Explain A delays, B cancels and rebooks a subset, and C uses alternate routing. Point out cost, passengers, crew/rule badges and confidence. Point to **RECOMMENDED**: “The recommendation is a transparent score, not an LLM decision.” If there is a tie, multiple cards may be tagged.

**2:30 — Human approval and execution.** You already signed in before the timer because the dashboard requires authentication. Click **Option B**, then **Approve selected plan**. Follow executor and communicator lines. Say: “A controller approves the plan; execution rechecks capacity and rewrites affected bookings.” The activity reports the executed rebooking count. Do not imply cancellations or rebookings were only simulated.

**3:30 — Passenger view.** Click **Passenger view** in the left navigation. Select a traveler in the dropdown; show their personalized notification and language. Click **Change my flight / train**, then an offered alternative to confirm a seat. If none is available, select the rehearsed traveler; don't promise that every traveler will have an alternative.

**4:15 — Earlier repair.** Click **Command center**, then **Repair finished early**. Point to the re-planning and updated impact lines in Agent activity and the fresh option cards. Say: “The earlier recovery feeds the same incident back through impact and planning; the new version waits for approval.” Previously executed actions are not rolled back.

**4:45 — Work orders and close.** Click **Work orders**. For the order from the incident, click **Start**, then **Complete**. Say: “Deterministic core, LLM only for words — $0 to run today, pennies per incident in production.” The latter is an illustrative estimate, not a guaranteed price: actual provider pricing and usage vary; free-tier aliases must remain on a free tier.

If the live tunnel fails, switch to `http://localhost:8000`. If the providers fail, show the template fallback. If the selected passenger has no alternative, explain the seat constraint rather than forcing a booking. Never reset a shared database during the demo.
