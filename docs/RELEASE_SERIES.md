# Release Completeness

`/release-series/` (Data menu, staff only) finds the missing issues of magazines and other periodicals: monthly or
bimonthly magazines (`COMIC Kairakuten BEAST 2026-12`), weeklies (`WEEKLY Kairakuten 2026 No.42`) and numbered series
(`COMIC X-EROS #141`). It's most useful to find what wasn't crawled while the backend was down.

Each **release series** says which galleries and archives belong to it and how to read the issue from their titles.
The check compares the issues found with the ones the series' schedule expects. A gallery **or** an archive is enough
for an issue to be found. An issue found only as galleries, none of them with an archive, is shown in purple on the grid
("gallery only") and listed in the JSON's `gallery_only`, so you know which issues still need their archive downloaded.
Galleries are only metadata, so completeness counts archives: the overview's Complete bar shows the issues archived (green,
yellow while some are missing or gallery only) followed by the gallery-only ones (purple), and the series page's Complete
tile is the archived percentage, with the percentage counting galleries under it. The JSON has both, `archive_completeness`
and `completeness`, with the `archived` count.

## Defining a series

| Field | |
|---|---|
| Name | Also used to build the default title regex and backlog query. |
| Kind of issue | Year and month (`2026-12`), year and issue number (`2026 No.42`) or issue number (`#141`). |
| Title contains | Candidates: galleries and archives whose title contains any of these texts (one per line, case-insensitive). Defaults to the name. |
| Title excludes | Titles containing any of these are skipped, e.g. `BEAST` and `XTC` for `COMIC Kairakuten`. |
| Tags | Candidates must have at least one of these tags, e.g. `anthology, other:anthology`. |
| Allowed languages | Candidates whose language tags aren't all in this list are skipped (`japanese`: raw only, translations have `translated` and their language). No language tag is always fine. |
| Title regex | One or more Python regexes, case-insensitive, with named groups `year` + `month`, `year` + `number`, or `number`. Optional `month_end` / `number_end` read combined issues (`2026-01・02`). Empty: built from the name, matching it at the start of the title, after optional `[..]`/`(..)` groups, so chapters that only mention the issue (`[Artist] Title (COMIC LO 2024-02)`) don't count. |
| Schedule | When issues come out, see below. Empty: the one suggested from the issues found. |
| Lead days | Days before its cover month an issue comes out (year and month series). Empty: measured from the galleries' posted dates (median of the last 36 issues). |
| Backlog query | Search sent to the `es-gallery-json` backlog sources for a missing issue. |
| Backlog page query | Search sent to the backlog sources that are a provider's search page (panda). |

### Schedule

One period per line, `<first issue> <rule>`, each until the next line's first issue. The first line is where the
check starts, so old scattered issues don't count as gaps.

```
2010-01 monthly
2024-02 even
```

- Year and month: `monthly`, `even`, `odd`, `quarterly`, or the months (`2,5,8,11`).
- Year and number: issues per year (`48`) or `auto` (the highest number found that year). The current year is checked up to the highest number found.
  Issues numbered without a year (`#1`, from title regex lines without a `year` group) take `all` or `none`, and come before the yearly ones, see below.
- Number: `all`, from that number to the highest found.
- Any: `none`: no issues from then on (ended, or paused until the next line).

Some series numbered their issues without a year before they started counting them per year
(`WEEKLY Kairakuten Vol.37`, then `WEEKLY Kairakuten 2021 No.1`). A year and number series reads both with two title
regex lines, the second without a `year` group, so its issues are `#1`, `#2`…; and a schedule that checks them first:

```
^WEEKLY\s+Kairakuten\s+(?P<year>\d{4})\s*No\.?\s*(?P<number>\d+)
^WEEKLY\s+Kairakuten\s+Vol\.?\s*(?P<number>\d+)(?:\s*[-&]\s*(?P<number_end>\d+))?
```

```
#1 all
2021 48
```

The series page suggests a schedule from the issues found: it starts at the first year from which every year has
most of its issues, and notices when a magazine switches between monthly and even or odd months. Save it as is, or
adjust it first.

### What counts as missing

