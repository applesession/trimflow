# workspace-gojo-satoru

Локальный Python-скрипт для сборки аниме-компиляций из эпизодов:
- получает серии из magnet-ссылки или из локальной папки;
- может автоматически находить свежие ongoing-релизы из AniLiberty и добавлять их в очередь;
- запрашивает у AniSkip интервалы `op/ed`;
- вырезает пропуски через `ffmpeg`;
- склеивает итоговый файл;
- накладывает watermark;
- сохраняет артефакты в `output/` и загружает их в S3.

## Структура

- `main.py` — точка входа: загрузка конфига, preflight-проверки, запуск `jobs`
- `scripts/discover_jobs.py` — discovery свежих релизов AniLiberty и автогенерация queue
- `scripts/cron_run.py` — cron-friendly discovery + processing с lock и runtime-логом
- `scripts/telegram_bot.py` — Telegram operator layer через long polling
- `lib/` — основная логика проекта по модулям
- `lib/pipeline.py` — orchestration обработки одного job
- `lib/media.py` — `ffmpeg/ffprobe`, сегменты, финальный рендер
- `lib/aniskip.py` — запросы к AniSkip и сводка по вырезанию
- `lib/detector.py` — локальный audio-fingerprint detector для fallback по `OP/ED`
- `lib/discovery.py` — поиск и фильтрация файлов эпизодов
- `lib/storage.py` — загрузка в S3
- `lib/validation.py` — preflight-проверки и работа с `temp/`
- `lib/helpers.py` / `lib/config.py` / `lib/constants.py` — общие утилиты, конфиг и константы
- `config.json` — дефолтные настройки и automation-настройки
- `jobs.json` — активная очередь задач
- `completed_jobs.json` — архив успешно завершённых задач
- `state.json` — состояние discovery, дедупликация релизов и skip-метаданные
- `telegram_state.json` — offset и runtime-state Telegram-бота
- `.env` — S3-переменные окружения
- `requirements.txt` — Python-зависимости
- `assets/watermark.png` — watermark для итогового видео
- `input/` — локальные исходники, если источник `local`
- `downloads/` — временные загрузки из magnet
- `temp/` — промежуточные сегменты и concat-файлы
- `output/` — готовые результаты

## Что нужно для работы

Python-пакеты:

```bash
pip install -r requirements.txt
```

Системные утилиты должны быть доступны в `PATH`:
- `aria2c`
- `ffmpeg`
- `ffprobe`

## Переменные окружения

В `.env` должны быть заданы:

- `S3_ENDPOINT`
- `S3_ACCESS_KEY_ID`
- `S3_SECRET_ACCESS_KEY`
- `S3_REGION`
- `S3_BUCKET_NAME`
- `VK_ACCESS_TOKEN`
- `VK_GROUP_ID`
- `VK_API_VERSION`
- `TELEGRAM_BOT_TOKEN` — токен бота для уведомлений и команд
- `TELEGRAM_ALLOWED_CHAT_IDS` — список разрешённых `chat_id` через запятую
- `TELEGRAM_STATE_PATH` — опционально, кастомный путь для `telegram_state.json`
- `TELEGRAM_FORCE_IPV4` — опционально, по умолчанию `true`; форсирует IPv4 для Telegram API на VPS с проблемным IPv6

## Настройка задач

Статические настройки описываются в `config.json`, а сами задачи лежат в `jobs.json`.

Поддерживаются два типа источника:

1. `magnet` — скачать релиз через `aria2c`
2. `local` — взять уже существующие файлы из `input/` или другой папки

В `defaults` лежат общие настройки:
- `output_dir`
- `watermark_path`
- `skip_types`
- `cleanup`
- `encoding`
- `timing_detection`
- `timing_providers`
- `delivery`

В `automation` лежат настройки discovery:
- `enabled`
- `provider`
- `jobs_path`
- `completed_jobs_path`
- `state_path`
- `poll_limit`
- `download_root`
- `default_source_type`

Каждый job в `jobs.json` обычно содержит:
- `title`
- `mal_id` (опционально, нужен только если включён AniSkip)
- `season`
- `episodes_range`
- `source`

`episodes_range` теперь реально участвует в отборе серий. Поддерживаются форматы:
- `001-007`
- `003`
- `001,003,005-007`

