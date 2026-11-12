/** Bound the complete read, including a response body that never finishes. */
export async function readShareJson(
  url: string,
  init: Pick<RequestInit, "credentials"> = {},
  timeoutMs = 45_000
): Promise<{ response: Response; data: unknown }> {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 60_000) {
    throw new Error("分享读取期限不符合范围");
  }
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      reject(new Error("读取超时，请重新检查"));
      controller.abort();
    }, timeoutMs);
  });
  const read = (async () => {
    const response = await fetch(url, { ...init, signal: controller.signal });
    const data = await response.json().catch(() => null);
    return { response, data };
  })();
  try {
    return await Promise.race([read, timeout]);
  } finally {
    clearTimeout(timer);
  }
}
