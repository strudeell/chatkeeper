"""Chatkeeper: разовый импорт выгрузки из Telegram Desktop.

Зачем. Бот приносит только то, что пришло после подключения: истории в Bot API
нет вообще, такого метода не существует. Поэтому в первый день продукт пуст
и нем, а человек по пустой сводке решает, что вещь не работает, и уходит.

Штатная выгрузка это лечит и делает ещё две вещи, которых боту не добиться:
даёт список всех чатов (боту список диалогов недоступен) и приносит группы,
куда бот не добавлен и добавлен не будет.

Цена - несколько кликов один раз при установке. Ежедневная выгрузка была бы
издевательством, разовая - нет.

Команды:
    chatkeeper history chats  <путь>          список чатов из выгрузки, без содержимого
    chatkeeper history import <путь> [дней]   влить переписку за последние дни (по умолчанию 7)

Путь - это папка выгрузки или сам файл result.json внутри неё.

Как устроен импорт. Сообщения превращаются в такие же обновления, какие приносит
бот, и кладутся в тот же буфер. Дальше работает общий код: отсев стоп-списка,
склейка по номеру сообщения, сборка файла разбора. Отдельной ветки для импорта
нет намеренно - две дороги к одному файлу разошлись бы в поведении через месяц.
Побочная польза: если одно сообщение придёт и из выгрузки, и от бота, склейка
по номеру оставит одну запись, а не две.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_bot as src  # noqa: E402
from common import die, utf8_console  # noqa: E402

DEFAULT_DAYS = 7

# Что берём. Каналы и переписку с ботами пропускаем: договорённостей там нет,
# а объём большой. «Избранное» - это заметки самому себе, тоже мимо.
CHAT_TYPES = {
    "personal_chat": "private",
    "private_group": "group",
    "private_supergroup": "group",
    "public_supergroup": "group",
}

# Вложение в выгрузке названо иначе, чем в Bot API. Приводим к одному виду,
# чтобы описание вложения считалось общим кодом, а не вторым набором правил.
MEDIA = {
    "voice_message": "voice",
    "video_message": "video_note",
    "sticker": "sticker",
    "animation": "video",
    "video_file": "video",
    "audio_file": "audio",
}


def find_result(raw: str) -> Path:
    """Человек укажет папку, а не файл. Ищем сами и объясняем, если не нашли."""
    path = Path(raw.strip().strip('"'))
    if path.is_dir():
        path = path / "result.json"
    if not path.exists():
        die(
            f"Не нашёл файл выгрузки: {path}. Нужна папка, которую сделал телеграм "
            "при выгрузке, внутри неё лежит result.json. Формат при выгрузке "
            "надо выбирать JSON, а не HTML."
        )
    return path


def load_export(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        die(
            "Файл выгрузки не читается. Скорее всего выгрузка шла в формате HTML: "
            "она для чтения глазами, а не для разбора. Повтори выгрузку, выбрав JSON."
        )
    except OSError:
        die("Файл выгрузки не открывается. Проверь, что он не удалён и не занят.")
    return {}


def owner_id(export: dict) -> int | None:
    person = export.get("personal_information") or {}
    value = person.get("user_id")
    return int(value) if value is not None else None


def chat_list(export: dict) -> list[dict]:
    """Единый список чатов: выгрузка бывает полной и по одному чату."""
    chats = export.get("chats")
    if isinstance(chats, dict) and isinstance(chats.get("list"), list):
        return chats["list"]
    if export.get("messages") is not None:
        return [export]  # выгрузка одного чата: сам корень и есть чат
    return []


def flatten(text) -> str:
    """Текст в выгрузке бывает строкой, а бывает списком кусков с разметкой."""
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        parts = []
        for piece in text:
            if isinstance(piece, str):
                parts.append(piece)
            elif isinstance(piece, dict):
                parts.append(str(piece.get("text", "")))
        return "".join(parts)
    return ""


def when(message: dict) -> int | None:
    """Время сообщения. Берём unixtime: он однозначен, а строка даты - местная."""
    stamp = message.get("date_unixtime")
    if stamp is not None:
        try:
            return int(stamp)
        except (TypeError, ValueError):
            pass
    raw = message.get("date")
    if not raw:
        return None
    try:
        return int(datetime.fromisoformat(raw).timestamp())
    except ValueError:
        return None


def sender_id(message: dict) -> int | None:
    """from_id приходит как user123456 или channel123456."""
    raw = str(message.get("from_id") or "")
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else None


def to_update(message: dict, chat: dict, kind: str) -> dict | None:
    """Сообщение выгрузки -> обновление в том же виде, в каком его приносит бот."""
    if message.get("type") != "message":
        return None  # служебное: кто-то вошёл, сменил аватар, закрепил
    stamp = when(message)
    if stamp is None:
        return None

    body: dict = {
        "message_id": int(message.get("id", 0)),
        "date": stamp,
        "from": {
            "id": sender_id(message),
            "first_name": message.get("from") or "неизвестно кто",
        },
        "chat": {
            "id": int(chat.get("id", 0)),
            "type": kind,
            "first_name" if kind == "private" else "title": chat.get("name")
            or "без названия",
        },
    }

    text = flatten(message.get("text"))
    if text:
        body["text"] = text

    media = MEDIA.get(message.get("media_type") or "")
    if media:
        body[media] = {"duration": message.get("duration_seconds") or 0}
    elif message.get("photo"):
        body["photo"] = [{}]
    elif message.get("file"):
        body["document"] = {"file_name": Path(str(message["file"])).name}

    return {"update_id": 0, "business_message": body}


def cmd_chats(raw: str) -> None:
    """Список чатов без единой строки переписки: он нужен для вопросов человеку."""
    export = load_export(find_result(raw))
    rows = []
    for chat in chat_list(export):
        kind = CHAT_TYPES.get(str(chat.get("type")))
        if kind is None:
            continue
        messages = [m for m in chat.get("messages", []) if m.get("type") == "message"]
        if not messages:
            continue
        last = max((when(m) or 0) for m in messages)
        rows.append(
            {
                "id": int(chat.get("id", 0)),
                "name": chat.get("name") or "без названия",
                "kind": "личный" if kind == "private" else "группа",
                "count": len(messages),
                "last": datetime.fromtimestamp(last, tz=timezone.utc).astimezone(),
            }
        )

    rows.sort(key=lambda r: r["last"], reverse=True)
    print(f"Чатов в выгрузке: {len(rows)}")
    for row in rows:
        print(
            f"  {row['id']:>14}  {row['kind']:<7} {row['last']:%d.%m}  "
            f"{row['count']:>5} сообщ.  {row['name']}"
        )


def cmd_import(raw: str, days: int) -> None:
    export = load_export(find_result(raw))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    forbidden = src.forbidden_chats()

    updates = []
    taken_chats = 0
    skipped_forbidden = 0
    for chat in chat_list(export):
        kind = CHAT_TYPES.get(str(chat.get("type")))
        if kind is None:
            continue
        chat_id = int(chat.get("id", 0))
        if not chat_id:
            continue
        if src.in_stop_list(chat_id, forbidden):
            skipped_forbidden += 1
            continue

        portion = []
        for message in chat.get("messages", []):
            stamp = when(message)
            if stamp is None or stamp < since:
                continue
            update = to_update(message, chat, kind)
            if update is not None:
                portion.append(update)
        if portion:
            taken_chats += 1
            updates.extend(portion)

    if not updates:
        print(
            f"За последние {days} дн. в выгрузке ничего не нашлось. "
            "Возможно, при выгрузке был выбран другой период."
        )
        return

    src.append_updates(updates)

    # Владельца запоминаем: без него все сообщения подписывались бы именами,
    # и разбор не понял бы, где обещания самого человека, а где чужие.
    me = owner_id(export)
    if me is not None:
        state = src.load_state()
        connection = state.get("connection") or {}
        connection.setdefault("user_id", me)
        state["connection"] = connection
        src.save_state(state)

    print(f"Взято чатов: {taken_chats}")
    if skipped_forbidden:
        print(f"Пропущено по стоп-списку: {skipped_forbidden}")
    print(f"Сообщений влито: {len(updates)}")
    print("Дальше обычный разбор: chatkeeper collect fetch")


def main() -> None:
    utf8_console()
    argv = sys.argv[1:]
    if not argv or argv[0] not in {"chats", "import"} or len(argv) < 2:
        print(__doc__)
        sys.exit(2)

    if argv[0] == "chats":
        cmd_chats(argv[1])
        return

    days = DEFAULT_DAYS
    if len(argv) > 2:
        if not argv[2].isdigit():
            die("Число дней должно быть числом. Например: import <путь> 7")
        days = int(argv[2])
    cmd_import(argv[1], days)


if __name__ == "__main__":
    main()
