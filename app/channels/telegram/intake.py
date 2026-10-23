"""Приймальник: шлях клієнта від першого повідомлення до «На розгляді» (кроки 1–7 ТЗ).

Поточний крок зберігається в базі (Case.intake_step), тому перезапуск бота не губить розмову.
"""

import asyncio
import logging
import mimetypes
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.agents import analyst, intake_agent
from app.config import get_settings
from app.core import crypto, notify
from app.core.conflicts import Match, find_matches, normalize_name, normalize_phone
from app.core.defaults import CATEGORIES, URGENT_OPTIONS
from app.core.settings_store import document_list, get_int, survey_questions, template
from app.core.statuses import INTAKE_STATUSES, OPEN_STATUSES, CaseStatus, ConflictStatus
from app.core.workflow import audit, set_status
from app.db.models import Case, Client, Document, Party, utcnow
from app.messaging import hub
from app.messaging.hub import Button, Keyboard

log = logging.getLogger(__name__)

HUMAN_BUTTON = "Хочу говорити з людиною"
AWAITING_LAWYER = {CaseStatus.IN_REVIEW, CaseStatus.PREPARING, CaseStatus.APPROVED}
HUMAN_RE = re.compile(r"^\s*(адвокат|адвоката|людина|з людиною|оператор)\s*[.!]?\s*$", re.IGNORECASE)
ACK_INTERVAL_HOURS = 6
ALLOWED_MIME = {"application/pdf", "image/jpeg", "image/png", "image/webp", "image/heic"}
MAX_FILE_BYTES = 20 * 1024 * 1024  # ліміт Telegram Bot API на завантаження


@dataclass
class IncomingFile:
    file_id: str
    filename: str
    mime: str
    size: int


@dataclass
class IncomingEvent:
    chat_id: int
    user_id: int
    username: str = ""
    message_id: int | None = None
    text: str = ""
    callback: str = ""
    contact_phone: str = ""
    contact_user_id: int | None = None
    file: IncomingFile | None = None


FileFetcher = Callable[[str], Awaitable[bytes]]


def _kb(*rows: list[tuple[str, str]]) -> Keyboard:
    return Keyboard(rows=[[Button(text=t, data=d) for t, d in row] for row in rows])


def human_row() -> list[tuple[str, str]]:
    return [(HUMAN_BUTTON, "human")]


