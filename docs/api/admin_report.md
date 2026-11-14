# Admin Report (`/admin-report/`)

A summary of what happened in a time window, plus what currently needs an admin's attention. The same data is shown as a staff page and returned as JSON, for scripts and monitoring tools.

The data is built by `build_admin_report` in [`viewer/services/admin_report.py`](../../viewer/services/admin_report.py); the view is in [`viewer/views/admin_report.py`](../../viewer/views/admin_report.py).

## Access

Staff users only:

- With a logged-in session (the page, or `?format=json`).
- With a bearer token that has the `read` scope, whose user is staff: `Authorization: Bearer <token>`.

A non-staff user, or a missing or invalid token, gets `403` with `{"error": "..."}` for JSON requests.

```sh
curl -H "Authorization: Bearer $TOKEN" "https://panda.example/admin-report/?format=json&window=7d&log_level=ERROR"
```

## Parameters

| Parameter | Values | Default |
| :--- | :--- | :--- |
| `format` | `json` returns JSON; anything else renders the page | page |
| `window` | `24h`, `7d`, `30d` (ending now), or `custom` | `24h` (`custom` when `since` is given) |
| `since` / `until` | ISO date or datetime (`2026-11-01`, `2026-11-01T08:00`), in the server timezone when no offset is given. `until` defaults to now. | |
| `sections` | Comma separated: `content`, `wanted`, `downloads`, `tasks`, `logs`, `health`, `moderation`, `integrity`, `storage` | all |
| `log_level` | Minimum log level: `WARNING`, `ERROR`, `CRITICAL` | `WARNING` |
| `limit` | Size of the "top" and "latest" lists, 1 to 100 | `10` |

A window can cover at most 366 days. Invalid parameters return `400` with `{"error": "..."}` for JSON requests; the page shows the default report with the error.

## Response

```json
{
  "generated_at": "2026-11-14T10:00:00Z",
  "since": "2026-11-13T10:00:00Z",
  "until": "2026-11-14T10:00:00Z",
  "min_log_level": "WARNING",
  "top_n": 10,
  "section_titles": {"content": "Content", "logs": "Server logs"},
  "attention": [
    {"severity": "error", "section": "health", "message": "Scheduler Database Pruner is unresponsive"},
    {"severity": "warning", "section": "downloads", "message": "3 downloads failed"}
  ],
  "sections": {
    "content": {"archives_added": 12, "galleries_added": 40, "...": "..."},
    "logs": {"total": 5, "by_level": {"ERROR": 2, "WARNING": 3}, "groups": [], "...": "..."}
  }
}
```

`attention` is ordered by severity (`error`, `warning`, `info`) and lists what to act on: error/warning logs, no running worker process, unresponsive or overdue schedulers, nearly full disks, failed or stuck downloads, failed tasks, new file-check issues, and pending submissions.

A section that fails to build is returned as `{"error": "<exception>"}`, with an `error` entry in `attention`; the other sections are still built.

### Sections

Counts are for the window unless the field name ends in `_now` or `_total`, or the section is marked as current state.

- **`content`**: archives added (count, size, image count, public, by origin), archives recycled, galleries added (by provider and status; "No metadata" galleries are counted apart in `galleries_added_no_metadata`), `EventLog` entries by action.
- **`wanted`**: wanted gallery matches found (`FoundGallery`) and the latest ones, wanted galleries created, wanted galleries still being searched.
- **`downloads`**: downloads created, completed (and size), failed (by provider and method, latest failures with the first line of the error), cancelled. Current state: queued and downloading now, oldest queued, queued for more than 6 hours (`stuck_queued`).
- **`tasks`**: worker tasks completed and failed (by type, latest failures). Current state: pending and processing tasks, captured monitored links still pending, failed links (total).
- **`logs`**: server logs (`StatusLog`) at or above `log_level`, by level, and grouped by logger and message (numbers and long hex ids are ignored when grouping, `variants` counts the distinct messages merged). `enabled` is false when `disable_sql_log` is set. `window_partially_pruned` is true when the oldest log kept is newer than `since`: older logs were probably pruned, and the counts may be incomplete.
- **`health`** (current state): worker processes alive and stale (no heartbeat for 60 seconds), schedulers with a `problem`: `unresponsive` (marked running, no heartbeat for 5 minutes) or `overdue` (last run older than 2 cycles plus 10 minutes), providers in a rate-limit cooldown.
- **`moderation`**: gallery submissions received and resolved (by status). Current state: pending submissions and the oldest one.
- **`integrity`**: archive file-check issues found (by kind, archives affected), and the last check run.
- **`storage`** (current state): disk usage of the media root and the existing download folders (one entry per filesystem, `warning` at 90% used), archive and gallery totals, last database pruner run.

Datetimes are ISO 8601 strings; sizes are in bytes.

## Scheduled emails

Each staff user can get this report by email on their own schedule, set up in "My report email" on the page (`AdminReportSubscription`): every N hours, daily, or weekly, at a time in their own time zone, with their own sections, minimum log level and list size. "Skip when nothing needs attention" doesn't send the email when `attention` is empty.

Each email covers the time since the end of that user's last report email (the first one covers one period), so a failed or skipped send isn't lost: the next one includes it. `window=last_report` shows the same window on the page.

The emails are sent by the `timed_admin_reporter` scheduler in the worker process, using the `mail_logging` SMTP settings:

```yaml
admin_report:
  enable: true
  check_interval_minutes: 15.0
```

A report is sent at most `check_interval_minutes` after its scheduled time. "Send my report now" makes the user's report due and wakes the scheduler. The outcome of the last run (sent, skipped, not sent and why, or failed) is shown in "My report email".
