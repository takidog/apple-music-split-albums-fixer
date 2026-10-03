from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


def children(node: dict[str, Any]) -> list[dict[str, Any]]:
    value = node.get("children", [])
    return value if isinstance(value, list) else []


def walk(nodes: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for node in nodes:
        yield node
        yield from walk(children(node))


def item_fields(item: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for node in children(item):
        tag = node.get("tag")
        if not tag or tag in result:
            continue
        if "integer" in node:
            result[tag] = node["integer"]
        elif "text" in node:
            result[tag] = node["text"]
        elif "sha256" in node:
            # Redacted text still has a stable digest suitable for grouping.
            result[tag] = f"sha256:{node['sha256']}"
    return result


def extract_items(document: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for row in document:
        if not str(row.get("path", "")).endswith("/items"):
            continue
        dmap = row.get("response", {}).get("dmap", [])
        for node in walk(dmap):
            if node.get("tag") != "mlit":
                continue
            fields = item_fields(node)
            if not {"miid", "asal", "asco"}.issubset(fields):
                continue
            key = (fields.get("miid"), fields.get("asco"), fields.get("astn"))
            if key not in seen:
                found.append(fields)
                seen.add(key)
    return found


def album_key(item: dict[str, Any]) -> tuple[Any, ...]:
    # aePI is the album persistent identity in the observed Cloud Library delta.
    # Fall back to album name + album artist when it is absent.
    if "aePI" in item:
        return ("aePI", item["aePI"])
    return ("metadata", item.get("asal"), item.get("asaa"))


def make_plan(items: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        groups[album_key(item)].append(item)

    repairs: list[dict[str, Any]] = []
    consistent: list[dict[str, Any]] = []
    for key, album_items in groups.items():
        flags = Counter(item["asco"] for item in album_items)
        summary = {
            "album_key": list(key),
            "album": album_items[0].get("asal"),
            "track_count": len(album_items),
            "compilation_values": dict(sorted(flags.items())),
        }
        if len(flags) == 1:
            consistent.append(summary)
            continue
        consensus, count = flags.most_common(1)[0]
        # A tie has no safe consensus, so report it without suggesting writes.
        tied = sum(1 for value in flags.values() if value == count) > 1
        outliers = [] if tied else [
            {
                "miid": item["miid"],
                "track": item.get("astn"),
                "disc": item.get("asdn"),
                "current_asco": item["asco"],
                "proposed_asco": consensus,
            }
            for item in album_items
            if item["asco"] != consensus
        ]
        repairs.append({**summary, "status": "ambiguous_tie" if tied else "outliers_found", "outliers": outliers})

    return {
        "mode": "read_only_repair_plan",
        "items_examined": len(items),
        "albums_examined": len(groups),
        "albums_with_mixed_compilation_flags": repairs,
        "consistent_albums": consistent,
        "writes_generated": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a read-only repair plan from an exported Cloud Library sync detail JSON file.")
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()

    document = json.loads(args.input.read_text(encoding="utf-8"))
    plan = make_plan(extract_items(document))
    rendered = json.dumps(plan, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
