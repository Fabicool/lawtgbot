from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_store import get_value
from app.core.workflow import audit
from app.db.session import get_session
from app.web import auth
from app.web.templating import render

router = APIRouter()


@router.get("/login")
async def login_page(request: Request):
    if request.session.get("lawyer_id"):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


@router.post("/login", dependencies=[Depends(auth.verify_csrf)])
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    code: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    key = request.client.host if request.client else "unknown"
    if auth.too_many_attempts(key):
        return render(request, "login.html", error="Забагато спроб. Зачекайте 15 хвилин.")
    lawyer = await auth.authenticate(session, username, password, code)
    if lawyer is None:
        auth.register_failure(key)
        await audit(session, f"ip:{key}", "login_failed", details=username[:64])
        await session.commit()
        return render(request, "login.html", error="Невірний логін, пароль або код.")
    auth.reset_attempts(key)
    request.session.clear()
    request.session["lawyer_id"] = lawyer.id
    request.session["username"] = lawyer.username
    await audit(session, f"lawyer:{lawyer.username}", "login")
    await session.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/logout", dependencies=[Depends(auth.verify_csrf)])
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/privacy")
async def privacy(request: Request, session: AsyncSession = Depends(get_session)):
    """Публічна сторінка «Як ми захищаємо ваші дані» (посилання в першому повідомленні бота)."""
    return render(
        request,
        "privacy.html",
        lawyer_name=await get_value(session, "lawyer_name"),
        lawyer_phone=await get_value(session, "lawyer_phone"),
        public=True,
    )
