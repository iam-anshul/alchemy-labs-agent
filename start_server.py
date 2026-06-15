# Import first so Logfire is configured and pydantic-ai is instrumented before
# any agent modules (imported transitively via the routers) are loaded.
import observability  # noqa: F401

from fastapi import FastAPI
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.openapi.utils import get_openapi
from fastapi import APIRouter, status, Depends, HTTPException
from fastapi.openapi.docs import get_swagger_ui_html

from starlette.middleware.cors import CORSMiddleware
from supertokens_python import init, InputAppInfo, SupertokensConfig, get_all_cors_headers
from supertokens_python.framework.fastapi import get_middleware
from supertokens_python.recipe import session, emailpassword

from api.routes.chat import chat_router
from api.routes.workspace import workspace_router
from api.routes.documents import document_router
from api import ingest

from contextlib import asynccontextmanager

import secrets

from dotenv import load_dotenv
import os

import uvicorn

load_dotenv()
SUPERTOKENS_CONNECTION_STRING = os.getenv("SUPERTOKENS_CONNECTION_STRING")
SUPERTOKENS_API_KEY = os.getenv("SUPERTOKENS_API_KEY")
SUPERTOKENS_URI=os.getenv("SUPERTOKENS_URI")

# Domains the browser actually uses to reach the app. Must match the address in
# the address bar or session cookies won't be sent. Override per deployment.
API_DOMAIN = os.getenv("API_DOMAIN", "http://localhost:8000")
WEBSITE_DOMAIN = os.getenv("WEBSITE_DOMAIN", "http://localhost:8000")
# Cookies are sent only over HTTPS when True. Set False for plain-HTTP hosting
# (LAN/internal only — insecure on the public internet). Defaults to secure.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() in ("1", "true", "yes")

security = HTTPBasic()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the doc-reasoner ingest workers on boot so any document uploaded via
    # the /documents endpoint actually gets parsed + indexed. Without this, an
    # upload enqueues a doc that nothing drains, leaving it stuck at status
    # 'queued' forever (the /chat/run path starts its own workers, but standalone
    # uploads have no other trigger). shutdown_workers drains the queue on exit
    # so an in-flight ingest finishes before the process dies.
    ingest.start_workers()
    yield
    await ingest.shutdown_workers()


server = FastAPI(
    title="alchemy labs agentic server",
    description="api backend for sql alchemy",
    version="0.1",
    openapi_tags=[],
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

@server.get("/health")
def health_check():
    return {"status": "server is running"}

root_router = APIRouter()

async def get_current_user(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username  = secrets.compare_digest(credentials.username, "admin")
    correct_password = secrets.compare_digest(credentials.password, "password")
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

@server.get("/openapi.json", include_in_schema=False)
def custom_openapi(username: str = Depends(get_current_user)):
    if server.openapi_schema:
        return server.openapi_schema
    
    openapi_schema = get_openapi(
        title="alchemy labs server",
        version="0.1",
        routes=server.routes,
    )
    
    if "components" not in openapi_schema:
        openapi_schema["components"] = {}
    
    openapi_schema["components"]["securitySchemes"] = {
        "bearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT"
        }
    }
    
    
    for path in openapi_schema["paths"].values():
        for operation in path.values():
            if "security" not in operation:
                operation["security"] = []
            operation["security"].append({"bearerAuth": []})
    
    server.openapi_schema = openapi_schema
    return server.openapi_schema

server.openapi = custom_openapi

@server.get("/docs", include_in_schema=False)
async def get_documentation(username: str = Depends(get_current_user)):
    return get_swagger_ui_html(openapi_url="/openapi.json", title="alchemy labs server - API Docs")

init(
    app_info = InputAppInfo(
        app_name="alchemy labs server",
        api_domain=API_DOMAIN,
        website_domain=WEBSITE_DOMAIN
    ),
    supertokens_config = SupertokensConfig(
        connection_uri=SUPERTOKENS_URI,
        api_key=SUPERTOKENS_API_KEY
    ),
    framework="fastapi",
    recipe_list=[session.init(cookie_secure=COOKIE_SECURE), emailpassword.init()],
    mode="asgi"
)

# Trace incoming requests so each agent run is nested under the API call that
# triggered it. No-op if Logfire wasn't configured (no token).
observability.instrument_fastapi_app(server)

server.add_middleware(get_middleware())
server.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"] + get_all_cors_headers(),
)

server.include_router(root_router)

server.include_router(
    workspace_router,
    prefix="/workspace",
    tags=["workspace"]
)

server.include_router(
    chat_router,
    prefix="/chat",
    tags=["chat"]
)

server.include_router(
    document_router
)


if __name__ == "__main__":
    uvicorn.run("start_server:server", host="0.0.0.0", port=8000, reload=True)
