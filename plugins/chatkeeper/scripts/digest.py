"""Chatkeeper: память системы и сборка утренней сводки.

Здесь нет ни одного обращения к модели. Разбор живой речи - работа Claude,
а раскладывание готовых записей по блокам должно быть дешёвым, одинаковым
каждый день и проверяемым тестом. Поэтому обычный код.

Команды:
    preview  - показать сводку в консоли, ничего не отправляя
    send     - собрать и отправить сводку в телеграм
    demo     - положить выдуманные записи и отправить сводку по ним
"""

from __future__ import annotations

import html
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import STATE, read_json, utf8_console, write_json  # noqa: E402

# Тот же маркер, что ставит источник сообщений в файл разбора. По нему команда
# forget находит сессии, где осела переписка, поэтому строка должна совпадать
# в обоих местах буква в букву.
INBOX_MARKER = "<!-- CHATKEEPER-INBOX-V1 -->"
from send import send_message  # noqa: E402

COMMITMENTS = STATE / "commitments.json"

SILENCE_DAYS = 3  # столько ждём ответа, прежде чем напомнить о своём вопросе

WEEKDAYS = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]
MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


# --- хранилище ----------------------------------------------------------


def normalize(raw: object) -> dict | None:
    """Приводит одну запись к пригодному виду или отбрасывает её.

    Файл памяти пишет языковая модель, а она регулярно ошибается в мелочах:
    число строкой, пропущенное описание, имя вместо строки числом. Без проверки
    любая такая мелочь роняет сборку сводки - и, по инструкции, разбор не
    закрывается, запись остаётся в памяти, и завтра всё падает снова.
    Человек при этом видит только тишину. Поэтому чиним, а не падаем.
    """
    if not isinstance(raw, dict):
        return None

    item = dict(raw)

    text = item.get("text")
    if text is None or not str(text).strip():
        return None  # запись без описания бесполезна в сводке
    item["text"] = str(text).strip()

    for field in ("counterparty", "chat_name", "type", "status", "id"):
        value = item.get(field)
        if value is not None and not isinstance(value, str):
            item[field] = str(value)

    for field in ("due", "created", "closed_at"):
        value = item.get(field)
        if value is not None and not isinstance(value, str):
            item[field] = None

    effort = item.get("effort_days")
    if effort is not None:
        try:
            item["effort_days"] = int(float(effort))
        except (TypeError, ValueError):
            item["effort_days"] = None

    item.setdefault("type", "promise_out")
    item.setdefault("status", "open")
    return item


def load(path: Path | None = None) -> list[dict]:
    data = read_json(path or COMMITMENTS, [])
    if isinstance(data, dict):
        data = data.get("items", [])
    if not isinstance(data, list):
        return []

    items, dropped = [], 0
    for raw in data:
        clean = normalize(raw)
        if clean is None:
            dropped += 1
        else:
            items.append(clean)

    if dropped:
        print(f"Пропущено непонятных записей в памяти: {dropped}")
    return items


def save(items: list[dict], path: Path | None = None) -> None:
    write_json(path or COMMITMENTS, {"items": items})


