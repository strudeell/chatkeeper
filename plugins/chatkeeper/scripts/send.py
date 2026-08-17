"""Chatkeeper: доставка сводки в телеграм и файлы встреч.

Работает только через Bot API: бот шлёт сообщения владельцу и ничего больше.
Доступа к переписке у него нет по устройству самого Telegram.

Зависимостей нет - только стандартная библиотека. Для коробочного продукта
каждая лишняя библиотека это лишний повод сломаться на чужой машине.

Команды:
    test           - проверить канал доставки
    ics-demo       - собрать и прислать пробную встречу
"""

from __future__ import annotations

import json
import mimetypes
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import STATE, die, load_env, utf8_console  # noqa: E402

API = "https://api.telegram.org/bot{token}/{method}"


def credentials() -> tuple[str, str]:
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    owner = env.get("TELEGRAM_OWNER_ID", "")
    if not token:
        die("В .env нет TELEGRAM_BOT_TOKEN. Получить у @BotFather.")
    if not owner:
        die("В .env нет TELEGRAM_OWNER_ID. Нажми /start своему боту.")
    return token, owner


# --- транспорт ----------------------------------------------------------


def explain_http(code: int, body: str) -> str:
    """Код ответа - человеку. Он не должен видеть ни цифр, ни английского."""
    if code == 401:
        return (
            "Токен бота не подошёл. Скорее всего он скопирован не целиком "
            "или бот удалён. Возьми новый токен у @BotFather."
        )
    if code == 403:
        return (
            "Бот не может тебе написать первым - так устроен Telegram. "
            "Открой чат со своим ботом и нажми кнопку Start."
        )
    if code == 429:
        return (
            "Телеграм просит подождать: слишком много обращений подряд. "
            "Через несколько минут всё пройдёт само."
        )
    if code == 400:
        try:
            described = json.loads(body).get("description", "")
        except (json.JSONDecodeError, AttributeError):
            described = ""
        if "chat not found" in described.lower():
            return (
                "Бот не знает, кому писать. Открой чат с ботом, нажми Start "
                "и повтори настройку."
            )
        return f"Телеграм не принял запрос: {described or 'причина не указана'}"
    return f"Телеграм ответил отказом (код {code})."


NO_CONNECTION = (
    "Не получилось связаться с Telegram. Похоже, пропал интернет "
    "или отключился VPN. Включи и попробуй снова."
)


def _call(token: str, method: str, payload: dict) -> dict:
    """POST в Bot API. Токен в сообщениях об ошибках не показывается."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        API.format(token=token, method=method),
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        die(explain_http(error.code, error.read().decode("utf-8", errors="replace")))
    except urllib.error.URLError:
        die(NO_CONNECTION)
    except OSError:
        # Обрыв соединения и таймаут чтения мимо URLError не проходят молча,
        # но приходят как разные исключения - человеку всё равно, какое именно.
        die(NO_CONNECTION)
    return {}


def _call_multipart(token: str, method: str, fields: dict, file: Path) -> dict:
    """Отправка файла. Multipart собирается руками, чтобы не тащить зависимость."""
    boundary = uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += f"{value}\r\n".encode()

    mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    body += f"--{boundary}\r\n".encode()
    body += (
        f'Content-Disposition: form-data; name="document"; filename="{file.name}"\r\n'
    ).encode()
    body += f"Content-Type: {mime}\r\n\r\n".encode()
    body += file.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()

    request = urllib.request.Request(
        API.format(token=token, method=method),
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        die(explain_http(error.code, error.read().decode("utf-8", errors="replace")))
    except urllib.error.URLError:
        die(NO_CONNECTION)
    return {}


TELEGRAM_LIMIT = 4096  # жёсткий предел Telegram на одно сообщение
CHUNK = 3800           # с запасом: разметка и склейка добавляют символов


def split_text(text: str, limit: int = CHUNK) -> list[str]:
    """Режет длинный текст по границам строк.

    Резать по строкам, а не по символам, обязательно: разметка в сводке живёт
    внутри строк, и разрыв посередине тега испортил бы сообщение.
    """
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    current: list[str] = []
    size = 0

    for line in text.split("\n"):
        while len(line) > limit:  # одна строка длиннее предела - редкость, но бывает
            if current:
                parts.append("\n".join(current))
                current, size = [], 0
            parts.append(line[:limit])
            line = line[limit:]
        if size + len(line) + 1 > limit and current:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1

    if current:
        parts.append("\n".join(current))
    return parts


def send_message(text: str, parse_mode: str | None = None) -> None:
    """Отправляет сообщение владельцу, разбивая слишком длинное на части.

    Без разбиения сводка на несколько десятков обещаний просто не уходит,
    и человек видит тишину - а тишина неотличима от «сегодня дел нет».
    """
    token, owner = credentials()
    parts = split_text(text)

    for number, part in enumerate(parts, start=1):
        if len(parts) > 1:
            part = f"{part}\n\n<i>часть {number} из {len(parts)}</i>"
        payload: dict = {"chat_id": owner, "text": part}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        result = _call(token, "sendMessage", payload)
        if not result.get("ok"):
            die(f"Сообщение не ушло: {result.get('description', 'причина неизвестна')}")


def send_document(path: Path, caption: str = "") -> None:
    token, owner = credentials()
    fields = {"chat_id": owner}
    if caption:
        fields["caption"] = caption
    result = _call_multipart(token, "sendDocument", fields, path)
    if not result.get("ok"):
        die(f"Файл не ушёл: {result.get('description', 'причина неизвестна')}")


# --- встречи ------------------------------------------------------------


def _escape(text: str) -> str:
    """В формате календаря запятая и точка с запятой - служебные символы."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def fold(line: str, limit: int = 72) -> str:
    """Длинные строки календарь требует переносить, иначе может отказаться читать.

    Продолжение строки помечается пробелом в начале. Считаем по байтам:
    в кириллице один символ занимает два, и предел берётся вдвое быстрее.
    """
    raw = line.encode("utf-8")
    if len(raw) <= limit:
        return line

    parts, current = [], ""
    for char in line:
        if len((current + char).encode("utf-8")) > limit:
            parts.append(current)
            current = " " + char  # пробел - признак продолжения строки
        else:
            current += char
    parts.append(current)
    return "\r\n".join(parts)


