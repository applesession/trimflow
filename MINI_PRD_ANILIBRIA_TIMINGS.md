# Mini PRD: AniLibria Timings Provider

## Summary

Добавить в pipeline новый источник таймингов `OP/ED` из AniLibria/AniLiberty API и встроить его в существующий каскад вместе с `AniSkip` и локальным detector-ом.

Цель:
- повысить покрытие по `OP/ED`, когда `AniSkip` даёт `404` или неполные данные;
- лучше работать именно с релизами AniLiberty, где provider-specific тайминги потенциально ближе к фактическому видео;
- уменьшить долю `manual_review`, особенно на маленьких диапазонах вроде `001-003`.

## Problem

Текущий pipeline опирается на три механизма:
- `AniSkip exact`
- `AniSkip episodeLength=0`
- `local detector`

На практике это уже работает лучше, чем изначальная версия, но остаются проблемы:
- `AniSkip` может возвращать `404` по части эпизодов;
- detector хорошо добирает `ED`, но `OP` всё ещё нестабилен;
- часть серий уходит в `manual_review`, хотя у AniLiberty могут быть собственные тайминги.

## Product Goal

Pipeline должен уметь использовать AniLibria как дополнительный provider таймингов, не ломая текущие fallback-сценарии.

Ожидаемый результат:
- для релизов AniLiberty pipeline сначала пробует взять provider-specific `OP/ED`;
- если данные есть, они используются раньше AniSkip;
- если данных нет, поведение остаётся как сейчас.

## Success Criteria

- У части тайтлов, где сейчас `AniSkip` даёт `404`, появляются валидные `OP/ED`.
- Доля `episodes_manual_review` снижается.
- В manifest всегда прозрачно видно, откуда пришли тайминги:
  - `anilibria_exact`
  - `aniskip_exact`
  - `aniskip_lengthless`
  - `audio_fingerprint`
- Новая интеграция не ломает текущий pipeline и не ухудшает поведение для не-AniLiberty сценариев.

## Non-Goals

- Не заменять AniSkip полностью.
- Не удалять local detector.
- Не строить discovery новых релизов в этой итерации.
- Не использовать deprecated `api_v3.md` как основной контракт.

## Target User / Use Case

Основной пользователь:
- оператор пайплайна, который хочет минимизировать ручное заполнение и ручной просмотр серий;
- запуск происходит локально или на VPS для сборки compilation-видео по релизам AniLiberty.

Основной сценарий:
1. В job указан AniLiberty-релиз.
2. Pipeline пытается получить `OP/ED`.
3. Если AniLibria API знает тайминги, они используются сразу.
4. Если нет, включается текущий каскад `AniSkip -> detector`.

## Proposed Source Priority

Новый приоритет источников:

1. `AniLibria exact`
2. `AniSkip exact`
3. `AniSkip episodeLength=0`
4. `local detector season_consensus`
5. `local detector aniskip_reference`
6. `manual_review`

Логика:
- AniLibria идёт первым, потому что это provider-specific источник для релизов AniLiberty.
- AniSkip остаётся глобальным fallback.
- detector остаётся последней линией автоматического восстановления таймингов.

## Proposed Architecture

### 1. New module

Добавить новый модуль, например:
- `lib/anilibria.py`

Ответственность:
- запрос релиза/эпизодов через актуальный AniLiberty API v1;
- извлечение `opening/ending/skips`, если они реально есть в ответе;
- нормализация в тот же внутренний формат сегментов, который уже понимает pipeline.

### 2. New internal shape

Внутренний normalized result должен быть совместим с `get_aniskip_segments()`:

```json
{
  "segments": [
    {
      "type": "op",
      "start": 83.0,
      "end": 173.0,
      "source": "anilibria_exact",
      "confidence": "high"
    }
  ],
  "request_error": null,
  "request_urls": ["..."],
  "provider": "anilibria"
}
```

### 3. Pipeline integration

В `process_episode()` и/или в prefetch-слое:
- сначала попытка `AniLibria`;
- потом merge с текущим `AniSkip`;
- detector вызывается только для тех типов, которые всё ещё отсутствуют.

### 4. Detector integration

Если `AniLibria` дал `OP` или `ED`, detector должен уметь использовать их так же, как сейчас использует AniSkip-reference:
- `reference_source = anilibria_exact`
- приоритет reference:
  1. `anilibria_exact`
  2. `aniskip_exact`
  3. `aniskip_lengthless`

## Data / Metadata Requirements

Manifest должен расшириться прозрачными полями источника:

- `timing_info.per_type.*.source`
- `timing_info.per_type.*.match_strategy`
- `timing_info.per_type.*.reference_source`
- `timing_info.per_type.*.reference_episode`
- `timing_info.per_type.*.reference_similarity`

На верхнем уровне manifest:
- `timing_detection` остаётся только про detector;
- для внешних timing providers можно добавить `timing_sources_summary`, например:
  - `anilibria_available`
  - `aniskip_available`
  - `detector_available`

## Failure Modes

Нужно корректно обработать:
- AniLibria API не отвечает;
- API отвечает без skip-данных;
- skip-данные есть, но неполные (`только ED`, `только OP`);
- данные расходятся с AniSkip.

Принцип:
- внешний provider не должен валить весь job;
- при ошибке просто переходим к следующему источнику;
- если есть конфликт между AniLibria и AniSkip, приоритет у AniLibria, но в manifest должно быть видно, что был альтернативный внешний источник.

## Implementation Phases

### Phase 1. Feasibility / contract verification

- Подтвердить по актуальному API v1, в каком endpoint и в каком field shape реально приходят `OP/ED/skips`.
- Выбрать стабильный endpoint под релиз/эпизоды.
- Зафиксировать маппинг полей в коде и в README.

### Phase 2. Provider implementation

- Добавить `lib/anilibria.py`
- Реализовать:
  - запрос
  - парсинг
  - нормализацию сегментов
  - error handling

### Phase 3. Pipeline wiring

- Встроить AniLibria перед AniSkip.
- Обновить merge логики по типам.
- Поддержать `anilibria_exact` в manifest и quality summary.

### Phase 4. Detector reference support

- Разрешить detector использовать `anilibria_exact` как reference-source.
- Отразить это в metadata и cache key.

### Phase 5. Validation

- Прогнать минимум 2-3 тайтла:
  - кейс, где AniSkip падает, а AniLibria знает тайминги;
  - кейс, где знают оба;
  - кейс, где не знает никто, и работает detector/manual review.

## Testing

### Unit

- parser AniLibria response -> normalized segments
- merge precedence `anilibria_exact > aniskip_exact > aniskip_lengthless`
- detector reference priority prefers `anilibria_exact`

### Integration

- `AniLibria only`
- `AniLibria + AniSkip both available`
- `AniLibria missing -> AniSkip fallback`
- `AniLibria missing -> AniSkip missing -> detector fallback`

### Manual acceptance

- Manifest явно показывает новый источник.
- Pipeline не падает, если AniLibria недоступна.
- Качество вырезки не ухудшается на текущих работающих кейсах.

## Open Questions

- В каком именно endpoint v1 лежат `opening/ending/skips` для релиза/эпизода.
- Насколько эти тайминги привязаны к конкретной версии релиза.
- Нужно ли включать AniLibria provider для всех job или только для `source.type = magnet` / AniLiberty-подобных релизов.

## Recommended Default Decisions

- Использовать только актуальный AniLiberty API v1.
- Включать AniLibria provider по умолчанию для текущих AniLiberty job.
- Считать `anilibria_exact` самым приоритетным внешним timing source.
- Не убирать AniSkip и detector, а строить layered fallback.
