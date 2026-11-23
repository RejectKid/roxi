from urllib.parse import parse_qs

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import pilot_access as access
from .query_service import QueryError
from .storage import connect_read
from .views import templates

router = APIRouter()


def same_origin(request: Request) -> bool:
    return request.headers.get("origin") == f"{request.url.scheme}://{request.url.netloc}"


def secure_response(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def account(auth):
    if auth:
        with connect_read() as conn:
            return access.identity(conn, auth)
    return None


async def gate(request: Request, call_next):
    path = request.url.path
    if path == "/healthz" or path.startswith("/static/"):
        return await call_next(request)
    if access.demo():
        if (request.client is None or request.client.host not in {"127.0.0.1", "::1"}
                or request.url.hostname not in {"127.0.0.1", "::1", "localhost"}):
            return JSONResponse({"detail": "Demo mode is local only."}, 403)
        return await call_next(request)
    auth_header = request.headers.get("authorization")
    if auth_header is not None:
        scheme, _, token = auth_header.partition(" ")
        auth = access.credential(token, "api") if scheme.lower() == "bearer" else None
    else:
        auth = access.credential(request.cookies.get(access.COOKIE), "session")
    try:
        username = await run_in_threadpool(account, auth)
    except psycopg.OperationalError:
        return secure_response(JSONResponse({"detail": "The workspace database is unavailable."}, 503))
    request.state.username = username
    if path != "/login" and not username:
        if not path.startswith("/v1/") and request.method in {"GET", "HEAD"} and "text/html" in request.headers.get("accept", ""):
            return secure_response(RedirectResponse("/login", 303))
        error = QueryError(401, "unauthenticated", "Sign in or provide a valid access token.")
        body = error.result().model_dump(mode="json") if path.startswith("/v1/") else {"detail": error.message}
        return secure_response(JSONResponse(body, 401,
                                            headers={"WWW-Authenticate": "Bearer"}))
    if request.method not in {"GET", "HEAD", "OPTIONS"} and (path == "/login" or (auth and auth[1] == "session")):
        if not same_origin(request):
            return secure_response(JSONResponse({"detail": "Use the Roxi workspace origin."}, 403))
    context = access.AUTH.set(auth)
    try:
        return secure_response(await call_next(request))
    finally:
        access.AUTH.reset(context)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if access.demo() or getattr(request.state, "username", None):
        return RedirectResponse("/", 303)
    return templates.TemplateResponse(request, "pilot_login.html", {})


@router.post("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if access.demo():
        return RedirectResponse("/", 303)
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    if media_type != "application/x-www-form-urlencoded":
        raise access.AccessError(415, "Use the Roxi sign-in form.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 4096:
            raise access.AccessError(413, "The sign-in form is too large.")
        body.extend(chunk)
    try:
        fields = parse_qs(body.decode(), max_num_fields=2)
        username = fields.get("username", [""])[0].strip().lower()
        password = fields.get("password", [""])[0]
    except ValueError:
        username, password = "", ""
    if len(username) > 80 or len(password) > 256:
        username, password = "", ""
    peer = request.client.host if request.client else "unknown"
    token = await run_in_threadpool(access.login, username, password, peer)
    if not token:
        return templates.TemplateResponse(request, "pilot_login.html",
                                          {"error": "The username or password was incorrect."}, status_code=401)
    response = RedirectResponse("/", 303)
    response.set_cookie(access.COOKIE, token, max_age=access.SESSION_SECONDS, httponly=True,
                        secure=request.url.scheme == "https", samesite="strict")
    return response


@router.post("/logout")
def logout():
    access.logout()
    response = RedirectResponse("/login", 303)
    response.delete_cookie(access.COOKIE)
    return response
