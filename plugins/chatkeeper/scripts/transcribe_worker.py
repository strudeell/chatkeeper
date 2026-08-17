"""Расшифровка одного аудиофайла. Запускается python-ом из whisper-skill,
у которого установлен faster-whisper - в наше окружение эта тяжёлая
зависимость не тащится.

Вызов:  python transcribe_worker.py <путь к файлу> [модель]
Печатает распознанный текст в stdout и ничего больше.
"""

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

if len(sys.argv) < 2:
    print("Нужен путь к аудиофайлу", file=sys.stderr)
    sys.exit(2)

audio = sys.argv[1]
model_name = sys.argv[2] if len(sys.argv) > 2 else "small"

try:
    from faster_whisper import WhisperModel
except ImportError:
    print("faster-whisper не установлен в этом окружении", file=sys.stderr)
    sys.exit(3)

# int8 на процессоре: быстро и по качеству для речи в чатах достаточно
model = WhisperModel(model_name, device="cpu", compute_type="int8")
segments, _ = model.transcribe(audio, vad_filter=True)
print(" ".join(segment.text.strip() for segment in segments).strip())
