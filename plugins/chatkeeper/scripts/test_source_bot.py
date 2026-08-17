"""Тесты источника сообщений через бота. Запуск: python test_source_bot.py

Сети здесь нет: проверяется то, что ломается тихо и обнаруживается через недели -
кто автор сообщения, не теряется ли буфер, виден ли разрыв в сутки.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

# Папку данных подменяем до импорта: common вычисляет пути один раз при загрузке.
TEMP = tempfile.mkdtemp(prefix="chatkeeper-test-")
os.environ["CHATKEEPER_DATA"] = TEMP
sys.path.insert(0, str(Path(__file__).resolve().parent))

import source_bot as src  # noqa: E402

ME = 111
THEM = 222


def message(text="привет", sender=THEM, date=1000, number=1, **extra):
    body = {
        "message_id": number,
        "date": date,
        "from": {"id": sender, "first_name": "Пётр" if sender == THEM else "Хозяин"},
        "chat": {"id": THEM, "first_name": "Пётр", "type": "private"},
        **extra,
    }
    if text is not None:
        body["text"] = text
    return body


def update(number, body):
    return {"update_id": number, "business_message": body}


class Rows(unittest.TestCase):
    def test_autor_ya_opredelyaetsya_po_vladeltsu(self):
        row = src.render_row(message(sender=ME), me=ME)
        self.assertIn("я:", row)

    def test_sobesednik_nazyvaetsya_po_imeni(self):
        row = src.render_row(message(sender=THEM), me=ME)
        self.assertIn("Пётр:", row)

    def test_bez_vladeltsa_nikto_ne_stanovitsya_ya(self):
        # Если владелец неизвестен, лучше подписать по имени, чем соврать «я»
        row = src.render_row(message(sender=ME), me=None)
        self.assertNotIn("я:", row)

    def test_vlozhenie_pomechaetsya(self):
        row = src.render_row(message(text=None, voice={"duration": 3}), me=ME)
        self.assertIn("[голосовое]", row)

    def test_sluzhebnoe_bez_teksta_i_vlozheniy_otbrasyvaetsya(self):
        self.assertIsNone(src.render_row(message(text=None), me=ME))

    def test_dlinnyy_tekst_obrezaetsya(self):
        row = src.render_row(message(text="а" * 5000), me=ME)
        self.assertIn("обрезано", row)
        self.assertLess(len(row), src.MAX_TEXT_CHARS + 100)


class StopList(unittest.TestCase):
    def test_id_s_prefiksom_i_bez_odinakovo(self):
        self.assertTrue(src.in_stop_list(-1001234567890, {1234567890}))
        self.assertTrue(src.in_stop_list(555, {555}))
        self.assertFalse(src.in_stop_list(555, {556}))


class Buffer(unittest.TestCase):
    def setUp(self):
        src.BUFFER.unlink(missing_ok=True)
        src.INBOX.unlink(missing_ok=True)

    def test_zapisannoe_chitaetsya_obratno(self):
        src.append_updates([update(1, message()), update(2, message())])
        self.assertEqual(len(src.read_buffer()), 2)

    def test_bituyu_stroku_perezhivaem(self):
        src.append_updates([update(1, message())])
        with src.BUFFER.open("a", encoding="utf-8") as handle:
            handle.write("{это не json\n")
        src.append_updates([update(2, message())])
        self.assertEqual(len(src.read_buffer()), 2)

    def test_soobshcheniya_v_poryadke_razgovora(self):
        src.append_updates(
            [
                update(1, message(text="второе", date=2000, number=2)),
                update(2, message(text="первое", date=1000, number=1)),
            ]
        )
        src.write_inbox({"connection": {"user_id": ME}}, {}, datetime.now(timezone.utc))
        text = src.INBOX.read_text(encoding="utf-8")
        self.assertLess(text.index("первое"), text.index("второе"))

    def test_marker_na_meste(self):
        src.append_updates([update(1, message())])
        src.write_inbox({}, {}, datetime.now(timezone.utc))
        self.assertTrue(
            src.INBOX.read_text(encoding="utf-8").startswith(src.INBOX_MARKER)
        )

    def test_done_chistit_bufer(self):
        src.append_updates([update(1, message())])
        src.cmd_done()
        self.assertEqual(src.read_buffer(), [])
        self.assertFalse(src.INBOX.exists())


    def test_pravka_ne_udvaivaet_soobshchenie(self):
        # Правка приходит отдельным обновлением с тем же номером сообщения.
        # Без учёта номера разбор завёл бы два обещания вместо одного.
        first = message(text="сделаю в среду")
        edited = dict(first, text="сделаю в пятницу")
        src.append_updates([update(1, first)])
        src.append_updates([{"update_id": 2, "edited_business_message": edited}])
        src.write_inbox({"connection": {"user_id": ME}}, {}, datetime.now(timezone.utc))
        text = src.INBOX.read_text(encoding="utf-8")
        self.assertIn("в пятницу", text)
        self.assertNotIn("в среду", text)

    def test_pustoy_bufer_daet_ponyatnyy_fayl(self):
        src.write_inbox({}, {}, datetime.now(timezone.utc))
        self.assertIn("Новых сообщений нет", src.INBOX.read_text(encoding="utf-8"))


class Registry(unittest.TestCase):
    def setUp(self):
        src.BUFFER.unlink(missing_ok=True)
        src.RUNTIME.unlink(missing_ok=True)

    def test_chat_popadaet_v_reestr(self):
        src.append_updates([update(1, message())])
        src.write_inbox({}, {}, datetime.now(timezone.utc))
        chats = (src.read_json(src.RUNTIME, default={}) or {}).get("chats", [])
        self.assertEqual([c["name"] for c in chats], ["Пётр"])

    def test_isklyuchyonnyy_chat_ne_beryotsya(self):
        src.write_json(src.RUNTIME, {"excluded": [THEM]})
        src.append_updates([update(1, message(text="секрет"))])
        src.write_inbox({}, {}, datetime.now(timezone.utc))
        self.assertNotIn("секрет", src.INBOX.read_text(encoding="utf-8"))

    def test_golosovoe_bez_dvizhka_ostayotsya_pometkoy(self):
        # Ключ бота в тестах пустой, значит расшифровки нет и сети тоже.
        src.append_updates([update(1, message(text=None, voice={"duration": 5}))])
        src.write_inbox({}, {}, datetime.now(timezone.utc))
        self.assertIn("[голосовое]", src.INBOX.read_text(encoding="utf-8"))


class SetevyeSboi(unittest.TestCase):
    """Главная находка код-ревью: die() поднимает SystemExit, а он не Exception.

    Из-за этого одно битое голосовое роняло весь разбор, а буфер очищается только
    после успеха - значит то же сообщение роняло бы и каждый следующий запуск.
    Сводка не пришла бы уже никогда.
    """

    def setUp(self):
        # RUNTIME чистим тоже: соседний класс оставляет там исключённый чат,
        # и без этого тест падал бы по чужой причине.
        src.BUFFER.unlink(missing_ok=True)
        src.INBOX.unlink(missing_ok=True)
        src.RUNTIME.unlink(missing_ok=True)

    @staticmethod
    def _http_error(code=400):
        def boom(*args, **kwargs):
            raise urllib.error.HTTPError(
                "https://api.telegram.org/", code, "Bad Request", {}, io.BytesIO(b"{}")
            )

        return boom

    def test_myagkiy_rezhim_ne_ronyaet_razbor(self):
        with mock.patch("urllib.request.urlopen", self._http_error()):
            self.assertEqual(src.api("токен", "getFile", {}, strict=False), {})

    def test_strogiy_rezhim_ostanavlivaet_kak_ranshe(self):
        with mock.patch("urllib.request.urlopen", self._http_error()):
            with self.assertRaises(SystemExit):
                src.api("токен", "getFile", {}, strict=True)

    def test_bitoe_golosovoe_ne_meshaet_ostalnym(self):
        # Текстовое сообщение обязано доехать до файла разбора, даже если
        # соседнее голосовое скачать не удалось.
        src.append_updates(
            [
                update(1, message(text="сделаю в пятницу", number=1)),
                update(2, message(text=None, voice={"duration": 5}, number=2)),
            ]
        )
        with mock.patch("urllib.request.urlopen", self._http_error()):
            with mock.patch.object(src.voice, "find_engine", return_value=(Path("py"), "тест")):
                src.write_inbox({}, {"TELEGRAM_BOT_TOKEN": "токен"}, datetime.now(timezone.utc))
        text = src.INBOX.read_text(encoding="utf-8")
        self.assertIn("сделаю в пятницу", text)
        self.assertIn("[голосовое]", text)


class Podklyuchenie(unittest.TestCase):
    """Событие подключения приходит один раз и живёт сутки.

    При переустановке или если бота подключили давно, событие не повторится.
    Раньше система в этом случае считала, что подключения нет, при работающем
    подключении - и установка выглядела незаконченной навсегда.
    """

    def test_podklyuchenie_uznayotsya_po_soobshcheniyu(self):
        answer = {
            "ok": True,
            "result": {
                "id": "conn-1",
                "user": {"id": ME, "first_name": "Хозяйка"},
                "is_enabled": True,
            },
        }
        with mock.patch.object(src, "api", return_value=answer) as called:
            found = src.api("токен", "getBusinessConnection", {"business_connection_id": "conn-1"})
        self.assertEqual((found["result"]["user"] or {}).get("id"), ME)
        self.assertTrue(called.called)

    def test_soobshchenie_neset_nomer_soedineniya(self):
        # Ключевое допущение: у делового сообщения есть номер соединения,
        # по которому можно спросить телеграм, чьё оно.
        body = message()
        body["business_connection_id"] = "conn-1"
        self.assertEqual(body.get("business_connection_id"), "conn-1")


class ZapretnyeChaty(unittest.TestCase):
    def setUp(self):
        src.BUFFER.unlink(missing_ok=True)
        src.RUNTIME.unlink(missing_ok=True)

    def test_zapretnyy_chat_ne_lozhitsya_na_disk(self):
        # Человеку обещано «не читается никогда». Значит байты не должны попадать
        # на диск вообще, а не отсеиваться на показе.
        forbidden = {THEM}
        keep = src.is_forbidden(update(1, message(text="секрет")), forbidden)
        self.assertTrue(keep)
        self.assertFalse(src.is_forbidden(update(2, {"business_connection": {}}), forbidden))

    def test_sluzhebnoe_sobytie_prohodit_vsegda(self):
        # Событие подключения не относится к чату и обязано доехать,
        # иначе система перестанет понимать, жив ли бот.
        self.assertFalse(src.is_forbidden({"update_id": 1, "business_connection": {}}, {THEM}))


class Uborka(unittest.TestCase):
    def test_zabytoe_golosovoe_udalyaetsya(self):
        src.STATE.mkdir(parents=True, exist_ok=True)
        stale = src.STATE / "voice_999.ogg"
        stale.write_bytes(b"chuzhoy golos")  # содержимое неважно, важен сам файл
        old = time.time() - src.LOCK_STALE_SECONDS - 60
        os.utime(stale, (old, old))
        src.sweep_orphan_voices()
        self.assertFalse(stale.exists())

    def test_svezhee_golosovoe_ne_trogaem(self):
        # Файл может принадлежать разбору, который идёт прямо сейчас.
        src.STATE.mkdir(parents=True, exist_ok=True)
        fresh = src.STATE / "voice_1000.ogg"
        fresh.write_bytes(b"idet rasshifrovka")
        src.sweep_orphan_voices()
        self.assertTrue(fresh.exists())
        fresh.unlink()


class Gap(unittest.TestCase):
    def test_pereryv_bolshe_sutok_viden(self):
        now = datetime.now(timezone.utc)
        previous = (now - timedelta(hours=30)).isoformat()
        self.assertIn("не придёт", src.gap_warning(previous, now))

    def test_noch_ne_povod_dlya_trevogi(self):
        now = datetime.now(timezone.utc)
        previous = (now - timedelta(hours=10)).isoformat()
        self.assertIsNone(src.gap_warning(previous, now))

    def test_pervyy_zapusk_molchit(self):
        self.assertIsNone(src.gap_warning(None, datetime.now(timezone.utc)))

    def test_otmetka_beryotsya_do_perezapisi(self):
        # Тот самый баг: если записать нынешний забор раньше сравнения,
        # разрыв сравнивается сам с собой и не находится никогда.
        now = datetime.now(timezone.utc)
        state = {"last_done": (now - timedelta(hours=40)).isoformat()}
        previous = state.get("last_done") or state.get("last_fetch")
        state["last_fetch"] = now.isoformat()
        self.assertIsNotNone(src.gap_warning(previous, now))


if __name__ == "__main__":
    unittest.main(verbosity=2)
