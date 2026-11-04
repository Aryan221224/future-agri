"""API routers, one per area. Each module exposes ``router`` and optionally
``init(app)`` to register its services and job kinds."""
from . import aircraft, drone, farms, jobs, live, live_run, plans, records, session, stock_loop, system, workers

ROUTERS = [system, jobs, workers, farms, drone, session, plans, live, records, live_run, stock_loop, aircraft]
