"""Тесты поставки: то, что ломается не у нас, а у чужого человека при установке.

Остальные тесты проверяют поведение кода. Этот проверяет саму коробку: раскладку
папок, каталог маркетплейса и образец настроек. Такие поломки не видны в работе
у автора - у него всё уже установлено и настроено, - и проявляются только на чужой
машине, где ставится с нуля. Дешевле поймать здесь.

Отдельно проверяется, что в .env.example нет значений. Файл с ключами лежит рядом
с образцом и называется почти так же, поэтому перепутать их - вопрос одной опечатки,
а ценой будет токен бота в публичном репозитории.

Запуск: python test_package.py
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]      # папка плагина
REPO = PLUGIN.parents[1]                           # корень репозитория


def frontmatter(path: Path) -> dict[str, str]:
    """Разбирает шапку скилла. Полноценный YAML тут не нужен: поля плоские."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    _, _, rest = text.partition("---")
    head, _, _ = rest.partition("\n---")
    fields = {}
    for line in head.splitlines():
        key, sep, value = line.partition(":")
        if sep and not key.startswith(" "):
            fields[key.strip()] = value.strip()
    return fields


class Raskladka(unittest.TestCase):
    """Claude Code читает скиллы только по документированным путям."""

    def test_oba_skilla_na_meste(self):
        for name in ("chatkeeper", "install"):
            skill = PLUGIN / "skills" / name / "SKILL.md"
            self.assertTrue(skill.exists(), f"нет скилла {name}: {skill}")

    def test_u_skilla_est_opisanie(self):
        # Без description модель не знает, когда скилл применять, и он молчит.
        for name in ("chatkeeper", "install"):
            fields = frontmatter(PLUGIN / "skills" / name / "SKILL.md")
            self.assertTrue(fields.get("description"), f"у скилла {name} нет description")

    def test_skilly_ne_smeshany_so_starym_formatom(self):
        # Раскладки skills/ и корневой SKILL.md - взаимоисключающие. Если появятся
        # обе, часть скиллов просто не загрузится, и это не будет видно по ошибке.
        self.assertFalse((PLUGIN / "SKILL.md").exists(), "корневой SKILL.md лишний")

    def test_scenariy_ustanovki_na_meste(self):
        # На него ссылается скилл install и оба запускателя.
        self.assertTrue((PLUGIN / "install.md").exists())


class Katalog(unittest.TestCase):
    """Первая команда чужого человека - добавить маркетплейс. Она читает этот файл."""

    def setUp(self):
        self.market = json.loads(
            (REPO / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
        )
        self.manifest = json.loads(
            (PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )

    def test_obyazatelnye_polya_katologa(self):
        self.assertTrue(self.market.get("name"))
        self.assertTrue((self.market.get("owner") or {}).get("name"))
        self.assertTrue(self.market.get("plugins"))

    def test_put_k_pluginu_sushchestvuet(self):
        for entry in self.market["plugins"]:
            self.assertTrue(entry.get("name"))
            source = entry["source"]
            self.assertIsInstance(source, str, "источник-объект здесь не используется")
            self.assertTrue((REPO / source).is_dir(), f"нет папки плагина: {source}")

    def test_imena_v_kataloge_i_manifeste_sovpadayut(self):
        # Ставят командой install <имя плагина>@<имя маркетплейса>. Разъехались
        # имена - команда из README не сработает, а ошибка будет невнятной.
        self.assertIn(self.manifest["name"], [p["name"] for p in self.market["plugins"]])

    def test_versiya_est(self):
        # Без смены версии обновление до людей не доезжает: Claude Code считает,
        # что у них уже актуальная. Поле должно существовать, чтобы было что поднять.
        self.assertTrue(self.manifest.get("version"))


class Sekrety(unittest.TestCase):
    """Самое дорогое, что может утечь, - ключ бота."""

    def test_v_obraztse_net_znacheniy(self):
        for line in (PLUGIN / ".env.example").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            # Исключение одно: выбор источника - это настройка, а не секрет.
            if key.strip() == "CHATKEEPER_SOURCE":
                continue
            self.assertEqual(value.strip(), "", f"в образце заполнено значение: {key}")

    def test_nastoyashchiy_env_ne_lezhit_v_postavke(self):
        self.assertFalse((PLUGIN / ".env").exists(), "файл .env оказался в папке плагина")


class Zapuskateli(unittest.TestCase):
    def test_konets_stroki_v_shell_skripte(self):
        # \r\n в первой строке ломает запуск на macOS: интерпретатор ищет
        # программу с невидимым символом в имени и не находит.
        data = (PLUGIN / "bin" / "chatkeeper").read_bytes()
        self.assertNotIn(b"\r\n", data, "в запускателе для macOS концы строк Windows")
        self.assertTrue(data.startswith(b"#!"), "нет строки запуска в начале файла")

    def test_batnik_bez_kirillitsy(self):
        # cmd.exe читает батник в системной кодировке и ломается на кириллице.
        data = (PLUGIN / "bin" / "chatkeeper.cmd").read_bytes()
        try:
            data.decode("ascii")
        except UnicodeDecodeError as error:
            self.fail(f"в батнике не-ASCII символ: {error}")


if __name__ == "__main__":
    unittest.main()
