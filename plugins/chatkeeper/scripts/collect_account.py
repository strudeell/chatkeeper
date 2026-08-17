"""Chatkeeper: мост к Telegram.

Команды:
    login  - вход по QR-коду, один раз при установке
    chats  - отбор рабочих чатов по метаданным, без чтения сообщений
    fetch  - забрать новые сообщения из отобранных чатов в state/inbox.md

Принципы, которые здесь держатся кодом, а не обещаниями:
  * содержимое переписки в этом файле не читается вообще - только метаданные диалогов;
  * чаты из стоп-списка отсекаются до любого обращения к ним;
  * ничего никуда не отправляется.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.messages import GetDialogFiltersRequest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import voice  # noqa: E402
from common import (  # noqa: E402
    STATE,
    die,
    load_env,
    read_json,
    utf8_console,
    write_json,
)

# --- пути ---------------------------------------------------------------

SESSION = STATE / "telegram.session"
RUNTIME = STATE / "runtime.json"
QR_IMAGE = STATE / "qr.png"
INBOX = STATE / "inbox.md"

# --- правила отбора (раздел 9 ресёрча) ----------------------------------

DEAD_AFTER_DAYS = 30      # чат молчит дольше - считаем мёртвым
BIG_GROUP_MEMBERS = 50    # группа больше - это комьюнити, а не работа

# --- границы забора -----------------------------------------------------

INBOX_MARKER = "<!-- CHATKEEPER-INBOX-V1 -->"  # см. пояснение в cmd_fetch

FIRST_RUN_DAYS = 7        # при установке смотрим неделю назад
MAX_PER_CHAT = 200        # больше из одного чата за раз не берём
MAX_TOTAL = 2000          # общий предел, чтобы разбор не стоил как самолёт


def load_runtime() -> dict:
    return read_json(RUNTIME, {"stop_list": [], "chats": [], "last_run": None})


def save_runtime(data: dict) -> None:
    write_json(RUNTIME, data)


def make_client(env: dict[str, str]) -> TelegramClient:
    api_id = env.get("TELEGRAM_API_ID", "")
    api_hash = env.get("TELEGRAM_API_HASH", "")
    if not api_id or not api_hash:
        die(
            "В .env не заполнены TELEGRAM_API_ID и TELEGRAM_API_HASH. "
            "Взять на https://my.telegram.org -> API development tools."
        )
    if not api_id.isdigit():
        die("TELEGRAM_API_ID должен быть числом. Похоже, значения перепутаны местами.")
    STATE.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(SESSION), int(api_id), api_hash)


# --- вход по QR ---------------------------------------------------------


def open_in_viewer(path: Path) -> bool:
    """Открывает картинку системным просмотрщиком. False - не получилось."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=True, capture_output=True)
        else:
            subprocess.run(["xdg-open", str(path)], check=True, capture_output=True)
        return True
    except Exception:
        return False


def show_qr(url: str) -> None:
    """Показывает код входа человеку на экране.

    В консоль код НЕ печатаем без крайней нужды: это ссылка вида tg://login,
    дающая полный доступ к аккаунту, а всё напечатанное оседает в истории
    переписки с Claude. Картинка на экране туда не попадает.
    """
    import qrcode

    code = qrcode.QRCode(border=2)
    code.add_data(url)
    code.make(fit=True)
    code.make_image().save(QR_IMAGE)

    if open_in_viewer(QR_IMAGE):
        print(f"QR_READY {QR_IMAGE} (открыт на экране)")
        return

    print(f"QR_READY {QR_IMAGE}")
    print("Просмотрщик не открылся, показываю код прямо здесь:")
    code.print_ascii(invert=True)


