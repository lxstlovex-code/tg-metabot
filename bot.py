import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, FSInputFile, Message, InlineKeyboardButton,
    InlineKeyboardMarkup
)
from dotenv import load_dotenv
from mutagen.id3 import (
    ID3, ID3NoHeaderError, TIT2, TPE1, TALB, TDRC, TCON, TRCK, TPOS,
    APIC, PictureType
)
from mutagen.mp3 import MP3

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Put your token into .env")

MAX_FILE_MB = 20
BASE = Path("storage")
BASE.mkdir(exist_ok=True)

dp = Dispatcher()
logging.basicConfig(level=logging.INFO)


class Edit(StatesGroup):
    value = State()
    all_values = State()
    cover = State()


def user_dir(user_id: int) -> Path:
    p = BASE / str(user_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_tags(path: Path) -> dict:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()

    def one(key):
        frame = tags.get(key)
        if not frame:
            return "—"
        return str(frame.text[0]) if getattr(frame, "text", None) else "—"

    return {
        "title": one("TIT2"),
        "artist": one("TPE1"),
        "album": one("TALB"),
        "year": one("TDRC"),
        "genre": one("TCON"),
        "track": one("TRCK"),
        "disc": one("TPOS"),
        "has_cover": bool(tags.getall("APIC")),
    }


def render_tags(path: Path) -> str:
    t = get_tags(path)
    cover = "есть 🖼" if t["has_cover"] else "нет"
    return (
        f"🎵 <b>{path.name}</b>\n\n"
        f"Название: <b>{t['title']}</b>\n"
        f"Исполнитель: <b>{t['artist']}</b>\n"
        f"Альбом: <b>{t['album']}</b>\n"
        f"Год: <b>{t['year']}</b>\n"
        f"Жанр: <b>{t['genre']}</b>\n"
        f"Трек: <b>{t['track']}</b>\n"
        f"Диск: <b>{t['disc']}</b>\n"
        f"Обложка: <b>{cover}</b>"
    )


def keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Название", callback_data="edit:title"),
            InlineKeyboardButton(text="🎤 Исполнитель", callback_data="edit:artist"),
        ],
        [
            InlineKeyboardButton(text="💿 Альбом", callback_data="edit:album"),
            InlineKeyboardButton(text="📅 Год", callback_data="edit:year"),
        ],
        [
            InlineKeyboardButton(text="🎼 Жанр", callback_data="edit:genre"),
            InlineKeyboardButton(text="🔢 Трек", callback_data="edit:track"),
        ],
        [
            InlineKeyboardButton(text="💿 Диск", callback_data="edit:disc"),
            InlineKeyboardButton(text="🖼 Обложка", callback_data="edit:cover"),
        ],
        [
            InlineKeyboardButton(text="📝 Изменить всё", callback_data="edit:all"),
        ],
        [
            InlineKeyboardButton(text="🧹 Очистить метаданные", callback_data="edit:clear"),
        ],
        [
            InlineKeyboardButton(text="📥 Получить MP3", callback_data="edit:send"),
        ],
    ])


FIELD_NAMES = {
    "title": ("Название", "TIT2"),
    "artist": ("Исполнитель", "TPE1"),
    "album": ("Альбом", "TALB"),
    "year": ("Год", "TDRC"),
    "genre": ("Жанр", "TCON"),
    "track": ("Номер трека", "TRCK"),
    "disc": ("Номер диска", "TPOS"),
}


def set_field(path: Path, field: str, value: str):
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()

    frame_cls = {
        "title": TIT2, "artist": TPE1, "album": TALB,
        "year": TDRC, "genre": TCON, "track": TRCK, "disc": TPOS
    }[field]

    tags.delall(FIELD_NAMES[field][1])
    tags.add(frame_cls(encoding=3, text=[value]))
    tags.save(path, v2_version=3)


def clear_tags(path: Path):
    try:
        tags = ID3(path)
        tags.delete(path, delete_v1=True, delete_v2=True)
    except ID3NoHeaderError:
        pass


def set_cover(path: Path, image_path: Path):
    mime = "image/jpeg" if image_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    data = image_path.read_bytes()

    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()

    tags.delall("APIC")
    tags.add(APIC(
        encoding=3,
        mime=mime,
        type=PictureType.COVER_FRONT,
        desc="Cover",
        data=data
    ))
    tags.save(path, v2_version=3)


