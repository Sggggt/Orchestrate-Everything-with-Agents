export const ARTIFACT_PREVIEW_BYTES = 1024 * 1024;
export const artifactDownload = (id, project_id) => `/api/artifacts/${encodeURIComponent(id)}/content?project_id=${encodeURIComponent(project_id)}`;

export function artifactSource(artifact, agents = []) {
  if (artifact.origin === "run" && artifact.agent_id) {
    const agent = agents.find(item => item.id === artifact.agent_id);
    return { key: "agent:" + artifact.agent_id, kind: "agent", agent_id: artifact.agent_id,
      label: agent?.name || artifact.agent_name || artifact.agent_id };
  }
  if (artifact.origin === "upload") return { key: "upload", kind: "upload", label: "用户上传" };
  if (artifact.origin === "import") return { key: "import", kind: "import", label: "导入文件" };
  return { key: "unknown", kind: "unknown", label: "来源未记录" };
}

export function groupArtifacts(items, agents = []) {
  const groups = new Map();
  for (const artifact of items) {
    const source = artifactSource(artifact, agents);
    if (!groups.has(source.key)) groups.set(source.key, { source, items: [] });
    groups.get(source.key).items.push(artifact);
  }
  return [...groups.values()];
}

export function decodeArtifactText(bytes, truncated = false) {
  const encoding = bytes[0] === 0xff && bytes[1] === 0xfe ? "utf-16le"
    : bytes[0] === 0xfe && bytes[1] === 0xff ? "utf-16be" : "utf-8";
  let text;
  try { text = new TextDecoder(encoding, { fatal: true }).decode(bytes, { stream: truncated }); }
  catch { return { kind: "unsupported", message: "此文件无法按 UTF-8 或带编码标记的 UTF-16 文本读取，请下载查看。" }; }
  if (/[\x00-\x08\x0e-\x1f]/.test(text)) return { kind: "unsupported", message: "此文件包含二进制内容，请下载后用对应程序查看。" };
  return { kind: "text", text, encoding, truncated };
}

// Bound the original read-only browser preview of the authenticated download.
export async function readArtifactPreview(artifact, { fetcher = fetch, limit = ARTIFACT_PREVIEW_BYTES } = {}) {
  const type = artifact.media_type || "";
  if (!/(?:\+json|\+xml)$/.test(type) && /^(?:image\/|audio\/|video\/|application\/(?:pdf|zip|gzip|x-7z|vnd\.))/.test(type)) {
    return { kind: "unsupported", message: "此格式暂不支持原文预览，请下载后用对应程序查看。" };
  }
  const response = await fetcher(artifactDownload(artifact.id, artifact.project_id));
  if (!response.ok) {
    const value = response.headers.get("content-type")?.includes("application/json") ? await response.json() : null;
    throw new Error(value?.error?.safe_message || "产物读取失败，请稍后再试。");
  }
  const reader = response.body.getReader(), chunks = [];
  let length = 0, truncated = artifact.bytes > limit;
  try {
    while (length < limit) {
      const { value, done } = await reader.read();
      if (done) break;
      const taken = value.subarray(0, limit - length);
      chunks.push(taken); length += taken.byteLength;
      if (taken.byteLength < value.byteLength) truncated = true;
      if (length === limit) { await reader.cancel(); break; }
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return decodeArtifactText(bytes, truncated);
}
