"""Веб-платформа адвоката (FastAPI)."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.web.auth import LoginRequired
from app.web.routes import auth_routes, cases, clients, settings_routes
from app.web.templating import render


def create_app(flow=None) -> FastAPI:
    s = get_settings()
    app = FastAPI(title="Платформа адвоката", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.flow = flow
    app.add_middleware(
        SessionMiddleware,
        secret_key=s.secret_key,
        session_cookie="lawyer_session",
        max_age=12 * 3600,
        same_site="strict",
        https_only=s.cookie_secure,
    )
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        response = render(request, "error.html", code=exc.status_code, detail=exc.detail)
        response.status_code = exc.status_code
        return response

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(auth_routes.router)
    app.include_router(cases.router)
    app.include_router(clients.router)
    app.include_router(settings_routes.router)
    return app
