"""Chatkeeper: механика установки.

Всё, что при установке можно сделать без участия человека, делается здесь,
чтобы Claude не правил файлы вручную и не ошибался в них.

Команды:
    check              - что уже готово, а чего не хватает
    env-set КЛЮЧ ЗНАЧ  - записать значение в .env (значение в вывод не попадает)
    bot-owner          - поймать id владельца после его /start боту
    chats-keep ID,ID   - оставить в разборе только эти чаты
    stop-add ID,ID     - добавить чаты в стоп-список, который не читается никогда
    schedule ЧЧ:ММ     - записать время утренней сводки
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    DATA,
    ENV_FILE,
    ROOT,
    STATE,
    VENV,
    die,
    load_env,
    python_path,
    read_json,
    utf8_console,
    write_json,
)

RUNTIME = STATE / "runtime.json"
EXAMPLE = ROOT / ".env.example"

# Библиотеки, ставятся в папку данных, а не в папку плагина: обновление её не сотрёт.
# Работа через бота обходится стандартной библиотекой python, поэтому здесь пусто.
# telethon, qrcode и pillow нужны только источнику account (вход под аккаунтом)
# и ставятся отдельно, если человек этот источник выберет: тащить их всем
# ради пути, которым почти никто не пользуется, - лишние минуты установки.
PACKAGES: list[str] = []
PACKAGES_ACCOUNT = ["telethon", "qrcode", "pillow"]

# Ключи, без которых продукт не работает. api_id и api_hash сюда не входят:
# при работе через бота они не нужны вовсе, а проверка на них останавливала бы
# установку у тех, кто их получить не может.
REQUIRED_KEYS = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_OWNER_ID",
]


# --- .env ---------------------------------------------------------------


def ensure_env_file() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    if ENV_FILE.exists():
        return
    if EXAMPLE.exists():
        ENV_FILE.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        ENV_FILE.write_text(
            "\n".join(f"{key}=" for key in REQUIRED_KEYS) + "\n", encoding="utf-8"
        )


def env_set(key: str, value: str) -> None:
    ensure_env_file()
    # Токен, скопированный с переносом строки, иначе разорвал бы файл настроек
    value = value.replace("\r", " ").replace("\n", " ").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    lines = ENV_FILE.read_text(encoding="utf-8-sig").splitlines()
    found = False
    for index, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[index] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"OK: {key} записан")  # значение не печатаем никогда


# --- runtime ------------------------------------------------------------


def load_runtime() -> dict:
    return read_json(RUNTIME, {"stop_list": [], "chats": [], "last_run": None})


def save_runtime(data: dict) -> None:
    write_json(RUNTIME, data)


# --- установка как механизм ---------------------------------------------
#
# Раньше установка была только текстом инструкции: Клод мог её прочитать,
# а мог не прочитать, мог задать вопросы, а мог пропустить. Ничто этого
# не проверяло. Продукт, вся ценность которого в самонастройке, держался
# на честном слове модели - и однажды не сработал: сессия позвала скрипты
# напрямую, ни одного вопроса задано не было, человек получил систему,
# которая ничего у него не спросила.
#
# Теперь установка - это состояние. Вопросы пронумерованы, ответы записаны,
# незаконченная установка физически не пускает разбор.

QUESTIONS = [
    {
        "id": "consent",
        "kind": "yesno",
        "text": (
            "Chatkeeper будет читать твою личную переписку и отдавать её на разбор "
            "языковой модели. Твои собеседники об этом не узнают и согласия не давали. "
            "Переписка не уходит ни на какие чужие серверы, кроме самой модели. "
            "Согласна продолжить?"
        ),
    },
    {
        "id": "digest_time",
        "kind": "time",
        "text": "Во сколько присылать утреннюю сводку? Обычно в 9 утра.",
    },
    {
        "id": "calendar",
        "kind": "yesno",
        "text": (
            "Когда в переписке появится созвон с точным временем, я могу присылать "
            "файл, который одним нажатием ставит встречу в календарь с напоминанием. "
            "Нужно?"
        ),
    },
    {
        "id": "voice",
        "kind": "yesno",
        "needs_voice": True,  # без движка расшифровки вопрос бессмысленный
        "text": (
            "Голосовые сообщения я могу переводить в текст прямо на твоём компьютере, "
            "наружу они не уходят. Включаем?"
        ),
    },
]

YES = {"да", "ага", "конечно", "yes", "y", "1", "true", "давай", "нужно", "включаем"}
NO = {"нет", "no", "n", "0", "false", "не надо", "не нужно"}


def voice_available() -> bool:
    """Есть ли чем расшифровывать. Вопрос про голосовые иначе не задаём."""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import voice

        return voice.find_engine() is not None
    except Exception:
        return False


def answers(runtime: dict) -> dict:
    return (runtime.get("setup") or {}).get("answers") or {}


def pending(runtime: dict) -> list[dict]:
    """Вопросы, которые ещё не заданы. Порядок фиксирован и важен."""
    given = answers(runtime)
    left = []
    for question in QUESTIONS:
        if question["id"] in given:
            continue
        if question.get("needs_voice") and not voice_available():
            continue
        left.append(question)
    return left


def setup_ready(runtime: dict | None = None) -> tuple[bool, str]:
    """Готова ли система к разбору. Второе значение - что мешает, по-человечески."""
    runtime = load_runtime() if runtime is None else runtime
    given = answers(runtime)

    if given.get("consent") is False:
        return False, (
            "Разбор переписки не разрешён. Пока согласие не дано, система не работает - "
            "это не сбой, а осознанный запрет."
        )
    left = pending(runtime)
    if left:
        return False, (
            f"Установка не закончена: осталось вопросов - {len(left)}. "
            "Следующий: chatkeeper setup wizard"
        )

    env = load_env() if ENV_FILE.exists() else {}
    missing = [key for key in REQUIRED_KEYS if not env.get(key)]
    if missing:
        return False, "Установка не закончена: не задан ключ бота или владелец."
    return True, ""


# --- вопросы про чаты ---------------------------------------------------
#
# Списка диалогов у бота нет и быть не может, поэтому спросить обо всех чатах
# сразу нельзя: показывать нечего. Зато можно спрашивать по одному, когда чат
# впервые дал о себе знать. Через неделю обычной переписки карта соберётся сама,
# без выгрузки и без анкеты на сорок пунктов.
#
# Эти вопросы намеренно НЕ блокируют разбор: новый собеседник не должен
# останавливать утреннюю сводку.
#
# Спрашиваем НЕ сразу, а списком через неделю после установки. Вопрос про каждого
# человека в отдельности сразу после сводки навязчив: система дёргает по мелочи
# каждый день. За неделю чаты накапливаются, и человек один раз смотрит на список
# целиком - так он и решение принимает осмысленнее, видя картину, а не отдельное имя.

REVIEW_AFTER_DAYS = 7

CHAT_KINDS = {
    "рабочий": "work",
    "рабочая": "work",
    "работа": "work",
    "рабочее": "work",
    "личный": "personal",
    "личная": "personal",
    "личное": "personal",
    "не читать": "stop",
    "никогда": "stop",
    "стоп": "stop",
    "запретить": "stop",
}


def unknown_chats(runtime: dict) -> list[dict]:
    """Чаты, про которые человека ещё не спрашивали."""
    return [c for c in runtime.get("chats", []) if not c.get("kind")]


def review_due(runtime: dict) -> bool:
    """Пора ли показать список чатов целиком."""
    if not unknown_chats(runtime):
        return False
    raw = (runtime.get("setup") or {}).get("review_after")
    if not raw:
        return False
    try:
        return datetime.now(timezone.utc) >= datetime.fromisoformat(raw)
    except ValueError:
        return False


def schedule_review(runtime: dict) -> None:
    """Назначает разбор чатов через неделю. Вызывается, когда установка закончена."""
    block = runtime.setdefault("setup", {})
    if block.get("review_after"):
        return
    when = datetime.now(timezone.utc) + timedelta(days=REVIEW_AFTER_DAYS)
    block["review_after"] = when.isoformat()


def review_list(runtime: dict) -> str:
    """Список чатов для показа человеку. Номера - чтобы отвечать словами, а не id."""
    rows = []
    for number, chat in enumerate(unknown_chats(runtime), start=1):
        rows.append(
            f"{number}. {chat.get('name')} ({chat.get('type', 'личный')}) "
            f"id={chat.get('id')}"
        )
    return "\n".join(rows)


def chat_question(chat: dict) -> str:
    kind = chat.get("type", "личный")
    return (
        f"Появилась переписка: {chat.get('name')} ({kind}). "
        "Она рабочая или личная? Если это чат, в который лучше не заглядывать "
        "вообще никогда, так и скажи - закрою наглухо. "
        "Про рабочие можно сразу назвать проект: «рабочий: ремонт офиса»."
    )


def answer_chat(runtime: dict, chat_id: int, raw: str) -> str:
    """Ответ про чат. Возвращает человеческое подтверждение."""
    lowered = raw.lower().strip()
    project = ""
    for separator in (":", "-", ","):
        if separator in lowered:
            head, _, tail = lowered.partition(separator)
            if head.strip() in CHAT_KINDS:
                lowered, project = head.strip(), tail.strip()
                break

    kind = CHAT_KINDS.get(lowered)
    if kind is None:
        die(
            f"Не понял ответ про чат: {raw}. "
            "Скажи «рабочий», «личный» или «не читать»."
        )
        return ""

    chats = runtime.get("chats", [])
    target = next((c for c in chats if int(c.get("id", 0)) == chat_id), None)
    if target is None:
        die(f"Такого чата нет в списке: {chat_id}")
        return ""

    if kind == "stop":
        stop = {int(x) for x in runtime.get("stop_list", [])}
        stop.add(chat_id)
        runtime["stop_list"] = sorted(stop)
        # Из работы убираем сразу: стоп-список сильнее всего остального,
        # и такой чат не должен попадать даже в буфер на диске.
        runtime["chats"] = [c for c in chats if int(c.get("id", 0)) != chat_id]
        return f"Чат «{target.get('name')}» закрыт наглухо и читаться не будет."

    target["kind"] = kind
    if project:
        target["project"] = project
    name = target.get("name")
    if kind == "work":
        return f"Чат «{name}» помечен рабочим" + (f", проект «{project}»." if project else ".")
    return f"Чат «{name}» помечен личным."


def missing_steps() -> list[tuple[str, str]]:
    """Что осталось сделать руками. Порядок важен: следующий шаг зависит от прошлого."""
    env = load_env() if ENV_FILE.exists() else {}
    steps: list[tuple[str, str]] = []

    if not env.get("TELEGRAM_BOT_TOKEN"):
        steps.append(
            (
                "bot_token",
                "Нужен бот - он будет и приносить сообщения, и присылать сводку.\n"
                "Попроси человека: в телеграме найти @BotFather, написать /newbot, "
                "придумать имя и адрес, заканчивающийся на bot, и прислать длинную "
                "строку с двоеточием.\n"
                "Дальше: chatkeeper setup env-set TELEGRAM_BOT_TOKEN <строка>",
            )
        )
        return steps  # без ключа остальные шаги проверять бессмысленно

    if not env.get("TELEGRAM_OWNER_ID"):
        steps.append(
            (
                "bot_owner",
                "Попроси нажать Start в чате с новым ботом - без этого телеграм "
                "запрещает боту писать первым.\n"
                "Дальше: chatkeeper setup bot-owner",
            )
        )
        return steps

    state = read_json(STATE / "bot_state.json", default={}) or {}
    connection = state.get("connection") or {}
    if not connection:
        steps.append(
            (
                "connect",
                "Бот ещё не подключён к личным чатам. Скажи человеку:\n"
                "  открой телеграм НА КОМПЬЮТЕРЕ (на телефоне этот пункт ошибочно "
                "просит Premium, подписка не нужна),\n"
                "  Настройки - Telegram для бизнеса - Чат-боты, вписать адрес бота,\n"
                "  прав не выдавать никаких: система только читает.\n"
                "Перед этим у бота должен быть включён Secretary Mode: @BotFather - "
                "/mybots - бот - Bot Settings - Secretary Mode.\n"
                "Проверить: chatkeeper collect status",
            )
        )
    elif connection.get("enabled") is False:
        steps.append(
            (
                "connect",
                "Подключение бота к личным чатам отключено в настройках телеграма. "
                "Попроси включить его заново: Настройки - Telegram для бизнеса - Чат-боты.",
            )
        )
    return steps


def cmd_wizard() -> None:
    """Печатает следующий незаданный вопрос. Ответ записывает команда answer.

    Формат вывода машинный намеренно: его читает Клод, а человеку он задаёт
    вопрос своими словами. Так текст вопроса можно менять, не трогая логику.
    """
    runtime = load_runtime()
    if answers(runtime).get("consent") is False:
        print("ОТКАЗ")
        print("Человек отказался от разбора переписки. Установку не продолжай.")
        return

    left = pending(runtime)
    if left:
        question = left[0]
        print(f"ВОПРОС {question['id']}")
        print(question["text"])
        print(f"Осталось вопросов: {len(left)}")
        print(f"Ответ записывается: chatkeeper setup answer {question['id']} <ответ>")
        return

    # Вопросы кончились, но установка - это не только вопросы. Дальше идут шаги,
    # где человек что-то делает руками в телеграме. Раньше мастер про них не знал
    # и говорил ГОТОВО при отсутствующем ключе бота: сессия шла разбирать и упиралась
    # в отказ, не понимая, чего от неё хотят.
    for marker, text in missing_steps():
        print(f"ШАГ {marker}")
        print(text)
        return

    # Основные вопросы позади. Чаты разбираем не по одному и не сразу,
    # а списком через неделю: так человек видит картину целиком.
    if review_due(runtime):
        print("ВОПРОС chats-review")
        print(
            "Прошла неделя, вот все чаты, из которых приходили сообщения. "
            "Покажи человеку список без номеров id и спроси, какие из них рабочие. "
            "Остальные будут считаться личными. Если какой-то чат читать не стоит "
            "вовсе - запиши его отдельно как «не читать»."
        )
        print(review_list(runtime))
        print(
            "Ответ записывается: chatkeeper setup answer chats-work <id рабочих через запятую>"
        )
        print("Закрыть чат наглухо: chatkeeper setup answer chat:<id> не читать")
        return

    print("ГОТОВО")
    print("Все вопросы отвечены. Осталось проверить: chatkeeper setup check")


def cmd_answer(argv: list[str]) -> None:
    if len(argv) < 2:
        die("Нужно: answer ВОПРОС ОТВЕТ")
    key, raw = argv[0].strip(), " ".join(argv[1:]).strip()

    if key == "chats-work":
        runtime = load_runtime()
        work = set(parse_ids(raw)) if raw.lower() not in {"нет", "никакие", "-"} else set()
        marked_work, marked_personal = 0, 0
        for chat in unknown_chats(runtime):
            if int(chat.get("id", 0)) in work:
                chat["kind"] = "work"
                marked_work += 1
            else:
                # Всё, что человек не назвал рабочим, считается личным. Так вопрос
                # остаётся одним, а не превращается в опрос по каждому чату.
                chat["kind"] = "personal"
                marked_personal += 1
        save_runtime(runtime)
        print(f"Рабочих чатов: {marked_work}, личных: {marked_personal}")
        print("Список можно менять словами в любой момент.")
        return

    if key.startswith("chat:"):
        tail = key.split(":", 1)[1]
        if not tail.lstrip("-").isdigit():
            die(f"Непонятный номер чата: {tail}")
        runtime = load_runtime()
        print(answer_chat(runtime, int(tail), raw))
        save_runtime(runtime)
        left = len(unknown_chats(runtime))
        print(f"Новых чатов осталось: {left}" if left else "Про все чаты спрошено.")
        return

    question = next((q for q in QUESTIONS if q["id"] == key), None)
    if question is None:
        die(f"Нет такого вопроса: {key}. Список: chatkeeper setup wizard")

    if question["kind"] == "yesno":
        lowered = raw.lower()
        if lowered in YES:
            value = True
        elif lowered in NO:
            value = False
        else:
            die(f"Ответ на «{key}» должен быть да или нет, а пришло: {raw}")
            return
    else:  # время
        value = normalize_time(raw)

    runtime = load_runtime()
    block = runtime.setdefault("setup", {})
    block.setdefault("answers", {})[key] = value
    if key == "digest_time":
        runtime["digest_time"] = value  # то же поле, что ставит команда schedule
    save_runtime(runtime)

    print(f"OK: ответ на «{key}» записан")
    ready, reason = setup_ready(runtime)
    if ready:
        # Установка закончена - значит через неделю пора будет показать список чатов.
        schedule_review(runtime)
        save_runtime(runtime)
        print("Установка закончена.")
    else:
        print(reason)


def normalize_time(raw: str) -> str:
    """Человек говорит «в 9», «9:00», «09.00». Всё это одно и то же время."""
    cleaned = raw.lower().replace("в ", "").replace(".", ":").replace(" ", "")
    if cleaned.isdigit() and len(cleaned) <= 2:
        cleaned = f"{cleaned}:00"
    try:
        hours, minutes = (int(part) for part in cleaned.split(":"))
    except ValueError:
        die(f"Не понял время: {raw}. Скажи как «9:00».")
        return "09:00"
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        die(f"Такого времени не бывает: {raw}")
    return f"{hours:02d}:{minutes:02d}"


def cmd_gate() -> None:
    """Проверка для скриптов: можно ли работать. Ненулевой код останавливает разбор."""
    ready, reason = setup_ready()
    if ready:
        print("OK: установка закончена")
        return
    print(reason)
    sys.exit(1)


def parse_ids(raw: str) -> list[int]:
    ids = []
    for chunk in raw.replace(" ", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError:
            die(f"«{chunk}» не похоже на id чата - нужны числа через запятую")
    return ids


# --- команды ------------------------------------------------------------


def cmd_bootstrap() -> None:
    """Создаёт рабочее окружение в папке данных. Запускается любым python-ом."""
    DATA.mkdir(parents=True, exist_ok=True)
    py = python_path()

    if not py.exists():
        print(f"Создаю рабочее окружение в {VENV}")
        result = subprocess.run(
            [sys.executable, "-m", "venv", str(VENV)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            die(
                "Не удалось создать окружение python. "
                f"Причина: {result.stderr.strip()[:200]}"
            )
    if not py.exists():
        die(f"Окружение создано, но python в нём не найден ({py}).")

    # Пустой список - это обычное состояние, а не ошибка: работа через бота
    # обходится стандартной библиотекой. Вызывать pip без единого имени пакета
    # нельзя - он завершится с ошибкой, и установка встанет на ровном месте.
    if PACKAGES:
        print("Ставлю библиотеки, это займёт минуту")
        subprocess.run(
            [str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
            capture_output=True,
            text=True,
        )
        result = subprocess.run(
            [str(py), "-m", "pip", "install", "--quiet", *PACKAGES],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            die(
                "Не удалось поставить библиотеки. Скорее всего нет интернета "
                f"или он идёт через VPN. Причина: {result.stderr.strip()[:200]}"
            )

    ensure_env_file()
    print("OK: окружение готово")


INBOX_MARKER = "CHATKEEPER-INBOX-V1"  # тот же маркер, что ставит collect.py


def cmd_forget(argv: list[str]) -> None:
    """Находит и по подтверждению удаляет историю сессий, где осела переписка.

    Claude Code сохраняет всё прочитанное в файлы истории. Удаление `inbox.md`
    после разбора убирает только сам файл - копия остаётся в истории навсегда.
    Эта команда существует, чтобы обещание о приватности можно было сдержать.
    """
    projects = Path.home() / ".claude" / "projects"
    if not projects.exists():
        print("Истории сессий не найдено - удалять нечего.")
        return

    found: list[tuple[Path, int]] = []
    for path in projects.rglob("*.jsonl"):
        try:
            if INBOX_MARKER in path.read_text(encoding="utf-8", errors="ignore"):
                found.append((path, path.stat().st_size))
        except OSError:
            continue

    if not found:
        print("Переписки в истории сессий не найдено.")
        return

    total_mb = sum(size for _, size in found) / 1024 / 1024
    print(f"Файлов истории с перепиской: {len(found)} ({total_mb:.1f} МБ)\n")
    for path, size in sorted(found, key=lambda item: -item[1]):
        print(f"  {size / 1024 / 1024:6.1f} МБ  {path.name}")

    if "--confirm" not in argv:
        print(
            "\nЭто только список, ничего не удалено.\n"
            "ВАЖНО: удаляется вся сессия целиком. Если в этих же сессиях шла "
            "другая работа, она удалится вместе с перепиской.\n"
            "Удалить: chatkeeper setup forget --confirm"
        )
        return

    removed = 0
    for path, _ in found:
        try:
            path.unlink()
            removed += 1
        except OSError as error:
            print(f"  не удалось удалить {path.name}: {error}")
    print(f"\nУдалено файлов истории: {removed} из {len(found)}")


def cmd_check() -> None:
    env = load_env() if ENV_FILE.exists() else {}
    runtime = load_runtime()

    print(f"Папка с данными: {DATA}\n")
    print("Установка Chatkeeper - что готово:\n")

    ready_env = python_path().exists()
    print(f"  Рабочее окружение      {'есть' if ready_env else 'НЕТ'}")

    for key in REQUIRED_KEYS:
        mark = "есть" if env.get(key) else "НЕТ"
        print(f"  {key:<22} {mark}")

    # Что именно проверять, зависит от источника: при работе через бота файла
    # входа не существует в принципе, и старая строчка «Вход НЕТ» пугала бы
    # человека тем, что на самом деле в полном порядке.
    source = (env.get("CHATKEEPER_SOURCE") or "bot").strip().lower()
    if source == "account":
        session = STATE / "telegram.session"
        source_ready = session.exists()
        print(f"\n  Вход в телеграм        {'выполнен' if source_ready else 'НЕТ'}")
    else:
        bot_state = read_json(STATE / "bot_state.json", default={}) or {}
        connection = bot_state.get("connection") or {}
        source_ready = bool(connection) and connection.get("enabled", True)
        if not connection:
            mark = "НЕТ"
        elif source_ready:
            mark = "работает"
        else:
            mark = "ОТКЛЮЧЕНО в телеграме"
        print(f"\n  Личные чаты подключены {mark}")

    chats = runtime.get("chats", [])
    print(f"  Чатов в работе         {len(chats) if chats else 'НЕТ'}")

    stop = runtime.get("stop_list", [])
    print(f"  Стоп-список            {len(stop)} чатов")

    when = runtime.get("digest_time")
    print(f"  Время сводки           {when or 'НЕ ЗАДАНО'}")

    missing = [k for k in REQUIRED_KEYS if not env.get(k)]
    # Чаты в реестре больше не признак готовности: при работе через бота реестр
    # пуст ровно до первого входящего сообщения, а установка при этом закончена.
    if missing or not source_ready or not when or not ready_env:
        print("\nУстановка не закончена.")
        sys.exit(1)
    print("\nВсё на месте, можно запускать разбор.")


def cmd_account_deps() -> None:
    """Доставить библиотеки для источника account - входа под живым аккаунтом.

    Отдельной командой, а не при установке всем подряд: обычный путь через бота
    обходится стандартной библиотекой, и тянуть телефонный клиент телеграма
    каждому - это минуты ожидания ради дороги, которой почти никто не пойдёт.
    """
    py = python_path()
    if not py.exists():
        die("Окружение python не готово. Сначала bootstrap.")
    print("Ставлю библиотеки для входа под аккаунтом, это займёт минуту")
    result = subprocess.run(
        [str(py), "-m", "pip", "install", "--quiet", *PACKAGES_ACCOUNT],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        die(
            "Не удалось поставить библиотеки. Скорее всего нет интернета "
            f"или он идёт через VPN. Причина: {result.stderr.strip()[:200]}"
        )
    print("OK: теперь источник account доступен")


def cmd_env_set(argv: list[str]) -> None:
    if len(argv) < 2:
        die("Нужно: env-set КЛЮЧ ЗНАЧЕНИЕ")
    env_set(argv[0], argv[1])


def cmd_bot_owner() -> None:
    """Ловит id владельца из сообщения, которое он отправил боту."""
    token = load_env().get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        die("Сначала запиши TELEGRAM_BOT_TOKEN.")

    url = f"https://api.telegram.org/bot{token}/getUpdates?limit=10"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError:
        die(
            "Токен бота не подошёл. Проверь, что он скопирован целиком, "
            "вместе с цифрами до двоеточия, и пришли ещё раз."
        )
        return
    except urllib.error.URLError:
        die(
            "Не получилось связаться с Telegram. Похоже, пропал интернет "
            "или отключился VPN."
        )
        return

    messages = [u for u in data.get("result", []) if u.get("message")]
    if not messages:
        die(
            "Бот не получил ни одного сообщения. "
            "Открой своего бота в телеграме и нажми Start."
        )
        return

    # Боту мог написать кто-то ещё - например, случайно найдя его по адресу.
    # Молча взять последнего отправителя нельзя: сводка со всеми договорённостями
    # уехала бы чужому человеку.
    senders = {m["message"]["chat"]["id"]: m["message"]["chat"] for m in messages}
    if len(senders) > 1:
        names = ", ".join(
            f"{c.get('first_name', 'без имени')} (id {i})" for i, c in senders.items()
        )
        die(
            "Боту написал не один человек, и непонятно, кто из них владелец: "
            f"{names}.\n"
            "Задай владельца явно: chatkeeper setup env-set TELEGRAM_OWNER_ID <id>. "
            "И лучше смени адрес бота - кто-то посторонний его знает."
        )
        return

    chat = next(iter(senders.values()))
    env_set("TELEGRAM_OWNER_ID", str(chat["id"]))
    print(f"Владелец определён: {chat.get('first_name', 'без имени')}")
    print("Если это не ты - немедленно скажи об этом: сводка уйдёт не туда.")


def cmd_chats_keep(argv: list[str]) -> None:
    if not argv:
        die("Нужно: chats-keep ID,ID,ID")
    keep = set(parse_ids(argv[0]))
    runtime = load_runtime()
    before = runtime.get("chats", [])

    # Убранные чаты запоминаем навсегда. Иначе следующий отбор вернёт их в разбор:
    # человек сказал «этот не надо», а через неделю он снова в сводке.
    excluded = set(int(x) for x in runtime.get("excluded", []))
    excluded.update(int(c["id"]) for c in before if int(c["id"]) not in keep)
    runtime["excluded"] = sorted(excluded)

    runtime["chats"] = [c for c in before if int(c["id"]) in keep]
    save_runtime(runtime)
    print(f"Оставлено чатов: {len(runtime['chats'])} из {len(before)}")
    print(f"Убранные запомнены, повторный отбор их не вернёт: {len(excluded)}")


def cmd_stop_add(argv: list[str]) -> None:
    if not argv:
        die("Нужно: stop-add ID,ID")
    runtime = load_runtime()
    stop = set(int(x) for x in runtime.get("stop_list", []))
    stop.update(parse_ids(argv[0]))
    runtime["stop_list"] = sorted(stop)
    # из разбора эти чаты убираем сразу же
    runtime["chats"] = [c for c in runtime.get("chats", []) if int(c["id"]) not in stop]
    save_runtime(runtime)
    print(f"В стоп-списке чатов: {len(stop)}. Они не читаются никогда.")


def cmd_schedule(argv: list[str]) -> None:
    if not argv:
        die("Нужно: schedule ЧЧ:ММ")
    raw = argv[0].strip()
    try:
        hours, minutes = (int(part) for part in raw.split(":"))
        if not (0 <= hours < 24 and 0 <= minutes < 60):
            raise ValueError
    except ValueError:
        die(f"«{raw}» не похоже на время. Нужно так: 09:00")
        return
    runtime = load_runtime()
    runtime["digest_time"] = f"{hours:02d}:{minutes:02d}"
    save_runtime(runtime)
    print(f"Время сводки: {runtime['digest_time']}")


def main() -> None:
    utf8_console()
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)

    command, argv = sys.argv[1], sys.argv[2:]
    if command == "bootstrap":
        cmd_bootstrap()
    elif command == "check":
        cmd_check()
    elif command == "env-set":
        cmd_env_set(argv)
    elif command == "bot-owner":
        cmd_bot_owner()
    elif command == "chats-keep":
        cmd_chats_keep(argv)
    elif command == "stop-add":
        cmd_stop_add(argv)
    elif command == "schedule":
        cmd_schedule(argv)
    elif command == "forget":
        cmd_forget(argv)
    elif command == "account-deps":
        cmd_account_deps()
    elif command == "wizard":
        cmd_wizard()
    elif command == "answer":
        cmd_answer(argv)
    elif command == "gate":
        cmd_gate()
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
