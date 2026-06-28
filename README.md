# workspace-gojo-satoru

Локальный пайплайн сборки аниме-компиляций из эпизодов на TypeScript/Bun + SQLite.

- получает серии из magnet-ссылки или из локальной папки
- автоматически находит свежие ongoing-релизы из AniLiberty
- запрашивает у AniSkip / AniLiberty интервалы `op/ed`
- вырезает пропуски через `ffmpeg` с Python audio-fingerprint детектором
- склеивает итоговый файл, накладывает watermark
- загружает в S3 и публикует в VK (включая donut-релизы)
- управляется через Telegram-бота

## Стек

- **Runtime**: [Bun](https://bun.sh) (TypeScript)
- **Хранилище**: SQLite (`data.db`) в WAL-режиме — транзакционное, без гонок
- **Детектор OP/ED**: Python 3 + numpy/librosa (вызывается как subprocess)
- **Системные утилиты**: `ffmpeg`, `ffprobe`, `aria2c` (должны быть в PATH)
- **Зависимости**: `@aws-sdk/client-s3` (S3), `socks-proxy-agent`

## Структура

```
src/
├── shared/          # Инфраструктура (types, db, config, helpers, runtime)
├── api/             # Внешние HTTP/S3 клиенты (anilibria, aniskip, vk, s3, telegram)
├── core/            # Бизнес-логика (pipeline, media, detector, discovery, runner)
├── modules/         # Фичи (autojobs discovery, telegram bot logic)
├── main.ts          # Ручной запуск
├── cron_run.ts      # Cron: lock → discovery → processing → TG уведомления
├── discover_jobs.ts # Автономный discovery
├── telegram_bot.ts  # Long-polling бот
└── detector.py      # Python audio-fingerprint детектор (вызывается из core/detector.ts)
```

## Настройка

```bash
bun install
```

Скопировать `config.example.json` → `config.json`, заполнить `.env` по примеру из `README` ниже.

## Переменные окружения (`.env`)

- `S3_ENDPOINT`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION`, `S3_BUCKET_NAME`
- `VK_ACCESS_TOKEN`, `VK_API_VERSION`
- `VK_PUBLIC_GROUP_ID`, `VK_PRIVATE_GROUP_ID` (для donut), `VK_DONUT_LEVEL_ID`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS`
- `TELEGRAM_PROXY_URL`, `ANILIBERTY_PROXY_URL` (опционально, `socks5://...`)
- `TELEGRAM_STATE_PATH` (опционально)

## Запуск

```bash
# Ручной прогон очереди
bun run src/main.ts

# Cron-раннер (discovery + обработка + TG-уведомления)
bun run src/cron_run.ts

# Только discovery
bun run src/discover_jobs.ts

# Telegram-бот
bun run src/telegram_bot.ts
```

## Миграция со старой Python-версии

```bash
bun run scripts/migrate.ts
```

Переносит данные из `jobs.json`, `state.json`, `completed_jobs.json`, `telegram_state.json`, `.runtime/*.json` в SQLite `data.db`.

## Команды Telegram-бота

- `/start`, `/help` — справка
- `/status` — статус очереди и runtime
- `/current` — текущая обработка
- `/jobs` — список аниме в очереди
- `/errors` — последние ошибки
- `/add Название ; 001-012 ; magnet:?xt=... ; 1 ; 5` — добавить вручную
- `/remove <номер>` — удалить из очереди
- `/complete <номер>` — пометить завершённым

## Что будет на выходе

Для каждого job создаются:
- итоговое видео `.mkv`
- текстовый файл с таймкодами `.txt`
- manifest `.json` с данными по сериям и качеству вырезки
