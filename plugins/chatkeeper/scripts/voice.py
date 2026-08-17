"""Chatkeeper: расшифровка голосовых сообщений.

В рабочих чатах договорённости часто именно в голосовых, поэтому без них
теряется заметная часть смысла. Но расшифровка - вещь необязательная:
если её нет, продукт должен работать дальше, а не падать.

Расшифровка идёт локально, файл никуда не отправляется.

Ищем чем расшифровывать в таком порядке:
  1. наше собственное окружение, если faster-whisper туда доустановили;
  2. соседний whisper-skill, если он у человека стоит;
  3. ничего - тогда голосовые помечаются как нерасшифрованные.

Команды:
    check           - есть ли расшифровка и какая
    file ПУТЬ       - расшифровать один файл
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, die, python_path, utf8_console  # noqa: E402

WORKER = ROOT / "scripts" / "transcribe_worker.py"
MODEL = "small"          # small заметно точнее base на русском, а весит немного
MAX_SECONDS = 300        # длиннее пяти минут - это не договорённость, а лекция
TIMEOUT = 180            # если расшифровка встала, не держим весь разбор


def has_whisper(python: Path) -> bool:
    if not python.exists():
        return False
    result = subprocess.run(
        [str(python), "-c", "import faster_whisper"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def find_engine() -> tuple[Path, str] | None:
    """Возвращает python, которым расшифровывать, и откуда он взялся."""
    ours = python_path()
    if has_whisper(ours):
        return ours, "собственное окружение"

    skill_venv = Path.home() / ".claude" / "skills" / "whisper-skill" / ".venv"
    neighbour = python_path(skill_venv)
    if has_whisper(neighbour):
        return neighbour, "whisper-skill"

    return None


def transcribe(
    audio: Path, model: str = MODEL, python: Path | None = None
) -> str | None:
    """Текст голосового или None, если расшифровать нечем и не получилось.

    Готовый python можно передать снаружи: поиск движка запускает отдельный
    процесс, и делать это на каждое сообщение - пустая трата времени.
    """
    if python is None:
        engine = find_engine()
        if engine is None:
            return None
        python = engine[0]

    try:
        result = subprocess.run(
            [str(python), str(WORKER), str(audio), model],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(f"  расшифровка не уложилась в {TIMEOUT} секунд, пропускаю")
        return None

    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        print(f"  расшифровать не удалось: {detail[-1] if detail else 'причина неясна'}")
        return None

    return result.stdout.strip() or None


# --- команды ------------------------------------------------------------


def cmd_check() -> None:
    engine = find_engine()
    if engine is None:
        print("Расшифровки голосовых нет.")
        print("Голосовые будут помечаться в разборе как нерасшифрованные.")
        print(
            "Чтобы включить: поставить faster-whisper в окружение плагина "
            "или установить whisper-skill."
        )
        sys.exit(1)
    python, source = engine
    print(f"Расшифровка есть: {source}")
    print(f"  python: {python}")
    print(f"  модель: {MODEL}")


def cmd_file(argv: list[str]) -> None:
    if not argv:
        die("Нужно: voice file ПУТЬ_К_ФАЙЛУ")
    audio = Path(argv[0])
    if not audio.exists():
        die(f"Файл не найден: {audio}")
    model = argv[1] if len(argv) > 1 else MODEL

    text = transcribe(audio, model)
    if text is None:
        die("Расшифровать не удалось.")
    print(text)


def main() -> None:
    utf8_console()
    if len(sys.argv) < 2:
        print("Использование: voice [check | file ПУТЬ]")
        sys.exit(2)

    command, argv = sys.argv[1], sys.argv[2:]
    if command == "check":
        cmd_check()
    elif command == "file":
        cmd_file(argv)
    else:
        print("Использование: voice [check | file ПУТЬ]")
        sys.exit(2)


if __name__ == "__main__":
    main()
