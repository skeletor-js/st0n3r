<!--
ship_query.md — user prompt for `stoner ship blurbs` (query-letter artifact).

Filled by ship/blurbs.py via render_prompt. Placeholders:
  {title}, {author}, {word_count}, {logline}, {comps}, {promise},
  {canon}, {memory}, {threads}

No manuscript prose is present — only canon and rolling summaries
(invariant 4).
-->

Draft a one-page query letter to a literary agent for the novel "{title}"
({word_count} words) by {author}.

Follow the standard shape:

- A hook: one or two sentences that make an agent want the pages.
- A mini-synopsis: the setup, the protagonist's want and the obstacle, the
  stakes, and the choice the book turns on — WITHOUT giving away the ending
  (a query withholds the ending; a synopsis does not).
- A short comps-and-positioning line drawn from the Genre & Comps below.
- A one-line bio slot the author will fill in, marked `[BIO]`.

## Logline
{logline}

## Genre & Comps
{comps}

## Promise to the Reader
{promise}

## Story summaries (in order)
{memory}

## Threads
{threads}

## Canon
{canon}

Write only the query letter, as markdown. Keep it to roughly 250-350 words.
