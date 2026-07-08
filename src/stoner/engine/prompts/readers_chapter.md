<!--
readers_chapter.md — the batched chapter-read prompt for reader simulation.

One completion covers ONE chapter for k personas at once (the cost lever). Each
persona is addressed by a verbatim voice block plus its rolled-forward state
(what it remembers, expects, and has grown tired of). Every persona returns
span-anchored EVENTS only — bored, confused, reread, hooked — each anchored by a
verbatim quote copied from the chapter, never a numeric score. The response is
STRICT JSON keyed by persona id.

Placeholders: {chapter_number}, {chapter_body}, {canon}, {readers_block}.
Rendered by pipelines/common.render_prompt.
-->
# Chapter {chapter_number}

You are simulating a focus group of distinct readers, each reading this chapter
as themselves. Read the chapter once per reader, in that reader's sensibility,
given what they already remember and expect. Then report, per reader, where the
chapter caught them and where it lost them.

## The chapter (full text)

{chapter_body}

## Canon (background only — do not summarize it back)

{canon}

## The readers

{readers_block}

## What to return

For each reader, emit a list of MARKERS — span-anchored reactions of exactly
these four types:

- "hooked": a moment that pulled the reader in.
- "bored": a moment the reader's attention drifted.
- "confused": a moment the reader lost the thread.
- "reread": a moment the reader had to go back over to follow.

Every marker MUST carry a "quote" copied VERBATIM from the chapter text above
(a short phrase or sentence), so it can be located programmatically. Do NOT
invent quotes, paraphrase, or give star ratings, scores, or numbers — this is
event reporting, not scoring. A reader who sailed through cleanly may return few
or no markers; do not manufacture reactions.

Then, for each reader, update their carried state in their own voice:

- "memory": one line on what they now remember of the story (their words).
- "expectations": a short list of what they now expect or want next.
- "fatigue": one line on what, if anything, they have grown tired of.

Respond with STRICT JSON only (a single fenced json block or raw JSON, no other
prose) matching exactly this shape, keyed by the reader ids shown above:

```json
{
  "readers": {
    "READER_ID": {
      "markers": [
        {"type": "hooked", "quote": "verbatim text from the chapter", "note": "why, briefly"}
      ],
      "memory": "what this reader now remembers, in their voice",
      "expectations": ["what they want or expect next"],
      "fatigue": "what they have grown tired of, or empty"
    }
  },
  "summary": "one line on where the roster split"
}
```
