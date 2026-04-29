from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import get_settings
from app.services.factory import close_store, create_store

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = create_store(settings)
    try:
        yield
    finally:
        close_store(app.state.store)


app = FastAPI(title='CasePilot Backend', version=settings.version, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_origin_regex=settings.cors_allow_origin_regex,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)
app.include_router(api_router, prefix=settings.api_prefix)


@app.get('/')
def root():
    return {'service': settings.app_name, 'version': settings.version, 'docs': '/docs'}
