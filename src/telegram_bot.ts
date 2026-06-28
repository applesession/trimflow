import { initDb, getTelegramState, saveTelegramState } from "./shared/db";
import { ensureRuntimePaths, logLine } from "./shared/runtime";
import { fetchUpdates, sendMessage, sendFormattedMessage, isAllowedChat } from "./api/telegram";
import { handleCommand, buildMainKeyboard } from "./modules/bot";

const paths = ensureRuntimePaths();
const logPath = paths.telegramLogPath;

initDb();
let state = getTelegramState();

logLine(logPath, "start telegram_bot");

let failureDelay = 5;

while (true) {
  let updates: unknown[] = [];

  try {
    const offset = state.last_update_id != null ? state.last_update_id + 1 : undefined;
    updates = fetchUpdates(offset, 30);
    failureDelay = 5;
  } catch (err) {
    logLine(logPath, `telegram_poll_failed error=${String(err)} retry_in=${failureDelay}s`);
    Bun.sleepSync(failureDelay * 1000);
    failureDelay = Math.min(failureDelay * 2, 60);
    continue;
  }

  for (const update of updates) {
    const upd = update as Record<string, unknown>;
    try {
      const message = (upd.message ?? (upd.callback_query as Record<string, unknown>)?.message) as Record<string, unknown> | undefined;
      const chat = (message?.chat ?? {}) as Record<string, unknown>;
      const chatId = chat.id;
      const text = (upd.message?.text ?? "") as string;

      if (chatId == null || !isAllowedChat(String(chatId))) continue;

      if (text && String(text).trim()) {
        const cmdText = String(text).trim();
        const aliases: Record<string, string> = {
          "Статус": "/status", "Текущая": "/current", "Очередь": "/jobs",
          "Ошибки": "/errors", "Лог": "/log", "Помощь": "/help",
        };
        const mappedText = aliases[cmdText] ?? cmdText;

        let response: string | Record<string, unknown>;
        try {
          response = handleCommand(mappedText);
        } catch (err) {
          response = `Команда не выполнена\n\nПричина: ${String(err)}`;
        }

        const replyText = typeof response === "string" ? response : (response as Record<string, string>).text ?? String(response);
        const parseMode = typeof response === "object" ? (response as Record<string, string>).parseMode : undefined;

        if (parseMode) {
          sendFormattedMessage(chatId, replyText, parseMode, buildMainKeyboard());
        } else {
          sendMessage(chatId, replyText, buildMainKeyboard());
        }
      }

      const updateId = upd.update_id as number;
      if (updateId != null) {
        state.last_update_id = updateId;
        state.last_handled_at = upd.message?.date as number ?? state.last_handled_at;
        saveTelegramState(state);
      }
    } catch (err) {
      logLine(logPath, `telegram_update_failed error=${String(err)} update_id=${(upd as Record<string, unknown>).update_id}`);
      const updateId = (upd as Record<string, unknown>).update_id as number;
      if (updateId != null) {
        state.last_update_id = updateId;
        saveTelegramState(state);
      }
    }
  }
}