async def cmd_login() -> None:
    """Показывает QR-код и ждёт, пока его отсканируют телефоном."""
    client = make_client(load_env())
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"OK: вход уже выполнен, аккаунт {me.first_name}")
        await client.disconnect()
        return

    try:
        qr = await client.qr_login()
        for attempt in range(3):
            show_qr(qr.url)
            print("Телефон: Настройки - Устройства - Подключить устройство")
            try:
                await qr.wait(timeout=120)
                break
            except asyncio.TimeoutError:
                if attempt == 2:
                    die("Код никто не отсканировал. Запусти вход заново.")
                print("Код устарел, показываю новый...")
                await qr.recreate()
            except SessionPasswordNeededError:
                die(
                    "На аккаунте включён облачный пароль (двухфакторная защита). "
                    "Вход по коду его не заменяет: временно отключи пароль "
                    "в настройках телеграма, войди и включи обратно."
                )

        me = await client.get_me()
        print(f"OK: вход выполнен, аккаунт {me.first_name}")

        runtime = load_runtime()
        runtime["owner_id"] = me.id
        save_runtime(runtime)
    finally:
        # Картинку с кодом входа убираем в любом случае, в том числе при ошибке:
        # это ключ от аккаунта, ему нечего валяться на диске.
        QR_IMAGE.unlink(missing_ok=True)
        await client.disconnect()


# --- отбор чатов --------------------------------------------------------


def is_muted(dialog) -> bool:
    """Уведомления выключены - человек уже сказал, что чат неважный."""
    settings = getattr(dialog.dialog, "notify_settings", None)
    mute_until = getattr(settings, "mute_until", None)
    if mute_until is None:
        return False
    if isinstance(mute_until, datetime):
        return mute_until > datetime.now(timezone.utc)
    return bool(mute_until)


def member_count(entity) -> int | None:
    return getattr(entity, "participants_count", None)


async def load_folders(client) -> dict[int, str]:
    """Папки Telegram: готовая разметка, которую человек сделал руками до нас."""
    mapping: dict[int, str] = {}
    try:
        result = await client(GetDialogFiltersRequest())
    except Exception:
        return mapping

    filters = getattr(result, "filters", result)
    for folder in filters:
        title = getattr(folder, "title", None)
        if title is None:
            continue  # папка "Все чаты", разметки не несёт
        # В свежих версиях заголовок - объект с полем text
        name = getattr(title, "text", title)
        for peer in list(getattr(folder, "include_peers", [])) + list(
            getattr(folder, "pinned_peers", [])
        ):
            peer_id = (
                getattr(peer, "user_id", None)
                or getattr(peer, "channel_id", None)
                or getattr(peer, "chat_id", None)
            )
            if peer_id:
                mapping[int(peer_id)] = str(name)
    return mapping


def in_stop_list(chat_id: int, stop_list: set[int]) -> bool:
    """Одна проверка стоп-списка на весь проект.

    Id чата ходит в двух видах: полный (-1001234567890) и «сырой», без префикса
    (1234567890) - именно так он приходит из папок Telegram. Проверять надо оба,
    и одинаково везде: если отбор чатов и забор сообщений сверяют по-разному,
    запретный чат отсеется при отборе, но будет прочитан при заборе.
    """
    return chat_id in stop_list or (abs(chat_id) % 10**10) in stop_list


def classify(dialog, folders: dict[int, str], stop_list: set[int]) -> dict:
    """Решение по одному диалогу. Сообщения не читаются."""
    entity = dialog.entity
    chat_id = dialog.id
    raw_id = abs(chat_id) % 10**10  # id в папках приходит без префикса -100

    if in_stop_list(chat_id, stop_list):
        return {"take": False, "reason": "стоп-список"}
    if getattr(entity, "bot", False):
        return {"take": False, "reason": "бот"}
    if getattr(entity, "broadcast", False):
        return {"take": False, "reason": "канал"}
    if dialog.archived:
        return {"take": False, "reason": "архив"}
    if is_muted(dialog):
        return {"take": False, "reason": "уведомления выключены"}

    last = dialog.date
    if last is not None:
        age = datetime.now(timezone.utc) - last
        if age > timedelta(days=DEAD_AFTER_DAYS):
            return {"take": False, "reason": f"молчит {age.days} дней"}

    members = member_count(entity)
    if members is not None and members > BIG_GROUP_MEMBERS:
        return {"take": False, "reason": f"группа на {members} человек"}

    folder = folders.get(raw_id) or folders.get(abs(chat_id))
    priority = "high" if (dialog.pinned or folder) else "normal"
    return {"take": True, "reason": "подходит", "folder": folder, "priority": priority}


