"""Summary-only presentation. Complete results remain available to Agent peers."""
from __future__ import annotations

import json
import re

DETAILS_MARKER = "<!-- agent:details -->"
SUMMARY_LIMIT = 1200
OUTPUT_GUIDANCE = (
    "面向用户的输出使用简短 Markdown，先概述任务、已做的工作和结论（约100–250字）。"
    "完整分析、证据、代码和交接内容放在 <!-- agent:details --> 标记之后；"
    "前端仅展示标记前的摘要，协作 Agent 接收完整内容。"
    "这只是展示约定，不要求 JSON，不为排版或缺少标记重新调用模型。"
)


def markdown_summary(value, *, limit=SUMMARY_LIMIT, fallback="未取得可展示的结果，请查看执行记录。"):
    """Best-effort projection, never a business-output validator."""
    if isinstance(value, dict):
        for key in ("summary_markdown", "summary", "text", "content", "conclusion"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return markdown_summary(candidate, limit=limit, fallback=fallback)
        analysis = value.get("analysis") or {}
        cards = analysis.get("cards", []) if isinstance(analysis, dict) else []
        lines = []
        if value.get("stage"):
            stages = {"intake": "需求整理", "research": "资料研究", "analysis": "结果分析",
                      "reuse": "复用审查", "design": "方案设计", "verify": "验证与交付"}
            lines.append("**任务**：" + stages.get(value["stage"], str(value["stage"])))
        if cards:
            lines.append(f"已整理 {len(cards)} 项结果。")
            lines.extend("- " + str(c.get("conclusion", ""))[:180]
                         for c in cards[:3] if isinstance(c, dict) and c.get("conclusion"))
        if value.get("issues"):
            lines.append(f"有 {len(value['issues'])} 项待处理事项，已保留在完整协作结果中。")
        if lines:
            return markdown_summary("\n\n".join(lines), limit=limit, fallback=fallback)
        return "已保留结构化结果，请查看详情。" if value and any(item not in (None, "", [], {}) for item in value.values()) else fallback
    if value is None:
        return fallback
    text = str(value).strip()
    if not text:
        return fallback
    # Legacy structured outputs are summarized, never dumped into the chat pane.
    candidate = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.I)
    candidate = re.sub(r"\n?```\s*$", "", candidate).strip()
    if candidate.startswith(("{", "[")):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            return fallback
        if isinstance(parsed, dict):
            return markdown_summary(parsed, limit=limit, fallback=fallback)
        return f"已整理 {len(parsed)} 项结果，完整内容已保留供协作使用。"
    text = text.split(DETAILS_MARKER, 1)[0].strip()
    # Code/JSON blocks belong to complete peer output, not the short status pane.
    text = re.split(r"(?m)^\s*```", text, maxsplit=1)[0].strip()
    # Accept ordinary Markdown headings as an optional details boundary as well.
    text = re.split(r"(?im)^#{1,6}\s*(?:详细内容|完整分析|完整内容|协作详情|details|full report)\s*$", text, maxsplit=1)[0].strip()
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] or text[:limit]
        text += "\n\n完整内容已保留供协作使用。"
    return text or fallback


def delivery_summary(value, *, limit=SUMMARY_LIMIT):
    partial = markdown_summary(value, limit=max(1, limit - 40), fallback="")
    return "交付检查未通过。" + ("\n\n" + partial if partial else "请查看保留的结果和执行记录。")


def result_summary(result):
    if (getattr(result, "metadata", {}).get("acceptance") or {}).get("state") == "FAILED":
        return delivery_summary(getattr(result, "output", {}))
    explicit = getattr(result, "summary_markdown", "")
    output = getattr(result, "output", {})
    return markdown_summary(explicit or output)


class SummaryStream:
    """Stream a bounded Markdown prefix; delimiters may cross SSE chunks."""
    def __init__(self, limit=SUMMARY_LIMIT):
        self.buffer = ""
        self.emitted = 0
        self.limit = limit
        self.closed = False

    def feed(self, delta):
        if self.closed or not isinstance(delta, str):
            return ""
        self.buffer += delta
        stripped = self.buffer.lstrip()
        if stripped.startswith(("{", "[", "```json")):
            # A structured legacy answer cannot be safely presented until complete.
            if len(self.buffer) > self.limit:
                self.closed = True
                self.buffer = self.buffer[:self.limit]
            return ""
        boundary = self.buffer.find(DETAILS_MARKER)
        fence = re.search(r"(?m)^\s*```", self.buffer)
        if fence and (boundary < 0 or fence.start() < boundary):
            boundary = fence.start()
        heading = re.search(r"(?im)^#{1,6}\s*(?:详细内容|完整分析|完整内容|协作详情|details|full report)\s*(?:\n|$)", self.buffer)
        if heading and (boundary < 0 or heading.start() < boundary):
            boundary = heading.start()
        end = min(boundary if boundary >= 0 else len(self.buffer), self.limit)
        if boundary < 0:
            # Withhold a possible partial HTML marker and the unfinished last heading.
            for start in range(max(self.emitted, len(self.buffer) - len(DETAILS_MARKER)), len(self.buffer)):
                if DETAILS_MARKER.startswith(self.buffer[start:]):
                    end = min(end, start)
                    break
            line = self.buffer.rfind("\n") + 1
            if self.buffer[line:].startswith("#"):
                end = min(end, line)
            if self.buffer[line:] and "```".startswith(self.buffer[line:]):
                end = min(end, line)
        delta = self.buffer[self.emitted:max(self.emitted, end)]
        self.emitted += len(delta)
        if boundary >= 0 or len(self.buffer) >= self.limit:
            self.closed = True
            self.buffer = self.buffer[:self.limit]
        return delta

    def finish(self):
        if self.closed:
            return ""
        summary = markdown_summary(self.buffer, limit=self.limit, fallback="")
        delta = summary[self.emitted:]
        self.emitted = len(summary)
        self.closed = True
        return delta
