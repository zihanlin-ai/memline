# Publishing and maintaining wiki pages

Load this file when a reviewed draft has the user's explicit approval to
publish, or when maintaining the published corpus (routing, listings,
provenance, staleness). Publishing without that approval is the one
unforgivable failure of this pipeline.

## Publish, after the user approves

Before moving any file, rerun the draft gate against the version the user saw:

```
memline wiki check-draft drafts/<slug>.md \
  --review drafts/<slug>.review.json \
  --adjudication drafts/<slug>.adjudication.json --strict
```

Proceed only when it exits zero: deterministic checks are clean, at least one
review pass is contract-valid, every finding has an Agent ruling, and no
blocking finding remains. Non-blocking findings do not require correction or
another LLM call. This machine result is necessary but not permission; the
user's explicit approval of this exact reviewed version is still required.

1. Move the draft into `content/`. A Blog filename and `date` use the **newest
   cited memory's date** — the article is dated by when the work happened;
   `published_on` records the write-up day and `evidence_span` the range.
2. Add frontmatter provenance — the contract `wiki check-pages` verifies:
   `sources: [{ref: "mem:<id>"|"sources/<path>", content_hash: "<sha256 now>"}]`.
   `content/blog/**` is a frozen archive, so the check verifies that its
   sources still exist and retain their publication-time hashes but does not
   flag a memory merely because it later gained a new head. `content/docs/**`
   represents current guidance and therefore requires active memory heads.
   A Docs page may cite a superseded memory only as explicit correction history:
   mark that source entry `historical: true`. The checker still requires the
   memory to exist and retain its publication-time hash, but does not mistake
   that declared historical citation for current guidance. Never use this flag
   to excuse a current claim whose support was superseded.
3. Carry the reviewed claims sidecar's `summary` and the accepted topic's
   `topic_key` into the frontmatter. A useful approved-topic `value` may seed
   the summary, but it is not copied blindly: the card must describe the final
   article after review. It is what a shelf listing shows and what a reviewer
   reads before opening the page — without it a reader has to open pages to
   learn what they are for.
4. Sanitize at publication too: no internal operational identifiers that are
   unnecessary for reuse — IPs, hostnames, job/container ids, person names,
   internal URLs, absolute paths. Public model names, versions, commits, error
   codes and measurements stay.
5. Route the page: add it to `content/docs/.nav.yml` by hand (Blog needs no
   entry — the blog shelf listing covers it). The skeleton is hand-written
   and stays that way: it carries entry points and reading order, which is
   judgement no generator has. A named entry states where the page sits in a
   reading order; a directory glob (`serving/*`) absorbs pages that only need
   to be reachable.
6. Regenerate the computed blocks: `memline wiki index`. One command now
   maintains both kinds — the opted-in shelf listings (a README that already
   carries the index markers; it never creates a file) and every page's
   related-pages block (computed from shared evidence; a page with no
   relations still gets the block saying so). Deterministic and idempotent,
   so run it on every publish rather than intending to run it eventually.
7. Gate: `memline wiki check-pages --strict`. This is the whole health
   question in one command — missing `summary`, `topic_key`, provenance,
   hashes, broken links, **and routing drift**: a page no `.nav.yml` entry
   reaches (`page_unrouted`) or an entry pointing at a page that moved
   (`nav_entry_dangling`). Any flag is a publication failure, not a warning
   to remember later. (`memline wiki nav` is the fast read-only subset for
   iterating on the skeleton without paying for a store round-trip.)
8. Record the publication in the ledger and `memline add` the fact.

On a frozen Blog, step 6 appends a block and never touches the prose;
verified byte-for-byte on a published article. What is frozen is what the
article claimed, not whether a catalogue card may sit beside it.

## Related-block thresholds

The defaults — three shared references *and* 15% of the smaller page — come
from the data, not from taste. Every overlapping pair among the 73 Docs
candidates shared exactly one memory, which is what unrelated pages look like
here; meanwhile a Docs page distilled from an article often sits entirely
inside it, which is why the share is measured against the smaller side.
Adjust with `memline wiki index --min-shared / --min-share` if the shape of
the content changes, and say so in the ledger when you do.

**Prose cross-references are still not part of publishing.** The generated
block claims only that two pages drew on the same material — it explicitly
disclaims saying the same thing. A sentence that says "the rule for this is
stated in <page>" carries meaning and only its writer can put it there. Let
those grow on top of the generated layer as the content demands.

## How the published corpus is organized

`content/docs/` follows the vLLM calibration contract
(`materials/vllm-docs-latest/STRUCTURE-NOTES.md`): task-oriented chapters,
filenames that carry the identifier a reader would glob for, most directories
deliberately without a README. A README exists only where the directory has a
meaningful default page (a family overview, a guide entry) and must carry
real orientation content — never a generated list of child links. There is no
whole-corpus index; `.nav.yml` plus the filenames replaced it, and
`content/docs/README.md` states the retrieval protocol (nav first, filename
glob second, full-text search for identifiers only).

## Staleness

When a page is flagged — evidence missing, superseded, or silently changed
since publication, or a frozen Blog contradicting updated Docs — mark it
`status: stale-pending-review` and treat it as not-current-fact until the user
rules. After review, current Docs and formal errata win; a frozen Blog body
stays as history.

The three flags mean different things and the middle one is the dangerous one:

- `memory_missing` — the citation resolves to nothing; the sentence is
  unsupported and must be re-evidenced or removed.
- `memory_content_changed` — the citation still resolves and nothing looks
  broken, but the text behind it is no longer the text that supported the
  claim. Read the sentence against the current memory before believing either.
- `memory_superseded` — the evidence was replaced. On a Docs page that is a
  correction to make; on a frozen Blog it is history and stays, because an
  overturned belief is what that article exists to record.

`memline wiki check-pages <wiki-root>` runs the same check on its own, for
looking at a single page without compiling.
