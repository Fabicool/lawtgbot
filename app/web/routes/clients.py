"""Клієнти, імпорт і експорт Excel."""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import crypto, excel
from app.core.conflicts import find_matches, normalize_name, normalize_phone
from app.core.workflow import audit
from app.db.models import Client, Lawyer, Party, utcnow
from app.db.session import get_session
from app.web.auth import current_lawyer, verify_csrf
from app.web.templating import flash, render

router = APIRouter(dependencies=[Depends(current_lawyer)])

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def xlsx_response(data: bytes, name: str) -> Response:
    stamp = utcnow().strftime("%Y-%m-%d")
    return Response(
        data, media_type=XLSX, headers={"Content-Disposition": f'attachment; filename="{name}-{stamp}.xlsx"'}
    )


@router.get("/clients")
async def clients(request: Request, q: str = "", session: AsyncSession = Depends(get_session)):
    query = select(Client).options(selectinload(Client.cases)).where(Client.full_name != "")
    if q.strip():
        like = f"%{normalize_name(q)}%"
        phone = normalize_phone(q) if any(ch.isdigit() for ch in q) else ""
        conds = [Client.name_norm.like(like)]
        if phone:
            conds.append(Client.phone.like(f"%{phone[-9:]}%"))
        query = query.where(or_(*conds))
    rows = (await session.scalars(query.order_by(Client.full_name).limit(500))).all()
    parties = (await session.scalars(select(Party).order_by(Party.name).limit(500))).all() if not q else []
    return render(request, "clients.html", clients=rows, parties=parties, q=q, nav="clients")


@router.get("/clients/{client_id}")
async def client_page(
    client_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    lawyer: Lawyer = Depends(current_lawyer),
):
    client = await session.scalar(
        select(Client).where(Client.id == client_id).options(selectinload(Client.cases))
    )
    if client is None:
        raise HTTPException(404, "Клієнта не знайдено")
    await audit(session, f"lawyer:{lawyer.username}", "view_client", "client", client.id)
    await session.commit()
    matches = await find_matches(session, client.full_name, exclude_client_id=client.id)
    return render(
        request,
        "client.html",
        client=client,
        tax_id=crypto.decrypt(client.tax_id_enc),
        matches=matches,
        nav="clients",
    )


@router.post("/clients", dependencies=[Depends(verify_csrf)])
async def add_client(
    request: Request,
    full_name: str = Form(...),
    phone: str = Form(""),
    tax_id: str = Form(""),
    note: str = Form(""),
    role: str = Form("client"),
    session: AsyncSession = Depends(get_session),
    lawyer: Lawyer = Depends(current_lawyer),
):
    name = " ".join(full_name.split())
    if not name:
        flash(request, "Вкажіть ПІБ або назву.", "error")
        return RedirectResponse("/clients", status_code=303)
    code = "".join(ch for ch in tax_id if ch.isdigit())
    if role == "client":
        obj = Client(
            full_name=name,
            name_norm=normalize_name(name),
            phone=normalize_phone(phone),
            tax_id_enc=crypto.encrypt(code) if code else "",
            tax_id_hash=crypto.code_hash(code),
            is_company=len(code) == 8,
            source="manual",
            note=note.strip(),
        )
    else:
        obj = Party(
            name=name,
            name_norm=normalize_name(name),
            tax_id_hash=crypto.code_hash(code),
            is_company=len(code) == 8,
            role="опонент",
            note=note.strip(),
        )
    session.add(obj)
    await session.flush()
    await audit(session, f"lawyer:{lawyer.username}", f"add_{role}", role, obj.id)
    await session.commit()
    flash(request, f"Додано: {name}")
    return RedirectResponse("/clients", status_code=303)


@router.get("/clients-template.xlsx")
async def template_xlsx():
    return Response(
        excel.template_bytes(),
        media_type=XLSX,
        headers={"Content-Disposition": 'attachment; filename="shablon-kliienty.xlsx"'},
    )


@router.post("/clients/import", dependencies=[Depends(verify_csrf)])
async def import_clients(
    request: Request,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    lawyer: Lawyer = Depends(current_lawyer),
):
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        flash(request, "Файл завеликий (понад 10 МБ).", "error")
        return RedirectResponse("/clients", status_code=303)
    result = await excel.import_xlsx(session, data)
    await audit(
        session,
        f"lawyer:{lawyer.username}",
        "import_excel",
        details=f"клієнтів {result.clients}, сторін {result.parties}, пропущено {result.skipped}",
    )
    await session.commit()
    flash(
        request,
        f"Імпортовано: клієнтів {result.clients}, інших сторін {result.parties}, "
        f"пропущено дублікатів {result.skipped}.",
    )
    for err in result.errors[:20]:
        flash(request, err, "error")
    return RedirectResponse("/clients", status_code=303)


@router.get("/export/clients.xlsx")
async def export_clients(
    session: AsyncSession = Depends(get_session), lawyer: Lawyer = Depends(current_lawyer)
):
    data = await excel.export_clients_xlsx(session)
    await audit(session, f"lawyer:{lawyer.username}", "export_clients")
    await session.commit()
    return xlsx_response(data, "kliienty")


@router.get("/export/cases.xlsx")
async def export_cases(
    session: AsyncSession = Depends(get_session), lawyer: Lawyer = Depends(current_lawyer)
):
    data = await excel.export_cases_xlsx(session)
    await audit(session, f"lawyer:{lawyer.username}", "export_cases")
    await session.commit()
    return xlsx_response(data, "zvernennia")
