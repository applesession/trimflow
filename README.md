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

```
src/
├── shared/          # Инфраструктура (db, config, helpers, runtime, validation)
├── api/             # Внешние HTTP/S3 клиенты (anilibria, aniskip, vk, storage)
├── core/            # Бизнес-логика (pipeline, media, detector, discovery, runner)
├── modules/         # Фичи (autojobs discovery, telegram bot logic)
├── cron_run.py      # Cron: discovery → render-lock → processing → TG уведомления
├── discover_jobs.py # Автономный discovery
└── telegram_bot.py  # Long-polling бот
main.py              # Точка входа: ручной запуск
```
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
- `VK_PUBLIC_GROUP_ID`
- `VK_PRIVATE_GROUP_ID` — обязателен для приватных/donut-релизов с `vk_privacy_view = 5`
- `VK_DONUT_LEVEL_ID` — нужен только если используется `video.save` с `vk_privacy_view = 5`
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
- `vk_wall_post_enabled`
- `vk_comment_enabled`
- `vk_privacy_view`

`vk_privacy_view` управляет режимом VK-публикации:
- `0`, `1`, `2`, `3` — обычная публикация: видео и пост в основном паблике
- `5` — приватный/donut-сценарий: видео загружается в приватный паблик с доступом `by_link`, а в основном паблике создаётся donut-пост с названием и ссылкой на это видео

Для приватного сценария комментарий под постом не создаётся, даже если `vk_comment_enabled = true`.

## Запуск

```bash
python main.py
```

## Discovery свежих релизов

```bash
python src/discover_jobs.py
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
python src/cron_run.py
```

Что делает runner:
- выполняет discovery под отдельным lock и безопасно дополняет SQLite-очередь;
- пытается взять render-lock; занятый lock не прерывает активный render;
- после каждого job перечитывает очередь, поэтому новый ongoing становится следующим по приоритету;
- пишет сводку в `stdout` и в `logs/cron.log`.

Runtime-файлы:
- `.runtime/discovery.lock` — lock активного discovery
- `.runtime/cron.lock` — lock активного render
- `logs/cron.log` — append-only лог раннера

Рекомендуемая команда для crontab:

```bash
*/5 * * * * cd /path/to/workspace-gojo-satoru && python src/cron_run.py
```

## Telegram Bot

```bash
python src/telegram_bot.py
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

VK-доставка теперь поддерживает два сценария:
- публичные релизы — видео + пост в основном паблике;
- приватные/donut-релизы (`vk_privacy_view = 5`) — видео в приватном паблике, donut-пост со ссылкой в основном паблике.

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
- При ошибке compilation render готовые финальные чанки остаются в `temp/` и используются следующим запуском; итоговая сборка выполняется через `ffmpeg -c copy`.
- Если render-процесс аварийно исчез, следующий cron вернёт job в очередь, запишет `render_interrupted` и отправит Telegram; OOM определяется через `journalctl` best-effort.
