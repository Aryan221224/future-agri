"""API routers, one per area. Each module exposes ``router`` and optionally
``init(app)`` to register its services and job kinds."""
from . import farms, jobs, system, workers

ROUTERS = [system, jobs, workers, farms]
