"""One-off: prune entity-graph links pointing at deleted memories.

Run with the memline daemon STOPPED (the in-daemon entity lock cannot
protect against an external writer). Talks to the qdrant server directly.

- entity rows whose links ALL point at deleted memories -> row deleted
- rows with some stale links -> linked_memory_ids rewritten to live ones
"""

import json
import urllib.request

BASE = "http://127.0.0.1:6333"
MAIN = "workspace_agent_memory"
ENTITIES = "workspace_agent_memory_entities"
DRY_RUN = True


def api(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def scroll(collection: str, with_payload):
    offset = None
    while True:
        body = {"limit": 1000, "with_payload": with_payload, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        result = api(f"/collections/{collection}/points/scroll", body)["result"]
        yield from result["points"]
        offset = result.get("next_page_offset")
        if offset is None:
            return


live = {str(p["id"]) for p in scroll(MAIN, with_payload=False)}
print(f"live memories: {len(live)}")

to_delete: list[str] = []
rewritten = 0
scanned = 0
stale_links = 0
for point in scroll(ENTITIES, with_payload=["linked_memory_ids"]):
    scanned += 1
    links = (point.get("payload") or {}).get("linked_memory_ids") or []
    keep = [m for m in links if m in live]
    if len(keep) == len(links):
        continue
    stale_links += len(links) - len(keep)
    if not keep:
        to_delete.append(point["id"])
    else:
        rewritten += 1
        if not DRY_RUN:
            api(
                f"/collections/{ENTITIES}/points/payload?wait=true",
                {"payload": {"linked_memory_ids": keep}, "points": [point["id"]]},
            )

if to_delete and not DRY_RUN:
    for i in range(0, len(to_delete), 500):
        api(f"/collections/{ENTITIES}/points/delete?wait=true", {"points": to_delete[i : i + 500]})

print(
    f"scanned={scanned} stale_links_removed={stale_links} "
    f"rows_rewritten={rewritten} orphan_rows_deleted={len(to_delete)} dry_run={DRY_RUN}"
)
