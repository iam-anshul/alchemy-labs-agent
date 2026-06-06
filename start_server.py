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

import secrets

from dotenv import load_dotenv
import os

import uvicorn

load_dotenv()
SUPERTOKENS_CONNECTION_STRING = os.getenv("SUPERTOKENS_CONNECTION_STRING")
SUPERTOKENS_API_KEY = os.getenv("SUPERTOKENS_API_KEY")
SUPERTOKENS_URI=os.getenv("SUPERTOKENS_URI")

security = HTTPBasic()

server = FastAPI(
    title="alchemy labs agentic server",
    description="api backend for sql alchemy",
    version="0.1",
    openapi_tags=[],
    docs_url=None,  
    redoc_url=None 
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
        api_domain="http://localhost:8000",
        website_domain="http://localhost:8000"
    ),
    supertokens_config = SupertokensConfig(
        connection_uri=SUPERTOKENS_URI,
        api_key=SUPERTOKENS_API_KEY
    ),
    framework="fastapi",
    recipe_list=[session.init(), emailpassword.init()],
    mode="asgi"
)

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
    chat_router,
    prefix="/chat",
    tags=["chat"]
)

if __name__ == "__main__":
    uvicorn.run("start_server:server", host="0.0.0.0", port=8000, reload=True)
