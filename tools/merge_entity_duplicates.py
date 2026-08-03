"""One-off: merge duplicate entity-graph rows and drop trivial noise.

Duplicates exist because entity dedup used to be (user, agent, run)-scoped, so
every session recreated the same entities. The fork is now user-scoped; this
migration collapses the historical fragments.

Merge key: (user_id, normalized text) where normalization = lowercase,
whitespace collapse, and hyphen-spacing collapse ("pre - patch" == "pre-patch").
Canonical row = most-linked; linked_memory_ids are unioned. Rows whose
normalized text is <3 chars or pure digits/punctuation are deleted as noise.

Run with the memline daemon STOPPED; talks to qdrant directly.
"""

import json
import re
import urllib.request
from collections import defaultdict

BASE = "http://127.0.0.1:6333"
ENTITIES = "workspace_agent_memory_entities"
DRY_RUN = True


def api(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def scroll():
    offset = None
    while True:
        body = {
            "limit": 1000,
            "with_payload": ["data", "entity_type", "linked_memory_ids", "user_id"],
            "with_vector": False,
        }
        if offset is not None:
            body["offset"] = offset
        result = api(f"/collections/{ENTITIES}/points/scroll", body)["result"]
        yield from result["points"]
        offset = result.get("next_page_offset")
        if offset is None:
            return


def normalize(text: str) -> str:
    norm = " ".join(text.strip().lower().split())
    return norm.replace(" - ", "-")


NOISE = re.compile(r"^[\W\d_]*$")

groups: dict[tuple, list[dict]] = defaultdict(list)
noise_rows: list[str] = []
total = 0
for point in scroll():
    total += 1
    payload = point.get("payload") or {}
    data = payload.get("data") or ""
    norm = normalize(data)
    if len(norm) < 3 or NOISE.match(norm):
        noise_rows.append(point["id"])
        continue
    groups[(payload.get("user_id") or "workspace", norm)].append(
        {
            "id": point["id"],
            "data": data,
            "links": payload.get("linked_memory_ids") or [],
        }
    )

merged_groups = 0
rows_deleted: list[str] = []
links_unioned = 0
for key, rows in groups.items():
    if len(rows) < 2:
        continue
    merged_groups += 1
    rows.sort(key=lambda r: len(r["links"]), reverse=True)
    canonical, rest = rows[0], rows[1:]
    union = set(canonical["links"])
    for row in rest:
        union.update(row["links"])
        rows_deleted.append(row["id"])
    links_unioned += len(union) - len(canonical["links"])
    if not DRY_RUN:
        api(
            f"/collections/{ENTITIES}/points/payload?wait=true",
            {"payload": {"linked_memory_ids": sorted(union)}, "points": [canonical["id"]]},
        )

if not DRY_RUN:
    doomed = rows_deleted + noise_rows
    for i in range(0, len(doomed), 500):
        api(f"/collections/{ENTITIES}/points/delete?wait=true", {"points": doomed[i : i + 500]})

print(
    f"total={total} dup_groups={merged_groups} dup_rows_deleted={len(rows_deleted)} "
    f"noise_rows_deleted={len(noise_rows)} links_gained_on_canonicals={links_unioned} dry_run={DRY_RUN}"
)
if DRY_RUN:
    sample = sorted(
        ((k, len(v)) for k, v in groups.items() if len(v) > 1), key=lambda x: -x[1]
    )[:10]
    print("top duplicate groups:")
    for (user, norm), n in sample:
        print(f"  {n:4d}x {norm[:60]}")
