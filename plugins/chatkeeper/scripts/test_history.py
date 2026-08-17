"""Тесты импорта выгрузки. Запуск: python test_history.py

Формат выгрузки телеграма полон тихих ловушек: текст бывает списком кусков,
дата бывает местной строкой без пояснений, отправитель приходит как user123456.
Ошибка в любом месте не падает, а даёт молча искажённую переписку - поэтому
проверяем именно разбор, а не работу целиком.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from testenv import isolate  # noqa: E402

# Своя папка данных - до импорта: модули продукта закрепляют пути при загрузке.
TEMP = isolate("chatkeeper-import-test-")

import history as imp  # noqa: E402
import source_bot as src  # noqa: E402

ME = 111
THEM = 222


def export(messages, kind="personal_chat", name="Маргарита", chat_id=THEM):
    return {
        "personal_information": {"user_id": ME},
        "chats": {
            "list": [
                {"name": name, "type": kind, "id": chat_id, "messages": messages}
            ]
        },
    }


def note(text="привет", number=1, days_ago=1, **extra):
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {
        "id": number,
        "type": "message",
        "date": moment.isoformat(timespec="seconds"),
        "date_unixtime": str(int(moment.timestamp())),
        "from": "Маргарита",
        "from_id": f"user{THEM}",
        "text": text,
        **extra,
    }


def write(data) -> Path:
    path = Path(TEMP) / "result.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


class Razbor(unittest.TestCase):
    def test_tekst_iz_kusochkov_sobiraetsya(self):
        # Ссылки и жирный текст приходят списком, а не строкой.
        pieces = ["позвони на ", {"type": "phone", "text": "+7 900"}, " вечером"]
        self.assertEqual(imp.flatten(pieces), "позвони на +7 900 вечером")

    def test_otpravitel_iz_stroki(self):
        self.assertEqual(imp.sender_id({"from_id": "user123456"}), 123456)
        self.assertIsNone(imp.sender_id({}))

    def test_sluzhebnoe_ne_beryotsya(self):
        service = {"type": "service", "action": "pin_message", "id": 5}
        self.assertIsNone(imp.to_update(service, {"id": THEM}, "private"))

    def test_gruppa_poluchaet_title(self):
        update = imp.to_update(note(), {"id": -100, "name": "Ремонт"}, "group")
        self.assertEqual(update["business_message"]["chat"]["title"], "Ремонт")

    def test_lichnyy_chat_poluchaet_imya(self):
        update = imp.to_update(note(), {"id": THEM, "name": "Пётр"}, "private")
        self.assertEqual(update["business_message"]["chat"]["first_name"], "Пётр")

    def test_golosovoe_uznayotsya(self):
        update = imp.to_update(
            note(text="", media_type="voice_message", duration_seconds=4),
            {"id": THEM, "name": "Пётр"},
            "private",
        )
        self.assertEqual(src.describe_media(update["business_message"]), "голосовое")


class Vlivanie(unittest.TestCase):
    def setUp(self):
        src.BUFFER.unlink(missing_ok=True)
        src.INBOX.unlink(missing_ok=True)
        src.RUNTIME.unlink(missing_ok=True)
        (src.STATE / "bot_state.json").unlink(missing_ok=True)

    def test_svezhee_beryotsya_staroe_net(self):
        path = write(
            export([note(text="свежее", number=1, days_ago=2),
                    note(text="древнее", number=2, days_ago=40)])
        )
        imp.cmd_import(str(path), days=7)
        src.write_inbox({}, {}, datetime.now(timezone.utc))
        text = src.INBOX.read_text(encoding="utf-8")
        self.assertIn("свежее", text)
        self.assertNotIn("древнее", text)

    def test_vladelets_zapominaetsya(self):
        path = write(export([note(days_ago=1)]))
        imp.cmd_import(str(path), days=7)
        state = src.load_state()
        self.assertEqual((state.get("connection") or {}).get("user_id"), ME)

    def test_zapretnyy_chat_ne_vlivaetsya(self):
        src.write_json(src.RUNTIME, {"stop_list": [THEM]})
        path = write(export([note(text="секрет", days_ago=1)]))
        imp.cmd_import(str(path), days=7)
        self.assertEqual(src.read_buffer(), [])

    def test_odno_soobshchenie_iz_dvuh_istochnikov_ne_dvoitsya(self):
        # Сообщение может прийти и от бота, и из выгрузки. Склейка идёт
        # по номеру сообщения, значит запись должна остаться одна.
        path = write(export([note(text="сделаю в среду", number=7, days_ago=1)]))
        imp.cmd_import(str(path), days=7)
        moment = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp())
        src.append_updates(
            [
                {
                    "update_id": 99,
                    "business_message": {
                        "message_id": 7,
                        "date": moment,
                        "from": {"id": THEM, "first_name": "Маргарита"},
                        "chat": {"id": THEM, "type": "private", "first_name": "Маргарита"},
                        "text": "сделаю в среду",
                    },
                }
            ]
        )
        src.write_inbox({}, {}, datetime.now(timezone.utc))
        text = src.INBOX.read_text(encoding="utf-8")
        self.assertEqual(text.count("сделаю в среду"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