ICS_KEEP_DAYS = 30


def cleanup_old_ics() -> None:
    """Убирает старые файлы встреч.

    В них названия встреч и заметки - те же личные данные, что и в переписке.
    Копиться на диске годами им незачем: свою работу они делают в день отправки.
    """
    cutoff = datetime.now().timestamp() - ICS_KEEP_DAYS * 86400
    for path in STATE.glob("*.ics"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def build_ics(
    title: str,
    start: datetime,
    minutes: int = 60,
    note: str = "",
    where: str = "",
) -> Path:
    """Собирает файл встречи. Время пишется в UTC - календарь покажет местное."""
    if start.tzinfo is None:
        start = start.astimezone()

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//chatkeeper//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uuid.uuid4()}@chatkeeper",
        f"DTSTAMP:{_stamp(datetime.now(timezone.utc))}",
        f"DTSTART:{_stamp(start)}",
        f"DTEND:{_stamp(start + timedelta(minutes=minutes))}",
        f"SUMMARY:{_escape(title)}",
    ]
    if note:
        lines.append(f"DESCRIPTION:{_escape(note)}")
    if where:
        lines.append(f"LOCATION:{_escape(where)}")
    lines += [
        "BEGIN:VALARM",
        "TRIGGER:-PT15M",
        "ACTION:DISPLAY",
        "DESCRIPTION:Напоминание",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]

    STATE.mkdir(parents=True, exist_ok=True)
    cleanup_old_ics()
    safe = "".join(c for c in title if c.isalnum() or c in " -_")[:40].strip()
    path = STATE / f"{start:%Y-%m-%d_%H%M}_{safe or 'встреча'}.ics"

    # Формат календаря требует переводов строки ровно CRLF.
    # newline="" здесь обязателен: без него Windows превращает каждый \n
    # в \r\n, готовый \r\n становится \r\r\n, и файл получается битым.
    # На macOS этого не видно - баг проявляется только у части пользователей.
    with path.open("w", encoding="utf-8", newline="") as file:
        file.write("\r\n".join(fold(line) for line in lines) + "\r\n")
    return path


# --- команды ------------------------------------------------------------


def cmd_test() -> None:
    send_message("Chatkeeper: проверка связи. Если ты это видишь, доставка работает.")
    print("OK: сообщение отправлено")


def cmd_message(argv: list[str]) -> None:
    """chatkeeper send message "текст" - отправить произвольное сообщение владельцу."""
    if not argv:
        die('Нужно: send message "текст сообщения"')
    send_message(argv[0])
    print("OK: сообщение отправлено")


def cmd_ics(argv: list[str]) -> None:
    """chatkeeper send ics "название" 2026-08-16T15:00 [минуты] [заметка]"""
    if len(argv) < 2:
        die('Нужно: send ics "название встречи" 2026-08-16T15:00 [минуты] [заметка]')
    title, when = argv[0], argv[1]
    minutes = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 60
    note = argv[3] if len(argv) > 3 else ""

    try:
        start = datetime.fromisoformat(when)
    except ValueError:
        die(
            f"«{when}» не похоже на дату и время. "
            "Нужно так: 2026-08-16T15:00"
        )
        return
    if start.tzinfo is None:
        start = start.astimezone()

    path = build_ics(title, start, minutes, note)
    send_document(path, caption=f"Встреча: {start:%d.%m в %H:%M} — {title}")
    print(f"OK: встреча отправлена ({path.name})")


def cmd_ics_demo() -> None:
    start = (datetime.now().astimezone() + timedelta(days=1)).replace(
        hour=15, minute=0, second=0, microsecond=0
    )
    path = build_ics(
        title="Созвон по проекту",
        start=start,
        minutes=30,
        note="Пробная встреча от Chatkeeper. Можно удалить.",
    )
    send_document(path, caption=f"Пробная встреча: завтра в {start:%H:%M}")
    print(f"OK: файл отправлен ({path.name})")


def main() -> None:
    utf8_console()
    if len(sys.argv) < 2:
        print("Использование: send [test | ics-demo | message ТЕКСТ | ics НАЗВАНИЕ ВРЕМЯ]")
        sys.exit(2)

    command, argv = sys.argv[1], sys.argv[2:]
    if command == "test":
        cmd_test()
    elif command == "ics-demo":
        cmd_ics_demo()
    elif command == "message":
        cmd_message(argv)
    elif command == "ics":
        cmd_ics(argv)
    else:
        print("Использование: send [test | ics-demo | message ТЕКСТ | ics НАЗВАНИЕ ВРЕМЯ]")
        sys.exit(2)


if __name__ == "__main__":
    main()
