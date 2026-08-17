"""Тесты мастера установки. Запуск: python test_setup_wizard.py

Проверяется то, ради чего мастер и заводился: вопросы задаются по одному
и в порядке, ответы переживают перезапуск, а незаконченная установка
физически не пускает разбор. Последнее - главное: раньше это была
рекомендация в тексте, и её обошли.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from testenv import isolate  # noqa: E402

# Своя папка данных - обязательно до импорта setup: он забирает пути к себе
# при загрузке, и позже подменить их уже нечем.
TEMP = isolate("chatkeeper-wizard-test-")

import setup  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent


def reset(**answers):
    runtime = {"stop_list": [], "chats": []}
    if answers:
        runtime["setup"] = {"answers": dict(answers)}
    setup.save_runtime(runtime)


def run(*args):
    """Запуск как из жизни: отдельным процессом, через точку входа."""
    env = dict(os.environ, CHATKEEPER_DATA=TEMP, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPTS / args[0]), *args[1:]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


class Poryadok(unittest.TestCase):
    def test_pervym_sprashivaem_soglasie(self):
        reset()
        self.assertEqual(setup.pending(setup.load_runtime())[0]["id"], "consent")

    def test_otvechennyy_vopros_bolshe_ne_zadayotsya(self):
        reset(consent=True)
        ids = [q["id"] for q in setup.pending(setup.load_runtime())]
        self.assertNotIn("consent", ids)
        self.assertEqual(ids[0], "digest_time")

    def test_vopros_pro_golosovye_bez_dvizhka_ne_zadayotsya(self):
        reset(consent=True, digest_time="09:00", calendar=True)
        # Движка расшифровки в тестовом окружении нет, значит вопрос лишний.
        if not setup.voice_available():
            self.assertEqual(setup.pending(setup.load_runtime()), [])


class Otvety(unittest.TestCase):
    def test_vremya_ponimaetsya_raznymi_sposobami(self):
        self.assertEqual(setup.normalize_time("в 9"), "09:00")
        self.assertEqual(setup.normalize_time("9.30"), "09:30")
        self.assertEqual(setup.normalize_time("21:05"), "21:05")

    def test_da_i_net_ponimayutsya_slovami(self):
        reset()
        run("setup.py", "answer", "consent", "давай")
        self.assertIs(setup.answers(setup.load_runtime()).get("consent"), True)
        run("setup.py", "answer", "calendar", "не надо")
        self.assertIs(setup.answers(setup.load_runtime()).get("calendar"), False)

    def test_myamlyanie_ne_prinimaetsya_za_otvet(self):
        reset()
        result = run("setup.py", "answer", "consent", "ну не знаю")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("consent", setup.answers(setup.load_runtime()))

    def test_vremya_popadaet_v_obshcheye_pole(self):
        reset(consent=True)
        run("setup.py", "answer", "digest_time", "в 8")
        self.assertEqual(setup.load_runtime().get("digest_time"), "08:00")


class Zapret(unittest.TestCase):
    def test_nezakonchennaya_ustanovka_ne_puskaet_razbor(self):
        reset()
        result = run("collect.py", "fetch")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Разбор не начат", result.stdout)

    def test_otkaz_ot_soglasiya_ostanavlivaet_nasovsem(self):
        reset(consent=False, digest_time="09:00", calendar=True)
        ready, reason = setup.setup_ready()
        self.assertFalse(ready)
        self.assertIn("не разрешён", reason)

    def test_status_rabotaet_i_vo_vremya_ustanovki(self):
        # Иначе установку не пройти: подключение бота проверяется именно ей.
        reset()
        result = run("collect.py", "status")
        self.assertEqual(result.returncode, 0)

    def test_gotovaya_ustanovka_puskaet(self):
        reset(consent=True, digest_time="09:00", calendar=True, voice=False)
        setup.ENV_FILE.write_text(
            "TELEGRAM_BOT_TOKEN=x\nTELEGRAM_OWNER_ID=1\n", encoding="utf-8"
        )
        ready, reason = setup.setup_ready()
        self.assertTrue(ready, reason)


class Master(unittest.TestCase):
    def test_master_pokazyvaet_odin_vopros(self):
        reset()
        result = run("setup.py", "wizard")
        self.assertIn("ВОПРОС consent", result.stdout)
        self.assertEqual(result.stdout.count("ВОПРОС"), 1)

    def test_bez_klyucha_bota_master_vedyot_dalshe_a_ne_rapotuet(self):
        # Находка прогона с нуля: мастер знал только про вопросы и говорил ГОТОВО
        # при отсутствующем ключе бота. Сессия шла разбирать и упиралась в отказ,
        # не понимая, чего от неё хотят.
        reset(consent=True, digest_time="09:00", calendar=True, voice=True)
        setup.ENV_FILE.write_text("TELEGRAM_BOT_TOKEN=\n", encoding="utf-8")
        result = run("setup.py", "wizard")
        self.assertIn("ШАГ bot_token", result.stdout)
        self.assertNotIn("ГОТОВО", result.stdout)

    def test_master_soobshchaet_o_zavershenii(self):
        reset(consent=True, digest_time="09:00", calendar=True, voice=True)
        setup.ENV_FILE.write_text(
            "TELEGRAM_BOT_TOKEN=x\nTELEGRAM_OWNER_ID=1\n", encoding="utf-8"
        )
        setup.write_json(
            setup.STATE / "bot_state.json",
            {"connection": {"id": "c", "user_id": 1, "enabled": True}},
        )
        result = run("setup.py", "wizard")
        self.assertIn("ГОТОВО", result.stdout)

    def test_master_uvazhaet_otkaz(self):
        reset(consent=False)
        result = run("setup.py", "wizard")
        self.assertIn("ОТКАЗ", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
