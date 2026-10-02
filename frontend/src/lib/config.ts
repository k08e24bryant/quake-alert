/** Public configuration. NEXT_PUBLIC_* must be read literally so Next.js can inline them. */

export function cleanUrl(value: string | undefined): string | null {
  const trimmed = value?.trim();
  return trimmed ? trimmed.replace(/\/+$/, "") : null;
}

export const config = {
  apiBaseUrl: cleanUrl(process.env.NEXT_PUBLIC_API_BASE_URL),
  telegramBotUrl: cleanUrl(process.env.NEXT_PUBLIC_TELEGRAM_BOT_URL),
  repoUrl: cleanUrl(process.env.NEXT_PUBLIC_REPO_URL),
} as const;
