"""Chatkeeper: источник сообщений через собственного бота, без входа под аккаунтом.

Зачем этот файл существует. Прежний сборщик (collect.py) входит в телеграм под живым
аккаунтом и потому требует api_id с my.telegram.org. Заказчик получить его не может:
сайт из России не открывается, а через VPN форма создания приложения падает. Здесь
тот же результат достигается через штатный механизм Telegram: бот, подключённый
владельцем в настройках («Telegram для бизнеса» -> «Чат-боты»), получает сообщения
из личных чатов владельца. Ни ключей, ни подписки, ни VPN.

Что этот источник НЕ делает и почему:

  - не собирает групповые чаты. Бот в группе виден всем участникам, а заказчик
    поставил условие: собеседники не должны знать о системе. Группы закрываются
    отдельной ручной выгрузкой, это другая фаза плана;
  - не читает историю. В Bot API нет метода выдачи прошлых сообщений вообще;
  - никогда ничего не пишет и не помечает прочитанным. Права can_reply
    и can_read_messages выдавать нельзя: первое оставляет на сообщении видимую
    отметку, второе меняет галочки прочтения у собеседника. Содержимое сообщений
    приходит и без этих прав - отдельного права «получать текст» в телеграме нет.

Главное правило этого файла - порядок из трёх шагов.

  1. забрали обновления и записали на диск, с принудительным сбросом на носитель;
  2. только теперь сдвинули смещение (offset), подтвердив телеграму получение;
  3. буфер очищается лишь после того, как разбор реально состоялся (команда done).

Телеграм отдаёт обновление ровно один раз: подтверждённое смещением он удаляет
и больше не выдаст никогда. Перепутанный порядок здесь означает не сбой, а тихую
потерю куска переписки, которую невозможно ни заметить, ни восстановить.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

API = "https://api.telegram.org/bot{token}/{method}"

BUFFER = STATE / "bot_updates.jsonl"   # сырые обновления, живут до успешного разбора
BOT_STATE = STATE / "bot_state.json"   # смещение и сведения о подключении
INBOX = STATE / "inbox.md"             # то, что читает разбор
RUNTIME = STATE / "runtime.json"       # общий с collect.py: стоп-список, исключения

INBOX_MARKER = "<!-- CHATKEEPER-INBOX-V1 -->"  # по нему setup forget ищет следы переписки

# Замок общий с collect.py намеренно: два источника одним ботом одновременно ходить
# не должны, телеграм на два параллельных getUpdates отвечает ошибкой 409.
LOCK = STATE / "running.lock"
LOCK_STALE_SECONDS = 3600

MAX_TEXT_CHARS = 2000    # длинные простыни режем, договорённости живут в начале
MAX_VOICES = 20          # расшифровка небыстрая, за один разбор больше не берём
MAX_VOICE_BYTES = 20 * 1024 * 1024  # предел скачивания для бота, задан телеграмом
BATCH = 100              # больше телеграм за один ответ и не отдаёт
MAX_ROUNDS = 200         # предохранитель: 20 тысяч сообщений это заведомо аномалия
GAP_HOURS = 24           # столько телеграм хранит неполученные обновления

# Нас интересуют только личные чаты владельца. Групповые обновления не запрашиваем:
# что не запрошено, то и не приходит, и в буфер случайно не попадёт.
ALLOWED = ["business_connection", "business_message", "edited_business_message"]


# --- сеть ---------------------------------------------------------------


NO_CONNECTION = (
    "Не получилось связаться с телеграмом. Похоже, пропал интернет "
    "или отключился VPN. Включи и попробуй снова."
)


def api(token: str, method: str, payload: dict, strict: bool = True) -> dict:
    """POST в Bot API. Токен не попадает ни в вывод, ни в текст ошибки.

    Своя реализация, а не импорт из send.py: источник сообщений не должен зависеть
    от скрипта отправки, они запускаются в разное время и по разным причинам.

    `strict=False` возвращает пустой ответ вместо остановки. Это нужно там, где сбой
    не должен стоить всего разбора: например, скачивание одного голосового. Поймать
    остановку снаружи через `except Exception` нельзя - `die` поднимает `SystemExit`,
    а тот наследуется от `BaseException`. Такой перехват выглядел бы надёжным
    и молча не работал, поэтому решение принимается здесь, а не на вызывающей стороне.
    """
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        API.format(token=token, method=method),
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if not strict:
            return {}
        body = error.read().decode("utf-8", errors="replace")
        if error.code == 401:
            die(
                "Телеграм не принял ключ бота. Скорее всего он был отозван "
                "в @BotFather. Понадобится установка заново."
            )
        if error.code == 409:
            die(
                "Этого бота прямо сейчас слушает другая программа. "
                "Закрой её и попробуй снова."
            )
        die(f"Телеграм ответил ошибкой {error.code}. {body[:200]}")
    except (urllib.error.URLError, OSError):
        if not strict:
            return {}
        die(NO_CONNECTION)
        return {}
    except json.JSONDecodeError:
        if not strict:
            return {}
        die("Телеграм ответил чем-то нечитаемым. Похоже на сбой сети или подмену ответа.")

    if not answer.get("ok"):
        if not strict:
            return {}
        die(f"Телеграм отклонил запрос: {answer.get('description', 'без объяснения')}")
    return answer


# --- состояние ----------------------------------------------------------


def load_state() -> dict:
    return read_json(BOT_STATE, default={}) or {}


def save_state(state: dict) -> None:
    write_json(BOT_STATE, state)


def append_updates(updates: list[dict]) -> None:
    """Дописывает обновления в буфер и заставляет систему сбросить их на диск.

    Без принудительного сброса запись может ещё несколько секунд жить в кэше:
    выключение питания в этот момент означает, что телеграм получение уже
    подтвердил, а на диске ничего нет.
    """
    STATE.mkdir(parents=True, exist_ok=True)
    with BUFFER.open("a", encoding="utf-8") as handle:
        for update in updates:
            handle.write(json.dumps(update, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_buffer() -> list[dict]:
    """Читает буфер. Битая строка пропускается, а не роняет весь разбор."""
    if not BUFFER.exists():
        return []
    updates = []
    for line in BUFFER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            updates.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return updates


# --- замок --------------------------------------------------------------


def acquire_lock() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        age = time.time() - LOCK.stat().st_mtime
        if age < LOCK_STALE_SECONDS:
            die(
                "Разбор уже идёт прямо сейчас - подожди, пока он закончится. "
                "Скорее всего это утренняя задача по расписанию."
            )
        LOCK.unlink(missing_ok=True)
    LOCK.write_text(str(os.getpid()), encoding="utf-8")


def release_lock() -> None:
    LOCK.unlink(missing_ok=True)


# --- разбор одного сообщения -------------------------------------------


def owner_id(state: dict, env: dict[str, str]) -> int | None:
    """Кто здесь «я». Сведения из подключения надёжнее, чем настройка в файле."""
    connection = state.get("connection") or {}
    if connection.get("user_id"):
        return int(connection["user_id"])
    raw = env.get("TELEGRAM_OWNER_ID", "").strip()
    return int(raw) if raw.isdigit() else None


def chat_title(chat: dict) -> str:
    name = " ".join(
        part for part in (chat.get("first_name"), chat.get("last_name")) if part
    ).strip()
    if name:
        return name
    if chat.get("title"):  # групповые чаты приходят из выгрузки, у них title
        return str(chat["title"])
    if chat.get("username"):
        return "@" + chat["username"]
    return str(chat.get("id", "неизвестно кто"))


def chat_kind(chat: dict) -> str:
    """Подпись раздела. От бота приходят только личные, группы - из выгрузки."""
    return "личный" if str(chat.get("type", "private")) == "private" else "группа"


def sender_name(message: dict, me: int | None) -> str:
    sender = message.get("from") or {}
    if me is not None and sender.get("id") == me:
        return "я"
    name = sender.get("first_name") or sender.get("title")
    return name or "неизвестно кто"


def describe_media(message: dict) -> str | None:
    """Что за вложение. Файлы не скачиваются: расшифровка голосовых - отдельная фаза."""
    if "voice" in message or "video_note" in message:
        return "голосовое"
    if "photo" in message:
        return "фото"
    if "video" in message:
        return "видео"
    if "sticker" in message:
        return "стикер"
    if "audio" in message:
        return "аудио"
    if "document" in message:
        name = (message.get("document") or {}).get("file_name")
        return f"файл {name}" if name else "файл"
    return None


def transcribe_voice(token: str, message: dict, python: Path) -> str | None:
    """Скачивает голосовое, расшифровывает и сразу удаляет файл.

    Скачивание идёт по одноразовой ссылке от телеграма. Файл живёт на диске
    ровно столько, сколько нужно расшифровке: это чужой голос, и хранить его
    у себя мы не имеем ни права, ни причины.
    """
    audio = message.get("voice") or message.get("video_note") or {}
    if (audio.get("duration") or 0) > voice.MAX_SECONDS:
        return None
    if (audio.get("file_size") or 0) > MAX_VOICE_BYTES:
        return None
    file_id = audio.get("file_id")
    if not file_id:
        return None

    tmp = STATE / f"voice_{message.get('message_id', 0)}.ogg"
    try:
        # strict=False обязателен: битый file_id даёт 400, пачка голосовых - 429,
        # и в строгом режиме любая из этих ошибок остановила бы весь разбор.
        # Хуже того, буфер очищается только после успеха, поэтому то же сообщение
        # роняло бы и каждый следующий запуск - сводка не пришла бы уже никогда.
        answer = api(token, "getFile", {"file_id": file_id}, strict=False)
        remote = (answer.get("result") or {}).get("file_path")
        if not remote:
            return None
        url = f"https://api.telegram.org/file/bot{token}/{remote}"
        with urllib.request.urlopen(url, timeout=120) as response:
            tmp.write_bytes(response.read())
        return voice.transcribe(tmp, python=python)
    except Exception:
        # Одно нерасшифрованное голосовое не повод ронять весь разбор:
        # сообщение останется в сводке с пометкой «голосовое».
        return None
    finally:
        tmp.unlink(missing_ok=True)


def in_stop_list(chat_id: int, stop_list: set[int]) -> bool:
    """Та же проверка, что и в collect_account.py: id ходит с префиксом и без него."""
    return chat_id in stop_list or (abs(chat_id) % 10**10) in stop_list


def forbidden_chats() -> set[int]:
    """Чаты, которые нельзя даже записывать на диск: стоп-список плюс убранные вручную."""
    runtime = read_json(RUNTIME, default={}) or {}
    ids = set()
    for key in ("stop_list", "excluded"):
        ids |= {int(x) for x in runtime.get(key, [])}
    return ids


def is_forbidden(update: dict, forbidden: set[int]) -> bool:
    """Обновление относится к запретному чату. Служебные события не трогаем."""
    if not forbidden:
        return False
    message = update.get("business_message") or update.get("edited_business_message")
    if not message:
        return False
    chat_id = int((message.get("chat") or {}).get("id", 0))
    return bool(chat_id) and in_stop_list(chat_id, forbidden)


def sweep_orphan_voices() -> None:
    """Убирает голосовые, оставшиеся от оборванных разборов.

    Обычно файл удаляется сразу после расшифровки, но убитый процесс до этого
    места не доходит. Тогда чужой голос лежит на диске сколько угодно долго -
    ровно то, чего продукт обещает не делать.
    """
    if not STATE.exists():
        return
    deadline = time.time() - LOCK_STALE_SECONDS
    for leftover in STATE.glob("voice_*.ogg"):
        try:
            if leftover.stat().st_mtime < deadline:
                leftover.unlink(missing_ok=True)
        except OSError:
            continue


def render_row(message: dict, me: int | None, spoken: str | None = None) -> str | None:
    """Одна строка файла разбора. None означает «показывать нечего»."""
    text = (message.get("text") or message.get("caption") or "").strip()
    media = describe_media(message)
    if spoken:
        # Расшифровка заменяет пустой текст голосового, но пометка остаётся:
        # человек должен видеть, что это распознанная речь, а не набранное вручную.
        text = spoken
        media = "голосовое, расшифровка"
    if not text and not media:
        return None  # служебное: сменился аватар, кто-то вошёл
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + " […обрезано]"

    stamp = datetime.fromtimestamp(message.get("date", 0), tz=timezone.utc)
    mark = f"[{media}] " if media else ""
    return f"- [{stamp.astimezone():%d.%m %H:%M}] {sender_name(message, me)}: {mark}{text}"


# --- команды ------------------------------------------------------------


def cmd_fetch() -> None:
    acquire_lock()
    try:
        env = load_env()
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            die(
                "В настройках нет ключа бота (TELEGRAM_BOT_TOKEN). "
                "Похоже, установка не завершена."
            )

        state = load_state()
        offset = int(state.get("offset", 0))
        started_at = datetime.now(timezone.utc)
        received = 0
        forbidden = forbidden_chats()
        sweep_orphan_voices()

        for _ in range(MAX_ROUNDS):
            answer = api(
                token,
                "getUpdates",
                {
                    "offset": offset,
                    "limit": BATCH,
                    "timeout": 0,
                    "allowed_updates": ALLOWED,
                },
            )
            updates = answer.get("result") or []
            if not updates:
                break

            # Порядок здесь не косметика: сначала на диск, потом подтверждение.
            # Но запретные чаты отсеиваем ДО записи: человеку обещано, что такой
            # чат не читается никогда, и обещание должно значить «байты не легли
            # на диск», а не «мы их сохранили, но не показали». Смещение при этом
            # двигается всё равно - иначе телеграм отдавал бы их снова и снова.
            append_updates([u for u in updates if not is_forbidden(u, forbidden)])
            received += len(updates)
            offset = max(int(u["update_id"]) for u in updates) + 1
            state["offset"] = offset
            save_state(state)

        # Сведения о подключении обновляем отдельно: они нужны, чтобы понимать,
        # кто в переписке «я», и чтобы заметить, если подключение сняли.
        for update in read_buffer():
            connection = update.get("business_connection")
            if connection:
                state["connection"] = {
                    "id": connection.get("id"),
                    "user_id": (connection.get("user") or {}).get("id"),
                    "enabled": connection.get("is_enabled", True),
                }
        # Отметку прошлого раза забираем ДО того, как записать нынешнюю: иначе
        # разрыв сравнивался бы сам с собой и не находился никогда.
        previous = state.get("last_done") or state.get("last_fetch")
        # Событие подключения приходит один раз и живёт сутки. Если бота подключили
        # давно или продукт переустановили, событие не повторится - и система решила
        # бы, что подключения нет, при работающем подключении. Поэтому опознаём его
        # и по самим сообщениям: каждое несёт номер соединения.
        if not state.get("connection"):
            for update in read_buffer():
                message = update.get("business_message") or {}
                link = message.get("business_connection_id")
                if not link:
                    continue
                answer = api(
                    token,
                    "getBusinessConnection",
                    {"business_connection_id": link},
                    strict=False,
                )
                found = answer.get("result") or {}
                if found:
                    state["connection"] = {
                        "id": found.get("id"),
                        "user_id": (found.get("user") or {}).get("id"),
                        "enabled": found.get("is_enabled", True),
                    }
                break

        state["last_fetch"] = started_at.isoformat()
        save_state(state)

        write_inbox(state, env, started_at, previous)
    finally:
        release_lock()


def write_inbox(
    state: dict, env: dict[str, str], started_at: datetime, previous: str | None = None
) -> None:
    """Собирает файл разбора из всего буфера, а не только из свежей порции.

    Именно поэтому оборванный вчера разбор ничего не теряет: буфер очищается
    только командой done, то есть после того, как сводка действительно собрана.
    """
    me = owner_id(state, env)
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    runtime = read_json(RUNTIME, default={}) or {}
    stop_list = {int(x) for x in runtime.get("stop_list", [])}
    excluded = {int(x) for x in runtime.get("excluded", [])}

    chats: dict[int, dict] = {}
    total = 0
    skipped_stop = 0
    voices = 0
    voices_done = 0
    voices_tried = 0
    engine = voice.find_engine() if token else None

    for update in read_buffer():
        message = update.get("business_message") or update.get("edited_business_message")
        if not message:
            continue
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id", 0))
        if not chat_id:
            continue
        if in_stop_list(chat_id, stop_list) or chat_id in excluded:
            skipped_stop += 1
            continue

        spoken = None
        if describe_media(message) == "голосовое":
            voices += 1
            if engine is not None and voices_tried < MAX_VOICES:
                # Считаем попытки, а не удачи: если движок найден, но ломается
                # на каждом файле, предохранитель по удачам не сработал бы никогда.
                voices_tried += 1
                spoken = transcribe_voice(token, message, engine[0])
                if spoken:
                    voices_done += 1

        row = render_row(message, me, spoken)
        if row is None:
            continue
        bucket = chats.setdefault(
            chat_id,
            {"title": chat_title(chat), "kind": chat_kind(chat), "rows": {}},
        )
        # Ключ - номер сообщения, а не порядок прихода. Правка приходит отдельным
        # обновлением с тем же номером: без такого ключа разбор увидел бы два
        # сообщения вместо одного исправленного и завёл бы два обещания.
        bucket["rows"][int(message.get("message_id", 0))] = (
            message.get("date", 0),
            row,
        )

    sections = []
    for bucket in chats.values():
        rows = sorted(bucket["rows"].values(), key=lambda item: item[0])
        total += len(rows)
        sections.append(
            f"## {bucket['title']} ({bucket.get('kind', 'личный')})\n"
            + "\n".join(row for _, row in rows)
        )

    # Реестр чатов. Списка диалогов у бота нет и быть не может, поэтому чат
    # становится известен только тогда, когда из него пришло первое сообщение.
    # Реестр нужен, чтобы человек мог сказать «этот чат не бери»: исключение
    # пишется по id, а id взять неоткуда, если чат нигде не перечислен.
    known = {int(c["id"]): dict(c) for c in runtime.get("chats", []) if c.get("id")}
    for chat_id, bucket in chats.items():
        entry = known.setdefault(chat_id, {"id": chat_id, "type": bucket.get("kind", "личный")})
        entry["name"] = bucket["title"]
        entry["type"] = bucket.get("kind", "личный")
        entry["last_seen"] = started_at.isoformat()
    runtime["chats"] = sorted(known.values(), key=lambda c: c.get("name") or "")
    write_json(RUNTIME, runtime)

    STATE.mkdir(parents=True, exist_ok=True)
    header = f"{INBOX_MARKER}\n# Личные чаты, собрано {started_at.astimezone():%d.%m.%Y %H:%M}\n"
    gap = gap_warning(previous, started_at)
    if gap:
        header += gap + "\n"
    body = "\n\n".join(sections) if sections else "Новых сообщений нет."
    INBOX.write_text(header + "\n" + body + "\n", encoding="utf-8")

    # В консоль только счётчики: содержимое переписки остаётся в файле и не должно
    # оседать в транскрипте сессии.
    connection = state.get("connection") or {}
    if not connection:
        print(
            "Подключение бота к личным чатам пока не видно. "
            "Проверь: настройки телеграма на компьютере -> Telegram для бизнеса -> Чат-боты."
        )
    elif connection.get("enabled") is False:
        print("ВНИМАНИЕ: подключение бота отключено в настройках телеграма.")
    print(f"Чатов: {len(chats)}")
    if skipped_stop:
        print(f"Пропущено по стоп-списку: {skipped_stop}")
    print(f"Сообщений собрано: {total}")
    if voices:
        if engine is None:
            print(f"Из них голосовых: {voices} - расшифровать нечем, помечены как есть")
        else:
            print(f"Из них голосовых: {voices}, расшифровано {voices_done}")
    if gap:
        print(gap)
    print(f"Файл для разбора: {INBOX}")
    print("После разбора обязательно выполни: chatkeeper collect done")


def gap_warning(previous: str | None, now: datetime) -> str | None:
    """Разрыв дольше суток означает безвозвратную потерю, и молчать об этом нельзя.

    Телеграм хранит неполученные обновления не дольше 24 часов. Если компьютер
    был выключен дольше, часть переписки не придёт никогда - человек должен знать,
    что тишина в сводке может быть не тишиной в чатах.
    """
    if not previous:
        return None
    try:
        seen_at = datetime.fromisoformat(previous)
    except ValueError:
        return None
    if seen_at.tzinfo is None:
        seen_at = seen_at.replace(tzinfo=timezone.utc)
    gap = now - seen_at
    if gap <= timedelta(hours=GAP_HOURS):
        return None
    return (
        f"> Перерыв {int(gap.total_seconds() // 3600)} часов. Телеграм хранит "
        f"неполученные сообщения {GAP_HOURS} часа, поэтому часть переписки "
        "за это время не пришла и не придёт."
    )


def cmd_done() -> None:
    """Разбор состоялся - буфер можно чистить. До этого момента нельзя."""
    # Защита от вызова невпопад: она была у прежнего источника, и терять её нельзя.
    # Молча «успешное» закрытие того, чего не было, скрывает сбой в порядке команд,
    # а найти такое потом крайне трудно.
    if not BUFFER.exists() and not INBOX.exists():
        print("Нечего закрывать: забора сообщений не было. Сначала collect fetch.")
        return
    state = load_state()
    state["last_done"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    BUFFER.unlink(missing_ok=True)
    INBOX.unlink(missing_ok=True)
    print("Готово: разобранные сообщения убраны, файл разбора удалён.")


def cmd_status() -> None:
    state = load_state()
    connection = state.get("connection") or {}
    if not connection:
        print("Подключение к личным чатам: не найдено.")
    else:
        print(
            "Подключение к личным чатам: "
            + ("работает" if connection.get("enabled", True) else "ОТКЛЮЧЕНО")
        )
    print(f"Необработанных сообщений в буфере: {len(read_buffer())}")
    print(f"Последний забор: {state.get('last_fetch', 'ещё не было')}")


def main() -> None:
    utf8_console()
    command = sys.argv[1] if len(sys.argv) > 1 else "fetch"
    if command == "fetch":
        cmd_fetch()
    elif command == "done":
        cmd_done()
    elif command == "status":
        cmd_status()
    else:
        die(f"Неизвестная команда: {command}. Есть fetch, done, status.")


if __name__ == "__main__":
    main()
