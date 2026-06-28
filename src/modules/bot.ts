import { getJobs, insertJob, deleteJob, getRuntimeStatus as dbGetStatus, getRuntimeErrors } from "../shared/db";
import { getDisplayTitle, parseEpisodesRange, ensureNonEmptySlug } from "../shared/helpers";
import { findMatchingJob } from "./autojobs";
import { readFileSync, existsSync } from "node:fs";
import type { Job } from "../shared/types";

// ============================================================================
// Formatting
// ============================================================================

function escapeMdV2(text: string): string {
  return text.replace(/[\\_*[\]()~`>#+\-=|{}.!]/g, "\\$&");
}

export function formatDatetimeRu(iso: string | null | undefined): string {
  if (!iso) return "ещё не запускалось";
  try { return new Date(iso).toLocaleString("ru-RU", { timeZone: "Europe/Moscow" }); }
  catch { return String(iso); }
}

export function formatRuntimeStageRu(stage: string | null | undefined): string {
  const map: Record<string, string> = {
    cron_start: "запуск cron", discovery: "обновление очереди", processing: "обработка очереди",
    completed: "завершено", failed: "завершено с ошибкой", job_start: "старт аниме",
    job_completed: "аниме обработано", job_failed: "ошибка обработки", validation: "подготовка",
    download: "загрузка исходников", episode_scan: "поиск серий", detector: "поиск OP/ED",
    render_segments: "вырезка сегментов", concat: "склейка частей", final_render: "финальный рендер",
    delivery_s3: "сохранение манифеста", delivery_vk: "публикация в VK", job_done: "аниме готово",
  };
  return map[stage ?? ""] ?? (stage ?? "неизвестно");
}

export function buildMainKeyboard(): Record<string, unknown> {
  return {
    keyboard: [[{ text: "Статус" }, { text: "Текущая" }], [{ text: "Очередь" }, { text: "Ошибки" }], [{ text: "Лог" }, { text: "Помощь" }]],
    resize_keyboard: true, one_time_keyboard: false,
  };
}

// ============================================================================
// Status
// ============================================================================

export function formatStatusMessage(): string {
  const jobs = getJobs();
  const rs = dbGetStatus();
  const active = rs.current_job ? getDisplayTitle(rs.current_job) : "сейчас ничего не обрабатывается";
  return `Статус пайплайна\n\nАктивная задача: ${active}\nАниме в очереди: ${jobs.length}`;
}

// ============================================================================
// Jobs
// ============================================================================

export function formatJobsMessage(page = 1, pageSize = 15): string {
  const jobs = getJobs();
  if (jobs.length === 0) return "Очередь пуста";
  const tp = Math.max(1, Math.ceil(jobs.length / pageSize));
  const p = Math.max(1, Math.min(page, tp));
  const start = (p - 1) * pageSize;
  const end = Math.min(start + pageSize, jobs.length);
  const lines = ["Очередь аниме", "", `Всего: ${jobs.length}`, `Страница: ${p}/${tp}`, `Показываю: ${start + 1}-${end}`, ""];
  for (let i = start; i < end; i++) {
    const j = jobs[i]!;
    const o = j.automation?.is_ongoing ? " [ongoing]" : "";
    lines.push(`${i + 1}. ${getDisplayTitle(j)}${o}`);
    lines.push(`  Сезон: ${j.season ?? 1}`);
    lines.push(`  Эпизоды: ${j.episodes_range ?? "?"}`);
  }
  return lines.join("\n");
}

// ============================================================================
// Current
// ============================================================================

export function formatCurrentMessage(): string {
  const rs = dbGetStatus();
  const cj = rs.current_job;
  const qp = rs.queue_progress;
  if (rs.run_status === "running" && cj) {
    return ["Текущая обработка", "", `Тайтл: ${getDisplayTitle(cj)}`, `Сезон: ${cj.season ?? "?"}`,
      `Эпизоды: ${cj.episodes_range ?? "?"}`, `Этап: ${formatRuntimeStageRu(cj.stage ?? rs.current_stage)}`,
      `Прогресс очереди: ${qp?.current_job_index ?? 0}/${qp?.total_jobs ?? 0} | готово ${qp?.jobs_processed ?? 0} | ошибок ${qp?.jobs_failed ?? 0}`,
      `Текущая серия: ${cj.current_episode ?? "ещё не началась"}`, `Всего серий: ${cj.total_episodes ?? "неизвестно"}`,
      `Старт: ${formatDatetimeRu(cj.started_at ?? rs.run_started_at)}`].join("\n");
  }
  return "Сейчас ничего не обрабатывается\n\nИстория запусков пока пуста";
}

// ============================================================================
// Errors
// ============================================================================

export function formatErrorsMessage(limit = 5): string {
  const errors = getRuntimeErrors(limit);
  if (errors.length === 0) return "Ошибок пока нет\nИстория сбоев ещё не накоплена";
  const lines = ["Последние ошибки", ""];
  for (const e of errors) {
    lines.push(`${formatDatetimeRu(e.created_at)}\nКонтекст: ${e.context ?? "неизвестно"}\nЭтап: ${formatRuntimeStageRu(e.stage)}\nТайтл: ${getDisplayTitle(e)}\nОшибка: ${String(e.message).slice(0, 280)}`);
    lines.push("");
  }
  return lines.join("\n").trim();
}

// ============================================================================
// Help
// ============================================================================

export function formatHelpMessage(): string {
  return ["Команды бота", "", "/status - статус очереди", "/current - текущая обработка",
    "/errors - последние ошибки", "/jobs - аниме в очереди", "/log - хвост лога",
    "/remove <номер> - удалить из очереди", "",
    "Пример: /add Название ; 001-012 ; magnet:?xt=... ; 1 ; 5"].join("\n");
}

// ============================================================================
// /log
// ============================================================================

export function formatLogMessage(): string {
  const logPath = "logs/cron.log";
  if (!existsSync(logPath)) return "Лог ещё не создан";
  const lines = readFileSync(logPath, "utf-8").split("\n").filter(Boolean);
  const tail = lines.slice(-20);
  if (tail.length === 0) return "Лог пока пуст";
  let content = tail.join("\n");
  if (content.length > 3500) content = content.slice(-3500);
  return `Хвост лога (${Math.min(20, lines.length)} строк)\n\n${content}`;
}

// ============================================================================
// /add
// ============================================================================

const VALID_PRIVACY = new Set([0, 1, 2, 3, 5]);

export function parseAddCommand(text: string): { title: string; season: number; episodesRange: string; magnet: string; privacyView: number } {
  const raw = text.slice("/add ".length);
  const parts = raw.split(/\s*;\s*/).map(s => s.trim());
  if (![3, 4, 5].includes(parts.length)) throw new Error("Формат: /add Название ; 001-012 ; magnet:?xt=... ; 1 ; 5");
  const [title, eps, magnet, seasonStr = "1", privacyStr = "0"] = parts;
  if (!title) throw new Error("Нужно указать название тайтла");
  if (!magnet!.startsWith("magnet:?")) throw new Error("Magnet-ссылка должна начинаться с magnet:?");
  parseEpisodesRange(eps!);
  const season = Number(seasonStr);
  if (isNaN(season) || season < 1) throw new Error("Сезон должен быть целым числом не меньше 1");
  const privacy = Number(privacyStr);
  if (isNaN(privacy) || !VALID_PRIVACY.has(privacy)) throw new Error(`privacy_view должен быть одним из: ${[...VALID_PRIVACY].join(", ")}`);
  return { title: title!, season, episodesRange: eps!, magnet: magnet!, privacyView: privacy };
}

function buildManualJob(p: ReturnType<typeof parseAddCommand>): Job {
  const slug = ensureNonEmptySlug(p.title);
  const job: Job = { title: p.title, season: p.season, episodes_range: p.episodesRange,
    source: { type: "magnet", magnet: p.magnet, download_dir: `downloads/${slug}` } };
  if (p.privacyView !== 0) job.delivery = { vk_privacy_view: p.privacyView };
  return job;
}

export function addJobFromCommand(text: string): { added: boolean; job: Job; reason: string | null } {
  const cmd = parseAddCommand(text);
  const candidate = buildManualJob(cmd);
  const jobs = getJobs();
  if (findMatchingJob(jobs, candidate)) return { added: false, job: candidate, reason: "duplicate_job" };
  insertJob(candidate);
  return { added: true, job: candidate, reason: null };
}

export function formatAddResult(r: { added: boolean; job: Job }): string {
  const j = r.job;
  if (!r.added) return `Аниме не добавлено\n\nПричина: такое аниме уже есть в очереди\nТайтл: ${getDisplayTitle(j)}\nЭпизоды: ${j.episodes_range}`;
  const labels: Record<number, string> = { 0: "всем", 1: "участникам", 2: "редакторам", 3: "по ссылке", 5: "донам" };
  return `Аниме добавлено\n\nТайтл: ${getDisplayTitle(j)}\nСезон: ${j.season}\nЭпизоды: ${j.episodes_range}\nVK доступ: ${labels[j.delivery?.vk_privacy_view ?? 0] ?? "?"}`;
}

// ============================================================================
// Router
// ============================================================================

export function handleCommand(text: string): string | Record<string, unknown> {
  text = text.trim();
  if (text === "/start" || text === "/help") return formatHelpMessage();
  if (text === "/status") return formatStatusMessage();
  if (text === "/current") return formatCurrentMessage();
  if (text === "/errors") return formatErrorsMessage();
  if (text === "/log") return formatLogMessage();

  if (text.startsWith("/add ")) return formatAddResult(addJobFromCommand(text));

  if (text.startsWith("/remove ")) {
    const idx = Number(text.slice("/remove ".length));
    if (!idx || idx < 1) throw new Error("Формат: /remove <номер>");
    const jobs = getJobs();
    const job = jobs[idx - 1];
    if (!job) throw new Error(`Аниме с номером ${idx} не найдено`);
    if (job.id != null) deleteJob(job.id);
    return `Аниме удалено из очереди\n\nТайтл: ${getDisplayTitle(job)}\nСезон: ${job.season}\nЭпизоды: ${job.episodes_range}`;
  }

  if (text === "/jobs" || text.startsWith("/jobs ")) {
    const page = text.startsWith("/jobs ") ? Math.max(1, Number(text.slice(6)) || 1) : 1;
    return formatJobsMessage(page);
  }

  return "Неизвестная команда. Напиши /help";
}
