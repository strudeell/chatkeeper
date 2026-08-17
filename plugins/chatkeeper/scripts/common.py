"""Chatkeeper: общее для всех скриптов - пути, настройки, аккуратные ошибки.

Здесь разделены две вещи, которые легко перепутать и очень больно перепутать:

  КОД     - папка плагина. При обновлении она заменяется целиком, а старая
            версия удаляется. Писать в неё нельзя ничего.
  ДАННЫЕ  - отдельная папка, которая обновление переживает. Там живут ключи,
            вход в телеграм, память об обещаниях и окружение python.

Если сложить данные рядом с кодом, первое же обновление плагина молча сотрёт
у человека вход в телеграм и все накопленные договорённости.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# --- код ----------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


# --- данные -------------------------------------------------------------


def data_dir() -> Path:
    """Папка, переживающая обновления плагина.

    Claude Code передаёт её в CLAUDE_PLUGIN_DATA. При разработке и при запуске
    скриптов вручную переменной нет - тогда берём то же самое место,
    но с пометкой dev, чтобы не смешивать с рабочими данными.
    """
    for variable in ("CLAUDE_PLUGIN_DATA", "CHATKEEPER_DATA"):
        value = os.environ.get(variable)
        if value:
            return Path(value)
    return Path.home() / ".claude" / "plugins" / "data" / "chatkeeper-dev"


DATA = data_dir()
STATE = DATA / "state"
ENV_FILE = DATA / ".env"
VENV = DATA / ".venv"


def python_path(venv: Path | None = None) -> Path:
    """Путь к python внутри окружения. На Windows и macOS он лежит по-разному."""
    venv = venv or VENV
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


# --- вспомогательное ----------------------------------------------------


def die(message: str) -> None:
    """Останов с человеческим объяснением вместо трассировки."""
    print(f"ОШИБКА: {message}")
    sys.exit(1)


def load_env() -> dict[str, str]:
    """Читает .env. Значения никогда не попадают в вывод.

    Кодировка utf-8-sig, а не utf-8: блокнот в Windows дописывает в начало файла
    невидимую метку, из-за которой первая строка перестаёт распознаваться.
    Кавычки вокруг значения снимаются - их часто прихватывают при копировании.
    """
    if not ENV_FILE.exists():
        die(
            f"Нет файла с настройками ({ENV_FILE}). "
            "Похоже, установка не завершена - запусти установку заново."
        )
    env: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value.strip()
    return env


def utf8_console() -> None:
    """Windows по умолчанию не умеет печатать кириллицу в консоль.

    Оба потока, а не только stdout: русская строка, ушедшая в stderr,
    иначе падает с ошибкой кодировки поверх исходной ошибки.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def read_json(path: Path, default: Any = None) -> Any:
    """Читает файл памяти. Кодировка utf-8-sig - та же ловушка, что и в .env:
    редактор в Windows дописывает невидимую метку, и обычный разбор JSON падает.

    Повреждённый файл - это не повод показать человеку трассировку.
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as error:
        die(
            f"Файл памяти повреждён и не читается: {path}\n"
            f"Строка {error.lineno}. Скорее всего его правили руками.\n"
            "Проще всего удалить этот файл - он соберётся заново при следующем разборе."
        )
    return default


def write_json(path: Path, data: Any) -> None:
    """Пишет файл памяти: без метки в начале, с отступами, по-русски читаемо."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
