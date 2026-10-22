export async function api(path, options = {}) {
  const headers = { "X-OEA-View-Version": "1.1", ...options.headers };
  if (
    options.body &&
    typeof options.body !== "string" &&
    !(options.body instanceof Blob)
  ) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch("/api" + path, { ...options, headers });
  const value = await response.json();
  // A successful task response may include its execution error as data.
  if (!response.ok) {
    const error = new Error(value.error?.safe_message || "本地服务请求失败");
    error.code = value.error?.code;
    error.details = value.error?.details || {};
    throw error;
  }
  return value;
}
