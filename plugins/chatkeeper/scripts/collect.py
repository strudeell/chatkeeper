"""Chatkeeper: выбор источника сообщений. Сам ничего не собирает.

Источников два, и они решают одну задачу разной ценой:

  bot      - бот, подключённый владельцем в настройках телеграма
             («Telegram для бизнеса» -> «Чат-боты»). Личные чаты, только новые
             сообщения. Ни ключей, ни подписки. Работает у всех. По умолчанию.

  account  - вход под живым аккаунтом. Видит всё, включая группы и историю,
             но требует api_id с my.telegram.org, который из России получить
             не удаётся, и попадает под пункт 1.5 правил Telegram API.

Зачем эта прослойка. Запускатель зовёт `scripts/<модуль>.py`, а весь продукт
и вся документация говорят `chatkeeper collect fetch`. Развилка спрятана сюда,
чтобы смена источника не требовала править запускатели, SKILL.md и инструкцию
по установке - то есть чтобы человеку ничего не пришлось переучивать.

Тяжёлый telethon импортируется только при выборе account: иначе установка
без него была бы невозможна, а она и есть обычный случай.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ENV_FILE, die, load_env, utf8_console  # noqa: E402

DEFAULT_SOURCE = "bot"

ACCOUNT_ONLY = {
    "login": (
        "Вход под аккаунтом нужен только источнику account. Сейчас работает бот, "
        "и входить никуда не надо: он подключается кнопками в самом телеграме."
    ),
    "chats": (
        "Отдельный отбор чатов боту не нужен: список диалогов ему недоступен, "
        "чат появляется в работе тогда, когда из него пришло сообщение."
    ),
}


def chosen_source() -> str:
    """Переменная окружения сильнее файла настроек: так удобнее проверять."""
    from_env = os.environ.get("CHATKEEPER_SOURCE", "").strip().lower()
    if from_env:
        return from_env
    if not ENV_FILE.exists():
        return DEFAULT_SOURCE
    return (load_env().get("CHATKEEPER_SOURCE", "") or DEFAULT_SOURCE).strip().lower()


GATED = {"fetch"}  # status и done нужны и во время установки, их не запираем


def guard(command: str) -> None:
    """Незаконченная установка не должна пускать разбор.

    Раньше это была рекомендация в тексте инструкции, и её удалось обойти,
    просто позвав скрипты напрямую. Человек получил систему, которая не задала
    ему ни одного вопроса. Теперь проверка стоит на дороге, а не в документе:
    обойти её нельзя ни мне, ни любой другой сессии.
    """
    if command not in GATED:
        return
    import setup

    ready, reason = setup.setup_ready()
    if ready:
        return
    print(reason)
    print("Разбор не начат: сначала установка.")
    sys.exit(1)


def main() -> None:
    utf8_console()
    source = chosen_source()
    command = sys.argv[1] if len(sys.argv) > 1 else "fetch"
    guard(command)

    if source == "account":
        try:
            import collect_account  # тянет telethon, поэтому только здесь
        except ImportError:
            die(
                "Источник account требует библиотеки, которых нет в окружении: "
                "telethon, qrcode, pillow. Поставить их можно командой "
                "chatkeeper setup account-deps. Если ставить не нужно - верни "
                "в настройках CHATKEEPER_SOURCE значение bot."
            )
            return
        collect_account.main()
        return

    if source != DEFAULT_SOURCE:
        die(
            f"Неизвестный источник сообщений: {source}. "
            "В настройках CHATKEEPER_SOURCE может быть только bot или account."
        )

    if command in ACCOUNT_ONLY:
        print(ACCOUNT_ONLY[command])
        return

    import source_bot

    source_bot.main()


if __name__ == "__main__":
    main()
