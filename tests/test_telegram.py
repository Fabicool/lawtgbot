from datetime import datetime

from aiogram.types import Chat, Contact, Document, Message, PhotoSize, User

from app.channels.telegram.bot import message_to_event
from app.channels.telegram.sender import to_markup
from app.messaging.hub import Button, Keyboard

USER = User(id=42, is_bot=False, first_name="Ірина", username="iryna")
CHAT = Chat(id=42, type="private")


def make(**kw) -> Message:
    return Message(message_id=7, date=datetime.now(), chat=CHAT, from_user=USER, **kw)


def test_text_message():
    ev = message_to_event(make(text="Добрий день"))
    assert (ev.chat_id, ev.user_id, ev.username, ev.text, ev.message_id) == (
        42,
        42,
        "iryna",
        "Добрий день",
        7,
    )


def test_photo_takes_largest():
    photos = [
        PhotoSize(file_id="small", file_unique_id="s", width=90, height=90, file_size=1000),
        PhotoSize(file_id="big", file_unique_id="b", width=1280, height=1280, file_size=200000),
    ]
    ev = message_to_event(make(photo=photos, caption="наказ"))
    assert ev.file.file_id == "big" and ev.file.mime == "image/jpeg" and ev.text == "наказ"


def test_document_and_contact():
    doc = Document(
        file_id="d1", file_unique_id="u", file_name="договір.pdf", mime_type="application/pdf", file_size=5
    )
    ev = message_to_event(make(document=doc))
    assert ev.file.filename == "договір.pdf"
    ev = message_to_event(make(contact=Contact(phone_number="380501112233", first_name="І", user_id=42)))
    assert ev.contact_phone == "380501112233" and ev.contact_user_id == 42


def test_keyboards():
    inline = to_markup(Keyboard(rows=[[Button("Так", "yes")]]))
    assert inline.inline_keyboard[0][0].callback_data == "yes"
    reply = to_markup(Keyboard(rows=[[Button("Номер", request_contact=True)]], inline=False))
    assert reply.keyboard[0][0].request_contact is True
    assert to_markup(Keyboard(remove=True)).remove_keyboard is True
