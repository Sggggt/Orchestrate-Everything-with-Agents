"""Small, cached canvas topology; no executable configuration in previews."""

import math


def ports(kind):
    return {"condition": ["true", "false"], "model": ["tools", "deliver", "wait", "recover_context"],
            "observe": ["model", "wait"]}.get(kind, [] if kind == "deliver" else ["default"])


def canvas_preview(graph):
    nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
    edges = graph.get("edges", []) if isinstance(graph, dict) else []
    nodes = nodes if isinstance(nodes, list) else []
    edges = edges if isinstance(edges, list) else []
    visible = []
    for node in nodes[:128]:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            continue
        position = node.get("position") or {}
        position = position if isinstance(position, dict) else {}
        point = {key: value if isinstance(value := position.get(key, 0), (int, float)) and math.isfinite(value) else 0 for key in ("x", "y")}
        kind = node.get("type", "unknown")
        visible.append({"id": node["id"], "title": str(node.get("title") or node["id"])[:120],
                        "type": kind, "position": point, "ports": ports(kind) if isinstance(kind, str) else []})
    ids = {node["id"] for node in visible}
    links = [{key: edge.get(key, "default" if key == "branch" else "") for key in ("id", "source", "target", "branch")}
             for edge in edges[:512] if isinstance(edge, dict) and isinstance(edge.get("source"), str)
             and isinstance(edge.get("target"), str) and edge["source"] in ids and edge["target"] in ids]
    for node in visible:
        if node['type'] in ('model', 'observe'):
            outgoing = [edge for edge in links if edge['source'] == node['id']]
            if len(outgoing) == 1 and outgoing[0]['branch'] == 'default':
                node['ports'] = ['default']
    return {"nodes": visible, "edges": links, "entry": graph.get("entry") if isinstance(graph, dict) else None,
            "omitted": len(visible) != len(nodes) or len(links) != len(edges)}
