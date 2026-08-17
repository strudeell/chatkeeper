"""Симуляция недели на выдуманных чатах. Запуск: python test_week.py

Отвечает на вопрос заказчицы: «через неделю всё действительно настроится само?»

Проверяется не отдельная функция, а поведение системы во времени. Семь дней,
чаты появляются в разные дни, человек отвечает словами, как в жизни. Важно,
что вопросы задаются по одному и ровно один раз на чат: продукт, который
переспрашивает, раздражает сильнее, чем продукт, который не спрашивает вовсе.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from testenv import isolate  # noqa: E402

# Своя папка данных - до импорта: модули продукта закрепляют пути при загрузке.
TEMP = isolate("chatkeeper-week-test-")

import setup  # noqa: E402
import source_bot as src  # noqa: E402

ME = 500

# Кто пишет, с какого дня недели начинает, и что человек про этот чат ответит.
# «Мама» появляется на четвёртый день - проверяем, что новый чат не теряется
# и не остаётся без вопроса.
PEOPLE = [
    {"id": 601, "name": "Маргарита", "since": 0, "answer": "рабочий: запуск"},
    {"id": 602, "name": "Костя", "since": 0, "answer": "рабочий"},
    {"id": 603, "name": "Лена", "since": 1, "answer": "личный"},
    {"id": 604, "name": "Мама", "since": 3, "answer": "не читать"},
    {"id": 605, "name": "Подрядчик", "since": 5, "answer": "рабочий: ремонт"},
]


def message(person: dict, day: int, number: int, text: str) -> dict:
    moment = datetime.now(timezone.utc) - timedelta(days=7 - day)
    return {
        "update_id": day * 100 + number,
        "business_message": {
            "message_id": number,
            "date": int(moment.timestamp()),
            "from": {"id": person["id"], "first_name": person["name"]},
            "chat": {"id": person["id"], "type": "private", "first_name": person["name"]},
            "text": text,
        },
    }


def answer_of(chat_id: int) -> str:
    return next(p["answer"] for p in PEOPLE if p["id"] == chat_id)


def run_answer(key: str, value: str) -> None:
    """Ответ так, как его подаёт Клод: через точку входа, а не внутренним вызовом."""
    saved = sys.argv
    try:
        sys.argv = ["setup.py", "answer", key, value]
        setup.main()
    finally:
        sys.argv = saved


class NedelyaZhizni(unittest.TestCase):
    """Один тест, но длинный: проверяется именно последовательность дней."""

    def test_za_nedelyu_karta_chatov_sobiraetsya_sama(self):
        # --- день нулевой: установка -----------------------------------
        setup.save_runtime({"stop_list": [], "chats": []})
        setup.ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        setup.ENV_FILE.write_text(
            "TELEGRAM_BOT_TOKEN=x\nTELEGRAM_OWNER_ID=500\n", encoding="utf-8"
        )
        for key, value in (
            ("consent", True),
            ("digest_time", "09:00"),
            ("calendar", True),
            ("voice", False),
        ):
            runtime = setup.load_runtime()
            runtime.setdefault("setup", {}).setdefault("answers", {})[key] = value
            setup.save_runtime(runtime)

        ready, reason = setup.setup_ready()
        self.assertTrue(ready, reason)

        runtime = setup.load_runtime()
        setup.schedule_review(runtime)
        setup.save_runtime(runtime)

        # Неделя ещё не прошла - система не должна лезть с вопросами вовсе.
        self.assertFalse(setup.review_due(setup.load_runtime()))

        # --- семь дней подряд ------------------------------------------
        for day in range(7):
            src.BUFFER.unlink(missing_ok=True)
            src.INBOX.unlink(missing_ok=True)

            forbidden = src.forbidden_chats()
            portion = []
            for number, person in enumerate(PEOPLE, start=1):
                if day < person["since"]:
                    continue
                update = message(person, day, day * 10 + number, f"сообщение дня {day}")
                if not src.is_forbidden(update, forbidden):
                    portion.append(update)
            src.append_updates(portion)

            src.write_inbox(
                {"connection": {"user_id": ME}},
                {},
                datetime.now(timezone.utc),
            )

            # Всю неделю система молчит про чаты: она их только копит.
            self.assertFalse(
                setup.review_due(setup.load_runtime()),
                f"на {day} день система полезла с вопросами раньше срока",
            )
            src.cmd_done()

        # --- неделя прошла ---------------------------------------------
        runtime = setup.load_runtime()
        runtime["setup"]["review_after"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        setup.save_runtime(runtime)

        self.assertTrue(setup.review_due(setup.load_runtime()))

        # Список показывается целиком, все пятеро в нём.
        listing = setup.review_list(setup.load_runtime())
        for person in PEOPLE:
            self.assertIn(person["name"], listing)

        # Человек называет рабочие, про запретный говорит отдельно.
        runtime = setup.load_runtime()
        setup.answer_chat(runtime, 604, "не читать")
        setup.save_runtime(runtime)
        run_answer("chats-work", "601,602,605")

        # --- что получилось -------------------------------------------
        runtime = setup.load_runtime()
        by_id = {int(c["id"]): c for c in runtime.get("chats", [])}

        # 1. Рабочее отделено от личного одним ответом.
        self.assertEqual(by_id[601]["kind"], "work")
        self.assertEqual(by_id[602]["kind"], "work")
        self.assertEqual(by_id[605]["kind"], "work")
        self.assertEqual(by_id[603]["kind"], "personal")

        # 2. Запретный чат исчез из работы и попал в стоп-список.
        self.assertNotIn(604, by_id)
        self.assertIn(604, runtime["stop_list"])

        # 3. Чат, появившийся на шестой день, в списке был и не потерялся.
        self.assertIn(605, by_id)

        # 4. Больше система не спрашивает: карта собрана.
        self.assertFalse(setup.review_due(runtime))
        self.assertEqual(setup.unknown_chats(runtime), [])
        self.assertEqual(setup.pending(runtime), [])

    def test_posle_zapreta_soobshcheniya_ne_lozhatsya_na_disk(self):
        """Отдельно и строго: закрытый чат не должен попадать даже в буфер.

        Тест самостоятельный: unittest идёт по алфавиту, и опираться на то,
        что недельный прогон уже случился, нельзя - это вернуло бы зависимость
        между тестами, из-за которой падение объясняется чужой причиной.
        """
        setup.save_runtime(
            {"stop_list": [604], "chats": [], "setup": {"answers": {"consent": True}}}
        )
        runtime = setup.load_runtime()
        self.assertIn(604, runtime.get("stop_list", []))

        src.BUFFER.unlink(missing_ok=True)
        forbidden = src.forbidden_chats()
        mama = next(p for p in PEOPLE if p["id"] == 604)
        update = message(mama, 8, 999, "секрет")
        self.assertTrue(src.is_forbidden(update, forbidden))

        src.append_updates([u for u in [update] if not src.is_forbidden(u, forbidden)])
        self.assertEqual(src.read_buffer(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