async def cmd_chats() -> None:
    client = make_client(load_env())
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        die("Вход не выполнен. Сначала команда login.")

    runtime = load_runtime()
    stop_list = {int(x) for x in runtime.get("stop_list", [])}
    excluded = {int(x) for x in runtime.get("excluded", [])}
    folders = await load_folders(client)

    taken, dropped = [], {}
    async for dialog in client.iter_dialogs():
        if dialog.id in excluded:
            dropped["убран вручную раньше"] = (
                dropped.get("убран вручную раньше", 0) + 1
            )
            continue
        verdict = classify(dialog, folders, stop_list)
        if verdict["take"]:
            taken.append(
                {
                    "id": dialog.id,
                    "name": dialog.name,
                    "type": "личный" if dialog.is_user else "группа",
                    "folder": verdict.get("folder"),
                    "priority": verdict["priority"],
                    "last_message": dialog.date.isoformat() if dialog.date else None,
                }
            )
        else:
            dropped[verdict["reason"]] = dropped.get(verdict["reason"], 0) + 1

    taken.sort(key=lambda c: (c["priority"] != "high", c["name"] or ""))
    runtime["chats"] = taken
    runtime["chats_scanned_at"] = datetime.now(timezone.utc).isoformat()
    save_runtime(runtime)

    total = len(taken) + sum(dropped.values())
    print(f"Всего диалогов: {total}")
    print(f"Отобрано: {len(taken)}")
    print("Отсеяно:")
    for reason, count in sorted(dropped.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {reason}")
    print(f"\nСписок сохранён: {RUNTIME}")

    await client.disconnect()


# --- забор новых сообщений ----------------------------------------------


MAX_TEXT_CHARS = 2000  # длинные простыни режем, договорённости живут в начале
MAX_VOICES = 20        # расшифровка небыстрая, за один разбор больше не берём


async def transcribe_voice(message, python: Path) -> str | None:
    """Скачивает голосовое, расшифровывает и сразу удаляет файл."""
    duration = getattr(message.file, "duration", None) or 0
    if duration > voice.MAX_SECONDS:
        return None

    tmp = STATE / f"voice_{message.id}.ogg"
    try:
        await message.download_media(file=str(tmp))
        return voice.transcribe(tmp, python=python)
    except Exception:
        return None
    finally:
        tmp.unlink(missing_ok=True)


def describe_media(message) -> str | None:
    """Что за вложение, без скачивания."""
    if message.voice or message.video_note:
        return "голосовое"
    if message.photo:
        return "фото"
    if message.video:
        return "видео"
    if message.document:
        name = getattr(message.file, "name", None)
        return f"файл {name}" if name else "файл"
    return None


def sender_name(message) -> str:
    if message.out:
        return "я"
    sender = message.sender
    if sender is None:
        return "неизвестно кто"
    name = getattr(sender, "first_name", None) or getattr(sender, "title", None)
    return name or "неизвестно кто"


LOCK = STATE / "running.lock"
LOCK_STALE_SECONDS = 3600  # час: дольше живой разбор не идёт даже с голосовыми


def acquire_lock() -> None:
    """Не даёт двум разборам идти разом.

    Утренняя задача по расписанию и просьба «разбери сейчас» легко совпадают
    по времени - утро и есть время расписания. Два процесса открыли бы один
    файл сессии телеграма и перезаписали бы выгрузку друг друга.
    """
    STATE.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        age = time.time() - LOCK.stat().st_mtime
        if age < LOCK_STALE_SECONDS:
            die(
                "Разбор уже идёт прямо сейчас - подожди, пока он закончится. "
                "Скорее всего это утренняя задача по расписанию."
            )
        LOCK.unlink(missing_ok=True)  # прошлый разбор оборвался, замок протух
    LOCK.write_text(str(os.getpid()), encoding="utf-8")
    # Снятие при любом выходе, включая падение: иначе оборвавшийся разбор
    # заблокировал бы следующий на целый час.
    atexit.register(release_lock)


def release_lock() -> None:
    LOCK.unlink(missing_ok=True)


async def cmd_fetch() -> None:
    acquire_lock()
    client = make_client(load_env())
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        die("Вход не выполнен. Сначала команда login.")

    runtime = load_runtime()
    chats = runtime.get("chats", [])
    if not chats:
        await client.disconnect()
        die("Список чатов пуст. Сначала команда chats.")

    stop_list = {int(x) for x in runtime.get("stop_list", [])}
    last_run_raw = runtime.get("last_run")
    if last_run_raw:
        since = datetime.fromisoformat(last_run_raw)
    else:
        since = datetime.now(timezone.utc) - timedelta(days=FIRST_RUN_DAYS)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    started_at = datetime.now(timezone.utc)
    sections: list[str] = []
    total = 0
    voices = 0
    voices_done = 0
    voices_tried = 0
    skipped_stop = 0
    engine = voice.find_engine()

    # Отметка «прочитано до» ведётся по каждому чату отдельно. Общая отметка
    # на всех означала бы: упёрлись в предел на пятнадцатом чате из сорока -
    # и сутки переписки в оставшихся двадцати пяти потеряны навсегда.
    chat_marks: dict[str, str] = runtime.get("chat_marks", {})
    pending_marks: dict[str, str] = {}
    unfinished: list[str] = []

    for chat in chats:
        chat_id = int(chat["id"])
        if in_stop_list(chat_id, stop_list):
            skipped_stop += 1
            continue
        if total >= MAX_TOTAL:
            # До этого чата не дошли. Его отметку не трогаем - дочитается завтра.
            unfinished.append(chat.get("name") or str(chat_id))
            continue

        mark = chat_marks.get(str(chat_id))
        since_chat = datetime.fromisoformat(mark) if mark else since
        if since_chat.tzinfo is None:
            since_chat = since_chat.replace(tzinfo=timezone.utc)

        rows: list[str] = []
        seen = 0
        try:
            async for message in client.iter_messages(chat_id, limit=MAX_PER_CHAT):
                if message.date <= since_chat:
                    break
                seen += 1
                if message.action is not None:
                    continue  # служебное: кто-то вошёл, сменил аватар

                text = (message.text or "").strip()
                media = describe_media(message)
                if media == "голосовое":
                    voices += 1
                    if engine is not None and voices_tried < MAX_VOICES:
                        # Считаем попытки, а не удачи: если движок найден,
                        # но ломается на каждом файле, предохранитель по удачам
                        # не сработал бы никогда, и разбор шёл бы часами.
                        voices_tried += 1
                        spoken = await transcribe_voice(message, engine[0])
                        if spoken:
                            text = spoken
                            voices_done += 1
                            media = "голосовое, расшифровка"
                if not text and not media:
                    continue
                if len(text) > MAX_TEXT_CHARS:
                    text = text[:MAX_TEXT_CHARS] + " […обрезано]"

                mark = f"[{media}] " if media else ""
                stamp = message.date.astimezone().strftime("%d.%m %H:%M")
                rows.append(f"- [{stamp}] {sender_name(message)}: {mark}{text}")
        except Exception as error:  # чат мог исчезнуть или закрыться
            print(f"  пропущен чат «{chat.get('name')}»: {type(error).__name__}")
            continue

        if seen >= MAX_PER_CHAT:
            # Дочитали до предела, а более старые сообщения остались. Отметку
            # не двигаем: завтра этот кусок прочитается заново. Повтор безопасен -
            # правила разбора запрещают заводить дубли, - а потеря нет.
            unfinished.append(chat.get("name") or str(chat_id))
        else:
            pending_marks[str(chat_id)] = started_at.isoformat()

        if rows:
            rows.reverse()  # в файле - в порядке разговора
            total += len(rows)
            title = chat.get("name") or str(chat_id)
            folder = f", папка «{chat['folder']}»" if chat.get("folder") else ""
            sections.append(
                f"## {title} ({chat.get('type', 'чат')}{folder})\n" + "\n".join(rows)
            )

    STATE.mkdir(parents=True, exist_ok=True)
    # Маркер нужен, чтобы потом найти, в каких файлах истории Claude Code осела
    # переписка: содержимое выгрузки попадает в транскрипт сессии и остаётся там
    # после того, как сам файл удалён. Ищет по нему команда setup forget.
    header = (
        f"{INBOX_MARKER}\n"
        f"# Новое с {since.astimezone():%d.%m.%Y %H:%M} "
        f"по {started_at.astimezone():%d.%m.%Y %H:%M}\n"
    )
    INBOX.write_text(header + "\n" + "\n\n".join(sections) + "\n", encoding="utf-8")

    # Отметки здесь НЕ применяем. Если разбор упадёт после выгрузки, сдвинутые
    # отметки означали бы, что сутки переписки потеряны навсегда.
    # Их применит команда done - когда разбор действительно закончен.
    runtime["pending_marks"] = pending_marks
    runtime["pending_until"] = started_at.isoformat()
    save_runtime(runtime)

    # В консоль - только счётчики. Содержимое переписки остаётся в файле.
    print(f"Чатов просмотрено: {len(chats)}")
    if skipped_stop:
        print(f"Пропущено по стоп-списку: {skipped_stop}")
    print(f"Сообщений собрано: {total}")
    if voices:
        if engine is None:
            print(f"Из них голосовых: {voices} - расшифровать нечем, помечены как есть")
        else:
            print(f"Из них голосовых: {voices}, расшифровано {voices_done}")
    if unfinished:
        print(
            f"Не дочитано чатов: {len(unfinished)} "
            f"({', '.join(unfinished[:3])}{'...' if len(unfinished) > 3 else ''}). "
            "Ничего не потеряно - они дочитаются при следующем разборе."
        )
    print(f"Файл для разбора: {INBOX}")
    print("После разбора обязательно выполни: chatkeeper collect done")

    release_lock()
    await client.disconnect()


def cmd_done() -> None:
    """Разбор закончен: двигаем отметку и убираем выгрузку переписки.

    Отдельная команда, а не хвост fetch, ровно по одной причине: пока разбор
    не доведён до конца, сутки переписки должны оставаться непрочитанными.
    """
    runtime = load_runtime()
    pending = runtime.get("pending_until")
    if not pending:
        die(
            "Нечего закрывать: выгрузки не было. "
            "Сначала chatkeeper collect fetch, потом разбор, потом эта команда."
        )
        return

    marks = runtime.get("chat_marks", {})
    marks.update(runtime.get("pending_marks", {}))
    runtime["chat_marks"] = marks
    runtime["last_run"] = pending
    runtime.pop("pending_until", None)
    runtime.pop("pending_marks", None)
    save_runtime(runtime)

    INBOX.unlink(missing_ok=True)
    print(f"Разбор закрыт. Следующий запуск читает с {pending}")
    print("Выгрузка переписки удалена.")


# --- точка входа --------------------------------------------------------


def main() -> None:
    utf8_console()
    commands = {"login": cmd_login, "chats": cmd_chats, "fetch": cmd_fetch}
    if len(sys.argv) < 2:
        print("Использование: collect [login | chats | fetch | done]")
        sys.exit(2)

    command = sys.argv[1]
    if command == "done":  # единственная команда без обращения к телеграму
        cmd_done()
        return
    if command not in commands:
        print("Использование: collect [login | chats | fetch | done]")
        sys.exit(2)
    asyncio.run(commands[command]())


if __name__ == "__main__":
    main()
