const TELEGRAM_API_BASE = "https://api.telegram.org";

// ============================================================================
// Config helpers
// ============================================================================

export function getTelegramToken(): string {
  return (Bun.env.TELEGRAM_BOT_TOKEN ?? "").trim();
}

export function parseAllowedChatIds(rawValue?: string): Set<string> {
  const value = rawValue ?? Bun.env.TELEGRAM_ALLOWED_CHAT_IDS ?? "";
  const result = new Set<string>();
  for (const item of value.split(",")) {
    const v = item.trim();
    if (v) result.add(v);
  }
  return result;
}

export function isAllowedChat(chatId: string | number, allowedChatIds?: Set<string>): boolean {
  const ids = allowedChatIds ?? parseAllowedChatIds();
  return ids.has(String(chatId));
}

export function telegramNotificationsEnabled(): boolean {
  return Boolean(getTelegramToken() && parseAllowedChatIds().size > 0);
}

// ============================================================================
// Telegram API via curl subprocess (proxychains-compatible)
// ============================================================================

function telegramRequest(
  method: string,
  payload: Record<string, unknown> = {},
  timeout = 60,
): { ok: boolean; result?: unknown; description?: string } {
  const token = getTelegramToken();
  if (!token) throw new Error("TELEGRAM_BOT_TOKEN is not configured");

  const url = `${TELEGRAM_API_BASE}/bot${token}/${method}`;
  const body = JSON.stringify(payload);

  const proc = Bun.spawnSync(
    ["curl", "-s", "--connect-timeout", String(timeout), "--max-time", String(timeout),
      "-X", "POST", "-H", "Content-Type: application/json", "-d", body, url],
    { stdout: "pipe", stderr: "pipe" },
  );

  const stdout = new TextDecoder().decode(proc.stdout);
  const stderr = new TextDecoder().decode(proc.stderr);

  if (proc.exitCode !== 0) {
    throw new Error(`Telegram API ${method} curl failed (exit ${proc.exitCode}): ${stderr || stdout}`);
  }

  let data: { ok: boolean; result?: unknown; description?: string };
  try {
    data = JSON.parse(stdout) as typeof data;
  } catch {
    throw new Error(`Telegram API ${method} invalid JSON: ${stdout.slice(0, 200)}`);
  }

  if (!data.ok) {
    throw new Error(`Telegram API ${method} failed: ${data.description ?? JSON.stringify(data)}`);
  }
  return data;
}

// ============================================================================
// Public API methods
// ============================================================================

export function fetchUpdates(offset?: number, timeout = 30): unknown[] {
  const payload: Record<string, unknown> = { timeout, allowed_updates: ["message", "callback_query"] };
  if (offset != null) payload.offset = offset;
  const data = telegramRequest("getUpdates", payload, timeout + 10);
  return (data.result as unknown[]) ?? [];
}

export function sendMessage(
  chatId: string | number,
  text: string,
  replyMarkup?: Record<string, unknown>,
): void {
  const payload: Record<string, unknown> = { chat_id: String(chatId), text, disable_web_page_preview: true };
  if (replyMarkup) payload.reply_markup = replyMarkup;
  telegramRequest("sendMessage", payload, 20);
}

export function sendFormattedMessage(
  chatId: string | number,
  text: string,
  parseMode: string,
  replyMarkup?: Record<string, unknown>,
): void {
  const payload: Record<string, unknown> = { chat_id: String(chatId), text, disable_web_page_preview: true, parse_mode: parseMode };
  if (replyMarkup) payload.reply_markup = replyMarkup;
  telegramRequest("sendMessage", payload, 20);
}

export function sendMessageWithFallback(
  chatId: string | number,
  text: string,
  options: { parseMode?: string; replyMarkup?: Record<string, unknown> } = {},
): void {
  const { parseMode, replyMarkup } = options;

  if (parseMode) {
    try {
      sendFormattedMessage(chatId, text, parseMode, replyMarkup);
      return;
    } catch (err) {
      if (parseMode === "MarkdownV2" && isTelegramMarkdownRetryableError(err)) {
        sendMessage(chatId, demoteMarkdownV2ToPlainText(text), replyMarkup);
        return;
      }
      throw err;
    }
  }

  sendMessage(chatId, text, replyMarkup);
}

export function answerCallbackQuery(callbackQueryId: string, text?: string): void {
  const payload: Record<string, unknown> = { callback_query_id: callbackQueryId };
  if (text) payload.text = text;
  telegramRequest("answerCallbackQuery", payload, 20);
}

export function isTelegramMarkdownRetryableError(err: unknown): boolean {
  const message = String(err ?? "").toLowerCase();
  return (
    message.includes("can't parse entities") ||
    message.includes("cannot parse entities") ||
    message.includes("can't find end of")
  );
}

export function demoteMarkdownV2ToPlainText(text: string): string {
  let value = text.replace(/\\([_*[\]()~`>#+\-=|{}.!])/g, "$1");
  value = value.replace(/\*/g, "").replace(/`/g, "");
  return value;
}

export function sendMessageToAllowedChats(
  text: string,
  options: { parseMode?: string; replyMarkup?: Record<string, unknown> } = {},
): void {
  if (!telegramNotificationsEnabled()) return;

  for (const chatId of [...parseAllowedChatIds()].sort()) {
    sendMessageWithFallback(chatId, text, options);
  }
}
