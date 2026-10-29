"""Bounded discovery over the same currently authorized tool registry."""

from oea.contracts.errors import require


def summaries(gateway, names):
    result = []
    for name in names:
        provider = gateway.providers[name]
        item = {"name": name, "description": " ".join(provider.describe().description.split())[:200]}
        upstream = getattr(provider, "upstream_name", None)
        if upstream is not None:
            item["source"] = provider.server_id + "/" + upstream
        result.append(item)
    return result


def search(gateway, names, *, query="", cursor=0, limit=10):
    require(type(cursor) is int and cursor >= 0 and type(limit) is int and 1 <= limit <= 20,
            "INVALID_REQUEST", "Read a bounded tool directory page")
    terms = query.casefold().split()
    found = []
    for item in summaries(gateway, names):
        text = (item["name"] + " " + item.get("source", "") + " " + gateway.providers[item["name"]].describe().description).casefold()
        score = sum(term in text for term in terms)
        if not terms or score:
            found.append((score, item))
    found.sort(key=lambda entry: (-entry[0], entry[1]["name"]))
    page = found[cursor:cursor + limit]
    return {"items": [item for _, item in page], "cursor": cursor + limit if len(found) > cursor + limit else None}