class IntakeFlow:
    def __init__(self, sm: async_sessionmaker[AsyncSession], fetch_file: FileFetcher) -> None:
        self.sm = sm
        self.fetch_file = fetch_file
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, chat_id: int) -> asyncio.Lock:
        return self._locks.setdefault(chat_id, asyncio.Lock())

    async def handle(self, ev: IncomingEvent) -> None:
        async with self._lock(ev.chat_id):
            async with self.sm() as session:
                try:
                    await self._handle(session, ev)
                    await session.commit()
                except Exception:
                    await session.rollback()
                    log.exception("Помилка обробки повідомлення від %s", ev.chat_id)
                    raise

    # ---------- допоміжне ----------

    async def _send(
        self, session: AsyncSession, case: Case, client: Client, text: str, kb: Keyboard | None = None
    ) -> None:
        await hub.send_to_client(session, case, client, text, keyboard=kb)

    async def _get_client(self, session: AsyncSession, ev: IncomingEvent) -> Client | None:
        return await session.scalar(select(Client).where(Client.telegram_id == ev.user_id))

    async def _open_case(self, session: AsyncSession, client: Client) -> Case | None:
        return await session.scalar(
            select(Case)
            .where(Case.client_id == client.id, Case.status.in_([s.value for s in OPEN_STATUSES]))
            .order_by(Case.id.desc())
            .options(selectinload(Case.documents), selectinload(Case.messages))
            .limit(1)
        )

    async def _daily_limit_reached(self, session: AsyncSession) -> bool:
        limit = await get_int(session, "daily_limit", 0)
        if limit <= 0:
            return False
        start = datetime.combine(utcnow().date(), time.min)
        count = await session.scalar(
            select(func.count(Case.id)).where(Case.created_at >= start, Case.status != CaseStatus.INACTIVE)
        )
        return (count or 0) >= limit

    # ---------- головний диспетчер ----------

    async def _handle(self, session: AsyncSession, ev: IncomingEvent) -> None:
        client = await self._get_client(session, ev)
        case = await self._open_case(session, client) if client else None
        text = ev.text.strip()

        if text.startswith("/start") or case is None:
            if case is not None and case.status in INTAKE_STATUSES and case.handoff == "assistant":
                await self._send(session, case, client, "Продовжимо з того місця, де зупинились.")
                await self._repeat_step(session, case, client)
                return
            if case is not None and (case.status in AWAITING_LAWYER or case.handoff == "lawyer"):
                # Звернення вже в адвоката — не створюємо дубль, а нагадуємо про нього.
                await self._send(
                    session,
                    case,
                    client,
                    f"Ваше звернення №{case.id} уже в адвоката. "
                    "Якщо хочете щось додати — просто напишіть тут.",
                )
                return
            await self._start(session, ev, client)
            return

        assert client is not None
        if ev.callback == "human" or (text and HUMAN_RE.match(text)):
            if text:
                hub.record_incoming(session, case, text)
            await self._handoff(session, case, client)
            return

        # Адвокат перехопив чат або справа вже в адвоката — асистент лише передає повідомлення.
        if case.handoff == "lawyer" or case.status not in INTAKE_STATUSES:
            await self._forward_to_lawyer(session, case, client, ev)
            return

        step = case.intake_step
        handler = getattr(self, f"_step_{step}", None)
        if handler is None:
            await self._forward_to_lawyer(session, case, client, ev)
            return
        if ev.file and step not in {"consent", "conflict_wait"}:
            await self._save_file(session, case, client, ev.file, reply=step == "documents")
            return
        await handler(session, case, client, ev)

    async def _start(self, session: AsyncSession, ev: IncomingEvent, client: Client | None) -> None:
        if client is None:
            client = Client(telegram_id=ev.user_id, telegram_username=ev.username or "", source="bot")
            session.add(client)
            await session.flush()
        if await self._daily_limit_reached(session):
            # Звернення не створюємо: клієнт отримує лише повідомлення про ліміт.
            sender = hub.get_sender("telegram")
            if sender is not None:
                await sender.send_text(ev.chat_id, await template(session, "daily_limit"))
            return
        case = Case(client_id=client.id, channel="telegram", intake_step="consent", intake={})
        session.add(case)
        await session.flush()
        if ev.text and not ev.text.startswith("/start"):
            hub.record_incoming(session, case, ev.text)
        await audit(session, "bot", "case_created", "case", case.id)
        await self._ask_consent(session, case, client)

    async def _repeat_step(self, session: AsyncSession, case: Case, client: Client) -> None:
        step = case.intake_step
        if step == "consent":
            await self._ask_consent(session, case, client)
        elif step == "urgent":
            await self._ask_urgent(session, case, client)
        elif step == "name":
            await self._send(session, case, client, await template(session, "ask_name"))
        elif step == "phone":
            await self._ask_phone(session, case, client)
        elif step == "tax_id":
            await self._send(
                session,
                case,
                client,
                await template(session, "ask_tax_id"),
                _kb([("Пропустити", "skip_tax")]),
            )
        elif step == "opponent":
            await self._ask_opponent(session, case, client)
        elif step == "category":
            await self._ask_category(session, case, client)
        elif step == "survey":
            await self._ask_current_question(session, case, client)
        elif step == "documents":
            await self._ask_documents(session, case, client)
        elif step == "conflict_wait":
            await self._send(session, case, client, await template(session, "conflict_pending"))

    # ---------- крок 1: згода ----------

    async def _ask_consent(self, session: AsyncSession, case: Case, client: Client) -> None:
        await self._send(
            session,
            case,
            client,
            await template(session, "greeting"),
            _kb([("Погоджуюсь", "consent_yes")], [("Лише контакт адвоката", "consent_no")]),
        )

    async def _step_consent(
        self, session: AsyncSession, case: Case, client: Client, ev: IncomingEvent
    ) -> None:
        if ev.text:
            hub.record_incoming(session, case, ev.text)
        if ev.callback == "consent_yes":
            client.consent_at = utcnow()
            await audit(session, "bot", "consent_given", "client", client.id)
            case.intake_step = "urgent"
            await self._ask_urgent(session, case, client)
        elif ev.callback == "consent_no":
            await self._send(session, case, client, await template(session, "no_consent"))
            await set_status(session, case, CaseStatus.CLOSED, "bot")
        else:
            await self._ask_consent(session, case, client)

    # ---------- крок 2: терміновий фільтр ----------

    async def _ask_urgent(self, session: AsyncSession, case: Case, client: Client) -> None:
        rows = [[(label, f"urgent:{key}")] for key, label in URGENT_OPTIONS.items()]
        await self._send(session, case, client, await template(session, "urgent_question"), _kb(*rows))

    async def _step_urgent(
        self, session: AsyncSession, case: Case, client: Client, ev: IncomingEvent
    ) -> None:
        urgent, reason = False, ""
        if ev.callback.startswith("urgent:"):
            key = ev.callback.split(":", 1)[1]
            if key not in URGENT_OPTIONS:
                await self._ask_urgent(session, case, client)
                return
            hub.record_incoming(session, case, URGENT_OPTIONS[key])
            urgent, reason = key != "none", URGENT_OPTIONS[key]
        elif ev.text:
            hub.record_incoming(session, case, ev.text)
            case.intake = {**case.intake, "urgent_text": ev.text}
            urgent, reason = await intake_agent.check_urgency(ev.text)
        else:
            await self._ask_urgent(session, case, client)
            return
        if urgent:
            await self._mark_urgent(session, case, client, reason)
        case.intake_step = "name"
        await self._send(session, case, client, await template(session, "ask_name"))

    async def _mark_urgent(self, session: AsyncSession, case: Case, client: Client, reason: str) -> None:
        if case.is_urgent:
            return
        case.is_urgent = True
        case.urgency_reason = reason
        case.unread = True
        await audit(session, "bot", "urgent", "case", case.id, reason)
        await self._send(session, case, client, await template(session, "urgent_reply"))
        await notify.notify_lawyer("urgent", case.id, f"Причина: {reason}")

    # ---------- крок 3: верифікація (ПІБ + телефон) ----------

    async def _step_name(self, session: AsyncSession, case: Case, client: Client, ev: IncomingEvent) -> None:
        name = " ".join(ev.text.split())
        if not name or len(name) < 3 or len(name) > 200 or any(ch.isdigit() for ch in name):
            await self._send(session, case, client, await template(session, "ask_name"))
            return
        hub.record_incoming(session, case, name)
        client.full_name = name
        client.name_norm = normalize_name(name)
        case.intake_step = "phone"
        await self._ask_phone(session, case, client)

    async def _ask_phone(self, session: AsyncSession, case: Case, client: Client) -> None:
        kb = Keyboard(rows=[[Button("Поділитися номером", request_contact=True)]], inline=False)
        first = client.full_name.split()[1] if len(client.full_name.split()) > 1 else client.full_name
        await self._send(session, case, client, await template(session, "ask_phone", name=first), kb)

    async def _step_phone(self, session: AsyncSession, case: Case, client: Client, ev: IncomingEvent) -> None:
        raw = ev.contact_phone or ev.text
        phone = normalize_phone(raw)
        if len(phone) < 12 or len(phone) > 16:
            await self._ask_phone(session, case, client)
            return
        hub.record_incoming(session, case, phone if ev.contact_phone else ev.text)
        client.phone = phone
        await set_status(session, case, CaseStatus.VERIFIED, "bot")
        await self._send(session, case, client, "Дякую!", Keyboard(remove=True))
        # Чи є в базі хтось із таким самим ПІБ? Тоді просимо РНОКПП, щоб відрізнити однофамільців.
        namesakes = [
            m
            for m in await find_matches(session, client.full_name, exclude_client_id=client.id)
            if m.reason in {"exact_name", "fuzzy_name"}
        ]
        if namesakes:
            case.intake = {**case.intake, "namesakes": [m.as_dict() for m in namesakes]}
            case.intake_step = "tax_id"
            await self._send(
                session,
                case,
                client,
                await template(session, "ask_tax_id"),
                _kb([("Пропустити", "skip_tax")]),
            )
        else:
            case.intake_step = "opponent"
            await set_status(session, case, CaseStatus.CONFLICT_CHECK, "bot")
            await self._ask_opponent(session, case, client)

    async def _step_tax_id(
        self, session: AsyncSession, case: Case, client: Client, ev: IncomingEvent
    ) -> None:
        if ev.callback == "skip_tax":
            hub.record_incoming(session, case, "Пропустити")
        else:
            digits = "".join(ch for ch in ev.text if ch.isdigit())
            # Повідомлення з кодом не зберігаємо і видаляємо з чату — ШІ його не побачить.
            hub.record_incoming(session, case, ev.text, hidden=True)
            if ev.message_id:
                sender = hub.get_sender("telegram")
                if sender:
                    await sender.delete_message(ev.chat_id, ev.message_id)
            if len(digits) != 10:
                await self._send(
                    session,
                    case,
                    client,
                    await template(session, "tax_id_invalid"),
                    _kb([("Пропустити", "skip_tax")]),
                )
                return
            client.tax_id_enc = crypto.encrypt(digits)
            client.tax_id_hash = crypto.code_hash(digits)
            same = await find_matches(
                session, client.full_name, tax_id_hash=client.tax_id_hash, exclude_client_id=client.id
            )
            verdicts = []
            for m in case.intake.get("namesakes", []):
                is_same = any(
                    s.kind == m["kind"] and s.ref_id == m["ref_id"] and s.reason == "tax_id" for s in same
                )
                verdicts.append({**m, "same_person": is_same})
            case.intake = {**case.intake, "namesakes": verdicts}
            await audit(session, "bot", "tax_id_checked", "client", client.id)
        case.intake_step = "opponent"
        await set_status(session, case, CaseStatus.CONFLICT_CHECK, "bot")
        await self._ask_opponent(session, case, client)

    # ---------- крок 4: перевірка конфліктів ----------

    async def _ask_opponent(self, session: AsyncSession, case: Case, client: Client) -> None:
        await self._send(
            session, case, client, await template(session, "ask_opponent"), _kb([("Немає", "no_opponent")])
        )

    async def _step_opponent(
        self, session: AsyncSession, case: Case, client: Client, ev: IncomingEvent
    ) -> None:
        opponent = "" if ev.callback == "no_opponent" else " ".join(ev.text.split())[:255]
        if ev.callback != "no_opponent" and not opponent:
            await self._ask_opponent(session, case, client)
            return
        hub.record_incoming(session, case, opponent or "Немає")
        if opponent:
            session.add(
                Party(case_id=case.id, name=opponent, name_norm=normalize_name(opponent), role="опонент")
            )
            case.intake = {**case.intake, "opponent": opponent}
        matches = await self.conflict_matches(session, case, client, opponent)
        case.conflict_matches = [m.as_dict() for m in matches]
        if matches:
            case.conflict_status = ConflictStatus.POSSIBLE
            case.intake_step = "conflict_wait"
            case.unread = True
            await audit(session, "bot", "conflict_possible", "case", case.id, f"{len(matches)} збігів")
            await self._send(session, case, client, await template(session, "conflict_pending"))
            await notify.notify_lawyer(
                "in_review", case.id, "Можливий конфлікт інтересів — потрібне рішення."
            )
            return
        case.conflict_status = ConflictStatus.CLEAR
        await self.start_survey(session, case, client)

    @staticmethod
    async def conflict_matches(
        session: AsyncSession, case: Case, client: Client, opponent: str
    ) -> list[Match]:
        """Конфлікт, якщо: друга сторона — наш клієнт, або клієнт — друга сторона в іншій справі."""
        result: list[Match] = []
        if opponent:
            result += [
                m
                for m in await find_matches(
                    session, opponent, exclude_client_id=client.id, exclude_case_id=case.id
                )
                if m.kind == "client"
            ]
        same_person_ids = {
            (n["kind"], n["ref_id"]) for n in case.intake.get("namesakes", []) if n.get("same_person")
        }
        for m in await find_matches(
            session,
            client.full_name,
            phone=client.phone,
            tax_id_hash=client.tax_id_hash,
            exclude_client_id=client.id,
            exclude_case_id=case.id,
        ):
            if m.kind == "party":
                result.append(m)
            elif (m.kind, m.ref_id) in same_person_ids or m.reason in {"tax_id", "phone"}:
                # Той самий клієнт уже є в базі (напр., з імпорту) — це не конфлікт, а повторне звернення.
                case.intake = {**case.intake, "returning_client_id": m.ref_id}
        return result

    # ---------- крок 5: опитування ----------

    async def start_survey(self, session: AsyncSession, case: Case, client: Client) -> None:
        await set_status(session, case, CaseStatus.SURVEY, "bot", force=True)
        case.intake_step = "category"
        await self._send(session, case, client, await template(session, "survey_intro"))
        await self._ask_category(session, case, client)

    async def _ask_category(self, session: AsyncSession, case: Case, client: Client) -> None:
        items = list(CATEGORIES.items())
        rows = [[(label, f"cat:{key}") for key, label in items[i : i + 2]] for i in range(0, len(items), 2)]
        await self._send(session, case, client, await template(session, "ask_category"), _kb(*rows))

    async def _step_category(
        self, session: AsyncSession, case: Case, client: Client, ev: IncomingEvent
    ) -> None:
        key = ev.callback.split(":", 1)[1] if ev.callback.startswith("cat:") else ""
        if key not in CATEGORIES:
            if ev.text:
                hub.record_incoming(session, case, ev.text)
                key = "other"
                case.intake = {**case.intake, "category_text": ev.text}
            else:
                await self._ask_category(session, case, client)
                return
        else:
            hub.record_incoming(session, case, CATEGORIES[key])
        case.category = key
        questions = await survey_questions(session, key)
        case.intake = {**case.intake, "questions": questions, "q_index": 0, "answers": {}, "rounds": 0}
        case.intake_step = "survey"
        await self._ask_current_question(session, case, client)

    async def _ask_current_question(self, session: AsyncSession, case: Case, client: Client) -> None:
        questions = case.intake.get("questions", [])
        idx = case.intake.get("q_index", 0)
        if idx >= len(questions):
            await self._finish_survey(session, case, client)
            return
        total = len(questions)
        await self._send(session, case, client, f"({idx + 1}/{total}) {questions[idx]}", _kb(human_row()))

    async def _step_survey(
        self, session: AsyncSession, case: Case, client: Client, ev: IncomingEvent
    ) -> None:
        if not ev.text:
            await self._ask_current_question(session, case, client)
            return
        hub.record_incoming(session, case, ev.text)
        questions = case.intake.get("questions", [])
        idx = case.intake.get("q_index", 0)
        answers = dict(case.intake.get("answers", {}))
        if idx < len(questions):
            answers[questions[idx]] = ev.text
        case.intake = {**case.intake, "answers": answers, "q_index": idx + 1}
        if not case.is_urgent:
            urgent, reason = await intake_agent.check_urgency(ev.text)
            if urgent:
                await self._mark_urgent(session, case, client, reason)
        await self._ask_current_question(session, case, client)

    async def _finish_survey(self, session: AsyncSession, case: Case, client: Client) -> None:
        rounds = case.intake.get("rounds", 0)
        max_rounds = await get_int(session, "max_clarify_rounds", 2)
        if rounds < max_rounds:
            result = await intake_agent.clarify(
                case.intake.get("answers", {}), case.intake.get("questions", [])
            )
            if result is not None:
                if case.category in {"", "other"} and result.category != "other":
                    case.category = result.category
                if result.questions:
                    questions = case.intake.get("questions", []) + result.questions
                    case.intake = {**case.intake, "questions": questions, "rounds": rounds + 1}
                    await self._send(session, case, client, "Дякую! Ще кілька уточнень:")
                    await self._ask_current_question(session, case, client)
                    return
        case.intake_step = "documents"
        await self._ask_documents(session, case, client)

    # ---------- крок 6: документи ----------

    async def _ask_documents(self, session: AsyncSession, case: Case, client: Client) -> None:
        docs = await document_list(session, case.category)
        listing = "\n".join(f"• {d}" for d in docs)
        await self._send(
            session,
            case,
            client,
            await template(session, "docs_intro", documents=listing),
            _kb([("Готово", "docs_done")], human_row()),
        )

    async def _save_file(
        self, session: AsyncSession, case: Case, client: Client, f: IncomingFile, *, reply: bool
    ) -> None:
        mime = f.mime or mimetypes.guess_type(f.filename)[0] or ""
        if mime not in ALLOWED_MIME:
            await self._send(
                session, case, client, "Приймаю лише фото та PDF. Спробуйте, будь ласка, інший формат."
            )
            return
        if f.size > MAX_FILE_BYTES:
            await self._send(
                session, case, client, "Файл завеликий (понад 20 МБ). Надішліть, будь ласка, менший."
            )
            return
        data = await self.fetch_file(f.file_id)
        folder = get_settings().files_dir / f"case_{case.id}"
        folder.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^\w.\-]+", "_", f.filename)[:120] or "file"
        n = len(case.documents) + 1
        path = folder / f"{n:02d}_{safe_name}"
        path.write_bytes(data)
        doc = Document(case_id=case.id, filename=f.filename, path=str(path), mime=mime, size=len(data))
        session.add(doc)
        case.documents.append(doc)
        hub.record_incoming(session, case, f"[файл] {f.filename}")
        check = await intake_agent.check_document(data, mime)
        if check is not None:
            doc.readable = check.readable
            doc.check_note = check.note
            if not check.readable:
                await self._send(
                    session,
                    case,
                    client,
                    await template(
                        session, "doc_unreadable", filename=f.filename, note=check.note or "нечітко"
                    ),
                )
                return
        if reply:
            await self._send(
                session,
                case,
                client,
                await template(session, "doc_received", filename=f.filename),
                _kb([("Готово", "docs_done")]),
            )

    async def _step_documents(
        self, session: AsyncSession, case: Case, client: Client, ev: IncomingEvent
    ) -> None:
        if ev.callback == "docs_done" or ev.text.lower() in {"готово", "все", "усе", "немає"}:
            if ev.text:
                hub.record_incoming(session, case, ev.text)
            await self._submit(session, case, client)
            return
        if ev.text:
            # Додаткова інформація від клієнта — зберігаємо як доповнення до опитування.
            hub.record_incoming(session, case, ev.text)
            extra = case.intake.get("extra", []) + [ev.text]
            case.intake = {**case.intake, "extra": extra}
            await self._send(
                session,
                case,
                client,
                "Записав. Надішліть документи або натисніть «Готово».",
                _kb([("Готово", "docs_done")]),
            )

    # ---------- крок 7: підтвердження прийому + Аналітик ----------

    async def _submit(self, session: AsyncSession, case: Case, client: Client) -> None:
        if case.documents:
            await set_status(session, case, CaseStatus.DOCUMENTS, "bot", force=True)
        await self._send(session, case, client, "Дякую! Опрацьовую матеріали, це займе до хвилини…")
        await session.flush()
        card = await analyst.build_card(case)
        case.card = card
        case.title = card.get("title") or case.title or f"Звернення #{case.id}"
        if card.get("category") and case.category in {"", "other"}:
            case.category = card["category"]
        case.is_complex = card.get("complexity") == "complex"
        if not case.is_urgent and any(d.get("urgent") for d in card.get("deadlines", [])):
            await self._mark_urgent(session, case, client, "Аналітик знайшов близький строк")
        await set_status(session, case, CaseStatus.IN_REVIEW, "bot", force=True)
        case.intake_step = "done"
        case.unread = True
        key = "response_days_complex" if case.is_complex else "response_days"
        days = await get_int(session, key, 7 if case.is_complex else 2)
        tpl = "received_ack_complex" if case.is_complex else "received_ack"
        await self._send(session, case, client, await template(session, tpl, days=days), _kb(human_row()))
        await notify.notify_lawyer("in_review", case.id)

    # ---------- передача адвокату ----------

    async def _handoff(self, session: AsyncSession, case: Case, client: Client) -> None:
        case.handoff = "lawyer"
        case.unread = True
        await audit(session, "bot", "handoff_requested", "case", case.id)
        await self._send(session, case, client, await template(session, "handoff"), Keyboard(remove=True))
        await notify.notify_lawyer("handoff", case.id)

    async def _forward_to_lawyer(
        self, session: AsyncSession, case: Case, client: Client, ev: IncomingEvent
    ) -> None:
        if ev.file:
            await self._save_file(session, case, client, ev.file, reply=False)
        elif ev.text:
            hub.record_incoming(session, case, ev.text)
        elif ev.contact_phone:
            hub.record_incoming(session, case, ev.contact_phone)
        else:
            return
        case.unread = True
        last_ack = case.intake.get("ack_at")
        now = utcnow()
        if (
            not last_ack
            or (now - datetime.fromisoformat(last_ack)).total_seconds() > ACK_INTERVAL_HOURS * 3600
        ):
            case.intake = {**case.intake, "ack_at": now.isoformat()}
            await self._send(session, case, client, await template(session, "lawyer_will_answer"))
            await notify.notify_lawyer("client_message", case.id)