def safe_output_name(path: Path) -> Path:
    t = get_tags(path)
    title = t["title"] if t["title"] != "—" else path.stem
    title = re.sub(r'[\\/:*?"<>|]', "_", title).strip() or "edited"
    return path.with_name(f"{title}.mp3")


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "👋 <b>MP3 Metadata Bot</b>\n\n"
        "Отправь мне MP3-файл, и я покажу его метаданные.\n"
        "После этого можно менять теги и обложку.",
        parse_mode="HTML"
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "🎵 Отправь MP3 как <b>файл</b>, а не как голосовое сообщение.\n\n"
        "Я умею менять название, исполнителя, альбом, год, жанр, "
        "номер трека/диска и встроенную обложку.",
        parse_mode="HTML"
    )


@dp.message(F.document)
async def receive_document(message: Message):
    doc = message.document
    name = doc.file_name or "audio.mp3"

    if not name.lower().endswith(".mp3"):
        await message.answer("❌ Нужен именно MP3-файл.")
        return

    size = doc.file_size or 0
    if size > MAX_FILE_MB * 1024 * 1024:
        await message.answer(f"❌ Файл слишком большой. Лимит этой версии — {MAX_FILE_MB} МБ.")
        return

    d = user_dir(message.from_user.id)
    path = d / "current.mp3"

    await message.bot.download(doc, destination=path)

    try:
        MP3(path)
    except Exception:
        path.unlink(missing_ok=True)
        await message.answer("❌ Не удалось прочитать MP3.")
        return

    await message.answer(render_tags(path), reply_markup=keyboard(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("edit:"))
async def callbacks(call: CallbackQuery, state: FSMContext):
    action = call.data.split(":", 1)[1]
    path = user_dir(call.from_user.id) / "current.mp3"

    if not path.exists():
        await call.answer("Сначала отправь MP3.", show_alert=True)
        return

    if action == "send":
        out = safe_output_name(path)
        if out != path:
            shutil.copy2(path, out)
        await call.message.answer_document(
            FSInputFile(out),
            caption="✅ Готово"
        )
        out.unlink(missing_ok=True)
        await call.answer()
        return

    if action == "clear":
        clear_tags(path)
        await call.message.edit_text(
            "🧹 Метаданные очищены.\n\n" + render_tags(path),
            reply_markup=keyboard(),
            parse_mode="HTML"
        )
        await call.answer("Готово")
        return

    if action == "cover":
        await state.set_state(Edit.cover)
        await call.message.answer("🖼 Пришли картинку JPG или PNG для обложки.")
        await call.answer()
        return

    if action == "all":
        await state.set_state(Edit.all_values)
        await call.message.answer(
            "Отправь одной строкой в формате:\n\n"
            "<code>Название | Исполнитель | Альбом | Год | Жанр | Трек | Диск</code>\n\n"
            "Пример:\n"
            "<code>12pM | almazik | demo | 2026 | Rap | 1 | 1</code>",
            parse_mode="HTML"
        )
        await call.answer()
        return

    if action in FIELD_NAMES:
        await state.update_data(field=action)
        await state.set_state(Edit.value)
        await call.message.answer(f"✏️ Введи новое значение: <b>{FIELD_NAMES[action][0]}</b>", parse_mode="HTML")
        await call.answer()


@dp.message(Edit.value)
async def edit_value(message: Message, state: FSMContext):
    data = await state.get_data()
    field = data["field"]
    path = user_dir(message.from_user.id) / "current.mp3"

    value = (message.text or "").strip()
    if not value:
        await message.answer("❌ Значение не может быть пустым.")
        return

    set_field(path, field, value)
    await state.clear()
    await message.answer("✅ Изменено.\n\n" + render_tags(path), reply_markup=keyboard(), parse_mode="HTML")


@dp.message(Edit.all_values)
async def edit_all(message: Message, state: FSMContext):
    path = user_dir(message.from_user.id) / "current.mp3"
    parts = [x.strip() for x in (message.text or "").split("|")]

    if len(parts) != 7:
        await message.answer("❌ Нужно ровно 7 значений, разделённых символом |.")
        return

    for field, value in zip(FIELD_NAMES.keys(), parts):
        if value:
            set_field(path, field, value)

    await state.clear()
    await message.answer("✅ Всё изменено.\n\n" + render_tags(path), reply_markup=keyboard(), parse_mode="HTML")


@dp.message(Edit.cover)
async def edit_cover(message: Message, state: FSMContext):
    if not message.photo:
        await message.answer("❌ Пришли именно изображение JPG или PNG.")
        return

    path = user_dir(message.from_user.id) / "current.mp3"
    photo = message.photo[-1]

    temp = user_dir(message.from_user.id) / "cover.jpg"
    await message.bot.download(photo, destination=temp)

    set_cover(path, temp)
    temp.unlink(missing_ok=True)
    await state.clear()

    await message.answer("🖼 Обложка установлена.\n\n" + render_tags(path),
                         reply_markup=keyboard(), parse_mode="HTML")


async def main():
    bot = Bot(TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