def parse_moment(value: str | None) -> datetime | None:
    """Приводит время к локальной зоне. Строку без зоны считает местным временем."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment.astimezone()


# --- отбор записей ------------------------------------------------------


def is_open(item: dict) -> bool:
    return item.get("status", "open") == "open"


def due_date(item: dict) -> date | None:
    moment = parse_moment(item.get("due"))
    return moment.date() if moment else None


def human_due(item: dict, today: date) -> str:
    when = due_date(item)
    if when is None:
        return "без срока"
    delta = (when - today).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    if delta == -1:
        return "вчера"
    if delta < 0:
        return f"просрочено на {abs(delta)} дн."
    return f"через {delta} дн."


def line(item: dict, today: date, with_due: bool = True) -> str:
    who = item.get("counterparty")
    text = html.escape(item.get("text", "без описания"))
    parts = [f"• {text}"]
    if who:
        parts.append(f"— {html.escape(who)}")
    if with_due and item.get("due"):
        parts.append(f"({human_due(item, today)})")
    return " ".join(parts)


def needs_early_start(item: dict, today: date) -> bool:
    """Работы на два дня, а срок завтра - значит начинать сегодня."""
    when = due_date(item)
    effort = item.get("effort_days")
    if when is None or not effort or effort <= 1:
        return False
    days_left = (when - today).days
    return 0 < days_left <= effort


# --- сборка сводки ------------------------------------------------------


def build(items: list[dict], today: date | None = None) -> str:
    today = today or datetime.now().astimezone().date()
    tomorrow = today + timedelta(days=1)
    live = [i for i in items if is_open(i)]

    meetings = [i for i in live if i.get("type") == "meeting"]
    tasks = [i for i in live if i.get("type") in ("promise_out", "deadline")]
    waiting = [i for i in live if i.get("type") == "promise_in"]
    questions = [i for i in live if i.get("type") == "question"]

    overdue = [i for i in tasks if (d := due_date(i)) and d < today]
    today_tasks = [i for i in tasks if due_date(i) == today]
    early = [i for i in tasks if needs_early_start(i, today)]
    undated = [i for i in tasks if due_date(i) is None]

    today_meetings = [i for i in meetings if due_date(i) == today]
    tomorrow_meetings = [i for i in meetings if due_date(i) == tomorrow]

    silent = []
    for item in questions:
        asked = parse_moment(item.get("created"))
        if asked and (today - asked.date()).days >= SILENCE_DAYS:
            silent.append(item)

    header = (
        f"<b>{WEEKDAYS[today.weekday()].capitalize()}, "
        f"{today.day} {MONTHS[today.month - 1]}</b>"
    )
    blocks: list[str] = [header]

    if overdue:
        rows = "\n".join(line(i, today) for i in overdue)
        blocks.append(f"\n🔴 <b>Просрочено</b>\n{rows}")

    if today_meetings:
        rows = []
        for item in sorted(today_meetings, key=lambda i: i.get("due") or ""):
            moment = parse_moment(item.get("due"))
            clock = f"{moment:%H:%M}" if moment else "время не указано"
            who = item.get("counterparty")
            tail = f" — {html.escape(who)}" if who else ""
            rows.append(f"• <b>{clock}</b> {html.escape(item.get('text', ''))}{tail}")
        blocks.append("\n📞 <b>Сегодня созвоны</b>\n" + "\n".join(rows))

    if today_tasks:
        rows = "\n".join(line(i, today, with_due=False) for i in today_tasks)
        blocks.append(f"\n📌 <b>Обещано на сегодня</b>\n{rows}")

    if early:
        rows = []
        for item in early:
            effort = item.get("effort_days")
            rows.append(
                f"• {html.escape(item.get('text', ''))} — срок "
                f"{human_due(item, today)}, работы на {effort} дн."
            )
        blocks.append(
            "\n⏳ <b>Начать сегодня, иначе не успеть</b>\n" + "\n".join(rows)
        )

    if undated:
        rows = "\n".join(line(i, today, with_due=False) for i in undated)
        blocks.append(f"\n📋 <b>Висит без срока</b>\n{rows}")

    if silent:
        rows = []
        for item in silent:
            asked = parse_moment(item.get("created"))
            days = (today - asked.date()).days if asked else "?"
            who = item.get("counterparty", "неизвестно кто")
            rows.append(
                f"• {html.escape(who)}, {days} дн. — "
                f"«{html.escape(item.get('text', ''))}»"
            )
        blocks.append("\n💤 <b>Мне не ответили</b>\n" + "\n".join(rows))

    if waiting:
        rows = "\n".join(line(i, today) for i in waiting)
        blocks.append(f"\n🤝 <b>Обещали мне</b>\n{rows}")

    if tomorrow_meetings:
        rows = []
        for item in sorted(tomorrow_meetings, key=lambda i: i.get("due") or ""):
            moment = parse_moment(item.get("due"))
            clock = f"{moment:%H:%M}" if moment else "время не указано"
            rows.append(f"• {clock} {html.escape(item.get('text', ''))}")
        blocks.append("\n🌅 <b>Завтра</b>\n" + "\n".join(rows))

    if len(blocks) == 1:
        blocks.append("\nНа сегодня ничего не висит. Свободный день.")

    return "\n".join(blocks)


# --- команды ------------------------------------------------------------


def demo_items() -> list[dict]:
    now = datetime.now().astimezone()
    today = now.date()

    def at(day_shift: int, hour: int) -> str:
        moment = (now + timedelta(days=day_shift)).replace(
            hour=hour, minute=0, second=0, microsecond=0
        )
        return moment.isoformat()

    return [
        {
            "id": "demo-1", "type": "promise_out", "status": "open",
            "text": "скинуть смету по ремонту", "counterparty": "Петя",
            "chat_name": "Ремонт офиса", "created": at(-3, 12), "due": at(-1, 18),
        },
        {
            "id": "demo-2", "type": "promise_out", "status": "open",
            "text": "отправить правки по договору", "counterparty": "Аня",
            "chat_name": "Юристы", "created": at(-1, 10), "due": at(0, 18),
        },
        {
            "id": "demo-3", "type": "meeting", "status": "open",
            "text": "созвон по запуску", "counterparty": "команда",
            "chat_name": "Проект Гамма", "created": at(-2, 9), "due": at(0, 15),
        },
        {
            "id": "demo-4", "type": "promise_out", "status": "open",
            "text": "презентация для инвесторов", "counterparty": "Сергей",
            "chat_name": "Проект Гамма", "created": at(-4, 11), "due": at(2, 12),
            "effort_days": 3,
        },
        {
            "id": "demo-5", "type": "question", "status": "open",
            "text": "когда пришлёшь доступы к панели?", "counterparty": "Костя",
            "chat_name": "Разработка", "created": at(-6, 14),
        },
        {
            "id": "demo-6", "type": "promise_in", "status": "open",
            "text": "прислать макеты", "counterparty": "Лена",
            "chat_name": "Дизайн", "created": at(-2, 16), "due": at(1, 18),
        },
        {
            "id": "demo-7", "type": "meeting", "status": "open",
            "text": "интервью с кандидатом", "counterparty": "HR",
            "chat_name": "Найм", "created": at(-1, 9), "due": at(1, 11),
        },
        {
            "id": "demo-8", "type": "promise_out", "status": "open",
            "text": "посмотреть подрядчиков по видео", "counterparty": "Марина",
            "chat_name": "Маркетинг", "created": at(-5, 13),
        },
        {
            "id": "demo-9", "type": "promise_out", "status": "done",
            "text": "оплатить хостинг", "counterparty": "Костя",
            "chat_name": "Разработка", "created": at(-7, 10), "due": at(-2, 18),
            "closed_at": at(-2, 15),
        },
    ]


def cmd_preview() -> None:
    # Маркер обязателен. Сводка - это выжимка из чужой переписки: кто кому что
    # обещал, с именами. Напечатанная в консоль, она оседает в истории сессии
    # Claude Code, а команда «почисти историю разборов» ищет сессии именно
    # по этому маркеру. Без него человеку обещана очистка, которая не случится.
    print(INBOX_MARKER)
    print(build(load()))


def cmd_send() -> None:
    send_message(build(load()), parse_mode="HTML")
    print("OK: сводка отправлена")


def cmd_demo() -> None:
    """Выдуманные данные живут в своём файле и рабочую память не трогают."""
    demo_file = STATE / "commitments.demo.json"
    save(demo_items(), demo_file)
    text = build(load(demo_file))
    print(text)
    send_message(text, parse_mode="HTML")
    print(f"\nOK: демонстрационная сводка отправлена (данные в {demo_file.name})")


def main() -> None:
    utf8_console()
    commands = {"preview": cmd_preview, "send": cmd_send, "demo": cmd_demo}
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        print(f"Использование: digest.py [{' | '.join(commands)}]")
        sys.exit(2)
    commands[sys.argv[1]]()


if __name__ == "__main__":
    main()
