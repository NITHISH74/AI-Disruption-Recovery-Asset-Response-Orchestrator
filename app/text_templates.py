"""Offline text fallbacks, separate from deterministic planning decisions."""
IMPACT = '{services} services and {passengers} bookings are affected. {connections} connections need review and {crew} crew members may exceed duty limits.'
PLAN_A = 'Keep services intact and avoid cancellations. Travelers wait longer and any crew duty risks need intervention.'
PLAN_B = 'Cancel {cancelled} services and allocate {assigned} available seats. {unassigned} travelers still need an alternative; cancellations and seat availability are the main trade-offs.'
PLAN_C = 'Stage services on an alternate route to keep the network moving. Additional handling costs and remaining crew duty risks are the main trade-offs.'
