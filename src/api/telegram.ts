import { request as httpsRequest } from "node:https";
import { SocksProxyAgent } from "socks-proxy-agent";

const TELEGRAM_API_BASE = "https://api.telegram.org";

// ============================================================================
// Config helpers
// ============================================================================

export function getTelegramToken(): string {
  return (Bun.env.TELEGRAM_BOT_TOKEN ?? "").trim();
}

export function getTelegramProxyUrl(): string {
  return (Bun.env.TELEGRAM_PROXY_URL ?? "").trim();
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
// Telegram API call via node:https (SOCKS5 proxy support)
// ============================================================================

function createAgent(): SocksProxyAgent | undefined {
  const proxy = getTelegramProxyUrl();
  if (!proxy) return undefined;
  return new SocksProxyAgent(proxy);
}

function nodePost(url: string, body: string, timeoutMs = 60_000): Promise<{ text: string; status: number }> {
  const parsed = new URL(url);
  const agent = createAgent();

  return new Promise((resolve, reject) => {
    const req = httpsRequest(
      url,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": String(Buffer.byteLength(body)),
        },
        agent,
        timeout: timeoutMs,
      },
      (res) => {
        let data = "";
        res.on("data", chunk => data += chunk);
        res.on("end", () => resolve({ text: data, status: res.statusCode ?? 0 }));
        res.on("error", reject);
      },
    );
    req.on("error", reject);
    req.on("timeout", () => { req.destroy(); reject(new Error(`Telegram API timeout: ${url}`)); });
    req.write(body);
    req.end();
  });
}

async function telegramRequest(
  method: string,
  payload: Record<string, unknown> = {},
  timeout = 60_000,
): Promise<{ ok: boolean; result?: unknown; description?: string }> {
  const token = getTelegramToken();
  if (!token) throw new Error("TELEGRAM_BOT_TOKEN is not configured");

  const url = `${TELEGRAM_API_BASE}/bot${token}/${method}`;
  const body = JSON.stringify(payload);

  const { text, status } = await nodePost(url, body, timeout);

  if (status < 200 || status >= 300) {
    let description: string | undefined;
    try {
      description = (JSON.parse(text) as { description?: string }).description;
    } catch {
      description = text.trim() || undefined;
    }
    throw new Error(`Telegram API ${method} HTTP ${status}: ${description ?? "unknown error"}`);
  }

  const data = JSON.parse(text) as { ok: boolean; result?: unknown; description?: string };
  if (!data.ok) {
    throw new Error(`Telegram API ${method} failed: ${data.description ?? JSON.stringify(data)}`);
  }
  return data;
}

// ============================================================================
// Public API methods
// ============================================================================

export async function fetchUpdates(offset?: number, timeout = 30) {
  const payload: Record<string, unknown> = {
    timeout,
    allowed_updates: ["message", "callback_query"],
  };
  if (offset != null) payload.offset = offset;
  const data = await telegramRequest("getUpdates", payload, (timeout + 10) * 1000);
  return (data.result as unknown[]) ?? [];
}

export async function sendMessage(
  chatId: string | number,
  text: string,
  replyMarkup?: Record<string, unknown>,
) {
  const payload: Record<string, unknown> = {
    chat_id: String(chatId),
    text,
    disable_web_page_preview: true,
  };
  if (replyMarkup) payload.reply_markup = replyMarkup;
  await telegramRequest("sendMessage", payload, 20_000);
}

export async function sendFormattedMessage(
  chatId: string | number,
  text: string,
  parseMode: string,
  replyMarkup?: Record<string, unknown>,
) {
  const payload: Record<string, unknown> = {
    chat_id: String(chatId),
    text,
    disable_web_page_preview: true,
    parse_mode: parseMode,
  };
  if (replyMarkup) payload.reply_markup = replyMarkup;
  await telegramRequest("sendMessage", payload, 20_000);
}

export async function sendMessageWithFallback(
  chatId: string | number,
  text: string,
  options: { parseMode?: string; replyMarkup?: Record<string, unknown> } = {},
): Promise<void> {
  const { parseMode, replyMarkup } = options;

  if (parseMode) {
    try {
      await sendFormattedMessage(chatId, text, parseMode, replyMarkup);
      return;
    } catch (err) {
      if (parseMode === "MarkdownV2" && isTelegramMarkdownRetryableError(err)) {
        await sendMessage(chatId, demoteMarkdownV2ToPlainText(text), replyMarkup);
        return;
      }
      throw err;
    }
  }

  await sendMessage(chatId, text, replyMarkup);
}

export async function answerCallbackQuery(callbackQueryId: string, text?: string) {
  const payload: Record<string, unknown> = { callback_query_id: callbackQueryId };
  if (text) payload.text = text;
  await telegramRequest("answerCallbackQuery", payload, 20_000);
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

export async function sendMessageToAllowedChats(
  text: string,
  options: { parseMode?: string; replyMarkup?: Record<string, unknown> } = {},
): Promise<void[]> {
  if (!telegramNotificationsEnabled()) return [];

  const chats = [...parseAllowedChatIds()].sort();
  return Promise.all(
    chats.map(chatId => sendMessageWithFallback(chatId, text, options)),
  );
}
