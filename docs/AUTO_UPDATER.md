# Auto Updater Schedules

`/auto-updater/` (Manage menu, staff only) sets up when the metadata (tags, title, rating…) of each provider's
galleries is refreshed. Tags are added by users, mostly in the first days after a gallery is posted, so the schedule is
by **gallery age**: the time since the gallery was posted. Each provider that can update its galleries by link gets at
most one **schedule**.

## Rules

A schedule is a list of rules, each one a set of ages to update at:

| Rule | Updates at |
|---|---|
| `{"from": "6h", "to": "1d", "every": "6h"}` | 6h, 12h, 18h, 1d |
| `{"from": "1d12h", "to": "3d", "every": "12h"}` | 1d12h, 2d, 2d12h, 3d |
| `{"from": "30d", "to": "30d"}` | 30d (once) |

Ages are written `30m`, `6h`, `2d`, `1d12h`, `1w` (a plain number is hours). A rule updates at its `from` age, then
every `every` while it doesn't pass `to`. Rules can overlap: each age counts once. `every` is at least 15 minutes, ages
go up to 3650 days, and a schedule has at most 500 updates per gallery.

The editor shows the rules on a timeline (logarithmic, so the first hours get as much room as the following weeks):

- the bars are the changes the gallery change log recorded for the provider's galleries in the last 90 days, by how old
  the gallery was when an update found them: tag changes and other changes. Updates find them, so they follow the
  schedule that ran: ages without updates show nothing.
- a lane per rule: drag its ends to resize it, its body to move it, a single update to move it; double-click the
  timeline to add a single update. The table below has the exact values.
- the last lane has every update.

Below it: the updates per gallery, the posted dates covered right now and how many galleries that is, the updates per
day (the galleries posted per day over the last 30 days × the updates per gallery) and how many galleries a run now
would queue.

**Presets**: "Close to release" (every 6h the first day, every 12h until day 3, daily until a week, then 10, 14 and 30
days), "Light" (1, 3, 7 and 30 days), "First week, daily", and "Old settings" (the `autoupdater` settings' equivalent).

## Running

- The `auto_updater` scheduler (`autoupdater.enable`, `startup`) wakes up when the next active schedule is due (its
  last run plus `queue_every`), and at least every 15 minutes to see new or changed schedules. A forced run from the
  workers dashboard runs every active schedule.
- A run takes the galleries that reached one of the ages since the previous run: for an age `a` and the window from the
  previous run to now, the galleries posted between `previous - a` and `now - a`. They're queued as a
  `gallery_updater` task (rate limited provider requests, deduplicated with the pending tasks). The windows follow each
  other, so a gallery is queued once per age whatever `queue_every` is: it only sets how many galleries each task gets.
- The first run takes one `queue_every`. After a long stop, a run catches up one day at most.
- **Run now** (provider page) and **Run selected now** (list) run like the scheduler. **Update whole range now** queues
  every gallery between the first and last age once, without moving the window.
- Each run saves on the schedule when it ran, whether it went fine, how many galleries it queued and the task.

## Export and import

**Export** downloads the schedules as JSON; **Import** takes that file or pasted JSON, and creates or updates the
schedules by provider (fields left out keep their value). `?format=json` on the list also gives the update ages and the
last run.

```json
{
  "version": 1,
  "schedules": [
    {
      "provider": "panda",
      "active": true,
      "queue_every": "1h",
      "rules": [
        {"from": "6h", "to": "1d", "every": "6h"},
        {"from": "1d12h", "to": "3d", "every": "12h"},
        {"from": "4d", "to": "7d", "every": "1d"},
        {"from": "30d", "to": "30d"}
      ],
      "notes": ""
    }
  ]
}
```

## From the old configuration

The auto updater used to run every `autoupdater.cycle_timer` days for each provider in `autoupdater.providers`,
updating the galleries posted between `cycle_timer + buffer_back` and `buffer_after` days ago (or the provider's
`general.autoupdater_timer`, `autoupdater_buffer_back`, `autoupdater_buffer_after`, `autoupdater_enable`).

Those settings aren't read anymore. While a provider listed there has no schedule, the list page offers to **import
them as schedules**: an update every `cycle_timer` days from `buffer_after` to `cycle_timer + buffer_back` days of age
(`cycle_timer: 1`, `buffer_back: 5`, `buffer_after: 1` becomes `{"from": "1d", "to": "6d", "every": "1d"}`), active
when the provider's `autoupdater_enable` is. Until then nothing is updated, and the scheduler logs a warning.
