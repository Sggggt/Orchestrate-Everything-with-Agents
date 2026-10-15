// Pure canvas geometry and editing constraints. Runtime validation is authoritative.
export const NODE_WIDTH = 216;
export const NODE_HEIGHT = 120;
export const clone = (value) => JSON.parse(JSON.stringify(value));
export const clampZoom = (value) => Math.max(0.2, Math.min(2, value));

export function portPoint(node, handle = "default", input = false) {
  return { x: node.position.x + (input ? 0 : NODE_WIDTH),
    y: node.position.y + (input ? 60 : handle === "true" ? 42 : handle === "false" ? 84 : 60) };
}

export function curve(from, to) {
  const bend = Math.max(60, Math.abs(to.x - from.x) * 0.48);
  return `M ${from.x} ${from.y} C ${from.x + bend} ${from.y}, ${to.x - bend} ${to.y}, ${to.x} ${to.y}`;
}

export function canConnect(graph, source, target, handle = "default") {
  const from = graph.nodes.find((node) => node.id === source), to = graph.nodes.find((node) => node.id === target);
  if (!from || !to) return "连线节点不存在。";
  if (source === target) return "节点不能连接自身。";
  if (from.type === "end" || to.type === "start") return "开始节点不能有输入，结束节点不能有输出。";
  if ((from.type === "condition" && !["true", "false"].includes(handle)) || (from.type !== "condition" && handle !== "default")) return "请选择正确的输出端口。";
  if (graph.edges.some((edge) => edge.source === source && edge.target === target && (edge.source_handle || "default") === handle)) return "这两个端口已连接。";
  const visited = new Set(), pending = [target];
  while (pending.length) {
    const current = pending.pop();
    if (current === source) return "此连线会形成循环依赖。";
    if (visited.has(current)) continue;
    visited.add(current);
    pending.push(...graph.edges.filter((edge) => edge.source === current).map((edge) => edge.target));
  }
  return "";
}

export function autoLayout(graph) {
  const arranged = clone(graph), levels = new Map(), pending = new Set(graph.nodes.map((node) => node.id));
  while (pending.size) {
    const ready = graph.nodes.filter((node) => pending.has(node.id) && graph.edges.filter((edge) => edge.target === node.id).every((edge) => !pending.has(edge.source)));
    if (!ready.length) break;
    for (const node of ready) {
      const parents = graph.edges.filter((edge) => edge.target === node.id).map((edge) => levels.get(edge.source) ?? 0);
      levels.set(node.id, parents.length ? Math.max(...parents) + 1 : 0);
      pending.delete(node.id);
    }
  }
  const slots = new Map();
  for (const node of arranged.nodes) {
    const level = levels.get(node.id) ?? 0, slot = slots.get(level) ?? 0;
    node.position = { x: 60 + level * 300, y: 80 + slot * 172 };
    slots.set(level, slot + 1);
  }
  return arranged;
}

export function fitViewport(graph, width, height) {
  if (!graph.nodes.length) return { x: 0, y: 0, zoom: 1 };
  const left = Math.min(...graph.nodes.map((node) => node.position.x));
  const top = Math.min(...graph.nodes.map((node) => node.position.y));
  const right = Math.max(...graph.nodes.map((node) => node.position.x + NODE_WIDTH));
  const bottom = Math.max(...graph.nodes.map((node) => node.position.y + NODE_HEIGHT));
  const zoom = Math.min(1, clampZoom(Math.min((width - 64) / (right - left), (height - 96) / (bottom - top))));
  return { x: (width - (right - left) * zoom) / 2 - left * zoom,
    y: (height - (bottom - top) * zoom) / 2 - top * zoom, zoom };
}