With lead days (measured or set), a year and month series expects every issue released (cover month minus the lead
days) more than 10 days ago. An issue released in the last 10 days is **due**, and the next one is shown with its
expected date. Without lead days, and for the other kinds, the check ends at the last issue found.

Dismiss an expected issue that was never released, or came out as part of a combined issue, so it doesn't count as
missing. Dismissals can be restored.

The missing issues of every series are also listed together by (estimated) release date on the overview. Issues
released within 21 days of each other are grouped, so issues missing in several series at once stand out.

## Backlog search

"Backlog search" on a missing issue opens the search dialog for the sources in `backlog_search.urls` without querying
them automatically, so you can deselect sources or adjust the title before clicking "Search" to read the first page of
results. Nothing is added yet; "Add checked" queues the checked results' links in the web crawler, like Add URLs (needs
the `crawler_adder` permission). Several issues can be searched in turn from the overview or a series page.

A gallery found by several sources is shown once. Each result shows what helps choosing which one to queue:

- whether its title (or Japanese title), read with the series' title regex, is the issue, and its languages;
- **already here**: it's a gallery here already (adding it again doesn't download it again when it has an archive,
  unless the crawler's `redownload` setting is on);
- **not counted**: it's here, but neither it nor its archives count as this issue, so the issue can still show as
  missing. Its tooltip says why, per gallery and archive: its title doesn't contain the series' title text or contains
  an excluded text, it doesn't have any of the series' tags, its language isn't allowed, or its title here isn't read
  as this issue by the title regex. Old galleries without the `other:anthology` tag are a common case: fix the series
  (or the gallery) instead of adding it again;
- **expunged**: removed from the provider's listings;
- **replaced by a newer version** (with a link to it, "newer version here" when that one is already here), or **update
  of an older version**: galleries that replaced each other form a chain, and the newest has priority;
- the `other:already_uploaded` and `other:replaced` tags;
- the gallery id, posted date, pages and the sources that found it.

At most one result per issue is checked: its title is the issue, in an allowed language, not already here, not expunged
and not replaced; among those, without the tags above, with the most pages, then the newest. None is checked when a
result for the issue, or the newer version of one, is already here. The panda API and the `es-gallery-json` documents
both tell about expunged galleries and chains; when both sources find a gallery, the provider's API wins.

There are two kinds of source:

- An `es-gallery-json` endpoint of another instance gets the **backlog query**, in Elasticsearch's query string syntax,
  on `title`, `title_jpn` and `tags.full`. Default: `title:"{title}" AND NOT tags.full:"language:translated"`.
- Any other URL is read as a provider's search page, for now panda's (`https://e-hentai.org/?...&f_search=`). It gets
  the **backlog page query**, in the provider's search syntax, default `"{title}" -language:translated$`. The
  galleries on the page are then read from the provider's API, with its settings (cookies, rate limits).

Placeholders, in both: `{title}`, `{name}`, `{key}` (`2026-12`, `2026 No.42`, `#141`), `{year}`, `{month}`,
`{number}`; e.g. `{month:02d}`. `{title}` is the title searched: the name and the issue (`WEEKLY Kairakuten 2024 No.9`).

Each issue in the dialog has its **title to search**, which can be changed for uploads titled in an unusual way
(`Comic Kairakuten Beast - Vol.019`), then searched again for that issue alone. It replaces `{title}` in both queries;
a query without `{title}` is replaced by the default one for that search. The results are still read as that issue,
with the series' title regex.

## Export, import and JSON

Export downloads every series and its dismissals as JSON. Import creates or updates series by name (fields left out
keep their value, or the default for a new series) and adds dismissals. A minimal import:

```json
{
  "series": [
    {"name": "COMIC Kairakuten BEAST", "key_type": "year_month", "title_contains": "Kairakuten BEAST", "tags": "anthology, other:anthology"},
    {"name": "WEEKLY Kairakuten", "key_type": "year_number", "title_excludes": "SELECTION", "tags": "anthology, other:anthology"},
    {"name": "COMIC X-EROS", "key_type": "number", "tags": "anthology, other:anthology"}
  ]
}
```

`?format=json` on the overview and on a series page returns the results, for scripts.
