from fastapi import APIRouter

from app.api import routes_agents, routes_cases, routes_diagnostics, routes_executions, routes_files, routes_knowledge, routes_obd

api_router = APIRouter()
api_router.include_router(routes_cases.router)
api_router.include_router(routes_files.router)
api_router.include_router(routes_obd.router)
api_router.include_router(routes_agents.router)
api_router.include_router(routes_executions.router)
api_router.include_router(routes_knowledge.router)
api_router.include_router(routes_diagnostics.router)
