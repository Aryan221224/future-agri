"""API routers, one per area. Each module exposes ``router`` and optionally
``init(app)`` to register its services and job kinds."""
from . import jobs, system, workers

ROUTERS = [system, jobs, workers]