`timing_detection` — opt-in fallback-детектор:
- `enabled`
- `mode`
- `search_head_seconds`
- `search_tail_seconds`
- `min_support_episodes`
- `frame_step_seconds`
- `min_segment_seconds`
- `max_segment_seconds`
- `auto_cut_min_confidence`

`encoding` может отдельно управлять финальным и промежуточным encode:
- `video_codec` / `preset` / `cq` — финальный рендер
- `segment_video_codec` / `segment_preset` / `segment_cq` / `segment_pixel_format` — нарезка сегментов перед склейкой

`delivery` управляет выходными каналами:
- `s3_enabled`
- `s3_upload_video`
- `s3_upload_timestamps`
- `s3_upload_manifest`
- `vk_enabled`

## Запуск

```bash
python main.py
```

## Discovery свежих релизов

```bash
python scripts/discover_jobs.py
```

Что делает discovery:
- опрашивает AniLiberty API;
- находит свежие ongoing-релизы;
- выбирает конкретный torrent-вариант релиза, а не общий release-level `episodes`;
- приоритизирует `AVC/x264`, а если он недоступен, делает fallback на `HEVC/x265`;
- создаёт новые job'ы в `jobs.json` или расширяет `episodes_range` у существующих;
- записывает уже увиденные эпизоды и skip-причины в `state.json`.

Повторный запуск идемпотентен: один и тот же эпизод не должен добавляться дважды.

## Cron-friendly запуск

```bash
python scripts/cron_run.py
```

Что делает runner:
- пытается взять файловый lock;
- если другой запуск ещё идёт, пишет `already_running` в лог и завершается без ошибки;
- выполняет discovery;
- сразу после этого обрабатывает всю текущую очередь job'ов;
- пишет сводку в `stdout` и в `logs/cron.log`.

Runtime-файлы:
- `.runtime/cron.lock` — lock активного запуска
- `logs/cron.log` — append-only лог раннера

Рекомендуемая команда для crontab:

```bash
*/10 * * * * cd /path/to/workspace-gojo-satoru && python scripts/cron_run.py
```

## Telegram Bot

```bash
python scripts/telegram_bot.py
```

Что делает бот:
- работает отдельным long-running process рядом с `cron_run.py`;
- читает `jobs.json` и `state.json` для операторских команд;
- отправляет уведомления о новых job'ах, ошибках и успешной обработке;
- принимает ручное добавление job в очередь.

Поддерживаемые команды:
- `/start` — краткая справка
- `/help` — формат команд
- `/status` — lock, количество job'ов, время последнего discovery
- `/jobs` — последние job'ы из очереди
- `/add Название : 001-003 : magnet:?xt=... : 2` — ручное добавление job

Замечания:
- бот использует `long polling`, не webhook;
- доступ ограничен только чатами из `TELEGRAM_ALLOWED_CHAT_IDS`;
- для быстрых действий бот показывает reply-кнопки `Статус`, `Очередь`, `Помощь`;
- `telegram_state.json` хранит `last_update_id` и не должен коммититься в git.

## Что будет на выходе

Для каждого job создаются:
- итоговое видео `.mkv`
- текстовый файл с таймкодами `.txt`
- manifest `.json` с данными по сериям и качеству вырезки

По умолчанию в `S3` теперь уходит только `manifest`. Видео и `.txt` остаются локально и используются для публикации в `VK`.

## Примечания

- Номера серий определяются по имени файла, поэтому важно, чтобы исходники были нормально названы.
- Файлы вне `episodes_range` исключаются из сборки и попадают в `manifest` как `excluded_files`.
- Если AniSkip не вернул часть сегментов, скрипт продолжит работу и добавит предупреждения в manifest.
- Если AniSkip не нашёл тайминги по реальной длине эпизода, скрипт делает fallback-запрос с `episodeLength=0` и сохраняет источник таймингов в `timing_info` внутри manifest.
- По умолчанию `AniSkip` сейчас выключен через `timing_providers.aniskip_enabled = false`, и pipeline опирается на `AniLiberty + local detector`.
- Если включён `timing_detection`, скрипт пытается достроить отсутствующие `OP/ED` локальным audio-detector'ом по нескольким сериям сезона.
- Detector режет автоматически только интервалы с `high` confidence; всё остальное помечается как `manual_review` в `timing_info`.
- Сегменты теперь режутся с точным seek через re-encode, чтобы `op/ed` обрезались аккуратнее, чем при `-c copy`.
- После успешной обработки временные папки могут очищаться автоматически, если это включено в `cleanup`.
