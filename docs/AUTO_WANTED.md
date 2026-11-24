# Auto Wanted Sources

`/auto-wanted/` (Manage menu, staff only) sets up the provider queries that create wanted galleries for new
releases: a wanimagazine RSS feed or products list, a fakku listing, a doujinshi.org mirror search. Each
**source** is one query. The `auto_wanted` scheduler runs each active one every `frequency`, or every
`auto_wanted.cycle_timer` hours when it has none.
A release that isn't a wanted gallery yet (same title) becomes one, with the source's values. One that already is
only gets a mention.

## Defining a source

| Field | |
|---|---|
| Name | Unique. Import matches sources by name. |
| Provider | A provider with a wanted generator (`core/providers/<provider>/wanted.py`). |
| Active | Run by the scheduler. Inactive sources can still be run by hand. |
| Frequency | How often the scheduler runs it, in the Monitored Links format: `06:00:00` (6 hours), `1 00:00:00` (a day). Empty: every `auto_wanted.cycle_timer` hours. |
| Force process | Process every link listed, even the ones processed before. Normally they're skipped, and a page with only those stops the query. |
| Query parameters | Depend on the provider, see below. |
| Reason, should search, keep searching, notify when found, public | The values of the wanted galleries created. |
| Wanted providers | The providers the wanted galleries are searched in (slugs, comma or space separated). |
| Unwanted title (+ regex flags) | Unwanted title of the wanted galleries. New sources start with the `auto_wanted.unwanted_title` setting. |

### Provider parameters

- **wanimagazine**: `subpath` (required): the RSS feed (`comics/feed/` for R18 単行本, `magazine/feed/` for R18 雑誌),
  or the products list page (`comics_index/`) with `url_html_type` on. `stop_page`: the last products list page read
  (default 1).
- **fakku**: `subpath` (required, `books`, `tags/doujin`), `container_tag` (required: CSS selector of each gallery's
  container), `link_tag` and `url_attribute_name` (the link inside it and its URL attribute), or
  `link_attribute_get_text` to read the URL from the container's text.
- **doujinshi_mirror**: the book search filters of the mirror's JSON API (`GET /api/v1/books`, see the
  doujinshi-browser `docs/API.md`): `q` and `match` (title), `circle`, `author`, `parody`, `character`, `contents`,
  `genre`, `type`, `convention`, `collections`, `publisher`, `imprint` (a name or id; `A|B` is either), `with` /
  `without` (item keys, `C1,K25`), `language`, `adult`, `anthology`, `copyshi`, `magazine` (`1`/`0`),
  `released_from` / `released_to`, `pages_min` / `pages_max`, `sort` and `order` (default `id` `desc`, newest added
  first), and `stop_page`, the last page read (50 books a page; default: until a page has only known books). Needs
  `providers.doujinshi_mirror.general.url`. Books already added by the old mugimugi provider (same gid) count as
  known.
- **mugimugi** (doujinshi.org, closed): no longer has a generator. Migration `viewer.0236` turned its sources into
  doujinshi_mirror ones, translating the parameters (`slist_C` → `circle`, `flist` → `type`, `age` → `adult`,
  `date` → `released_from`, `order=added&flow=DESC` → `sort=id&order=desc`...; the old parameters are kept in the
  source's notes). `cont`, `sub`, `FREE` and `scen` have no equivalent and are left out. Several values in a
  `slist_<letter>` (`A|B`) now mean either value.

The form shows each provider's parameters. Save and import check them: unknown names, wrong types and missing
required ones are refused.

## Running

- The `auto_wanted` scheduler (`auto_wanted.enable`, `startup`) wakes up when the next active source is due (its
  last run plus its frequency, or plus `cycle_timer` hours; a source that never ran is due right away), and at least
  every 15 minutes to see new or changed sources. It runs the due sources in a thread per provider, one after the
  other. A provider whose sources are still running is left for the next check, so its runs never overlap. A forced
  run from the workers dashboard runs every active source. The list shows when each source runs next.
- **Run selected**, **Run all active now**, **Run now** and **Save and run** queue an `auto_wanted_worker` task
  (workers dashboard) that runs the sources one after the other.
- Each run saves on the source when it ran, whether it went fine (`ok`, `errors`, `failed`), its error messages and
  how many new links, wanted galleries and mentions it added. The wanted galleries remember the source that created
  them (`WantedGallery.auto_wanted_source`), listed on the source's page.

## Export and import

**Export** downloads every source as JSON. **Import** takes that file (from here or another instance) or pasted
JSON, and creates or updates sources by name. Fields left out keep their value, or the default for a new source.
An empty `frequency` (`""`) means the `cycle_timer` default.
`?format=json` on the list also gives the last run of each source.

```json
{
  "version": 1,
  "sources": [
    {
      "name": "wanimagazine comics",
      "provider": "wanimagazine",
      "active": true,
      "frequency": "06:00:00",
      "force_process": false,
      "params": {"subpath": "comics/feed/"},
      "reason": "wanimagazine",
      "public": false,
      "should_search": true,
      "keep_searching": false,
      "notify_when_found": false,
      "wanted_providers": ["panda"],
      "unwanted_title": "",
      "regexp_unwanted_title": false,
      "regexp_unwanted_title_icase": false,
      "notes": ""
    }
  ]
}
```

## From the old configuration

Auto wanted used to read its queries from provider attributes (Django admin, provider properties):
`wanted_params_<query>_<name>`, `wanted_reason_<query>`, `wanted_public_<query>`, `wanted_should_search_<query>`,
`wanted_keep_searching_<query>`, `wanted_notify_when_found_<query>`, `wanted_provider(s)_<query>`, wanimagazine's
`stop_page_<query>` and the provider-wide `force_process`. Only the providers listed in `auto_wanted.providers` ran.

Those attributes aren't read anymore. While they exist, the list page offers to **import them as sources**
(`<provider> <query>`, active when the provider is in `auto_wanted.providers`, with the defaults each generator used
for missing attributes), shows them as JSON, and once imported offers to delete them. The `auto_wanted.providers`
setting is only used by that import now. mugimugi queries are imported as doujinshi_mirror sources, with the
parameters translated like the migration does.
