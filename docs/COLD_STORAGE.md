# Archive Cold Storage

Moves rarely downloaded archives to a private bucket in S3-compatible object storage, to free local disk space.
Downloads of those archives are redirected to short-lived presigned URLs, so the bucket serves them (Range requests
included) instead of the web server.

It suits a library that is mostly read: archives are written once, and the most recent ones get most of the downloads.
Keep the recent archives (and the thumbnails, extracted images and database) on the local disk, and offload the old
ones.

Any S3-compatible provider works. Pick one without egress fees, since the bucket serves the downloads, for example:

| Provider                          | Storage           | Egress                              |
|-----------------------------------|-------------------|-------------------------------------|
| OVHcloud Object Storage (Standard)| ~€7 / TB / month  | free                                |
| Backblaze B2                      | ~$7 / TB / month  | free up to 3× the stored volume     |

Avoid providers that bill egress per GB (AWS S3, GCS, Azure) for this use.

## How it works

- An offloaded archive's file name moves under `cold_storage.local_prefix` (default `galleries/cold`):
  `galleries/hath_dls/a.zip` becomes `galleries/cold/galleries/hath_dls/a.zip`. The name alone says an archive is
  offloaded, there is no extra database column.
- The object key is `<key_prefix><original name>`, e.g. `archives/galleries/hath_dls/a.zip`, so restoring puts the
  file back at its original path.
- `/archive/<id>/download/` (and `/ext-download/`) answer with a `302` to a presigned URL for offloaded archives, after
  the usual public/login/token checks. The URL sets the download file name and content type, like local downloads.
  Private archives stay private: the bucket is private and the URLs expire (`presigned_url_expiry`, 6 hours by
  default).
- Local archives are served exactly as before.

## Setup

1. Create a **private** bucket and an access key with read/write access to it.
   - OVH: Public Cloud → Object Storage → create an S3 bucket (Standard class), and an S3 user with
     read/write rights on it. The endpoint is `https://s3.<region>.io.cloud.ovh.net` and the region e.g. `gra`.
2. Configure the `cold_storage` section of `settings.yaml`:

   ```yaml
   cold_storage:
     enable: true
     endpoint_url: https://s3.gra.io.cloud.ovh.net
     region: gra
     bucket: my-panda-archives
     access_key_id: ...        # or env COLD_STORAGE_ACCESS_KEY_ID
     secret_access_key: ...    # or env COLD_STORAGE_SECRET_ACCESS_KEY
     key_prefix: archives/
     local_prefix: galleries/cold   # don't change it once archives are offloaded
   ```

   The environment variables `COLD_STORAGE_ENABLE`, `COLD_STORAGE_ENDPOINT_URL`, `COLD_STORAGE_BUCKET`,
   `COLD_STORAGE_ACCESS_KEY_ID` and `COLD_STORAGE_SECRET_ACCESS_KEY` override the file.
3. Restart, then check the bucket and credentials:

   ```bash
   python manage.py archive_cold_storage status --verify
   ```

   It writes, reads (also through a presigned URL), and deletes a small test object.

## Offloading

```bash
# What would be offloaded: archives added more than a year ago, oldest first, up to 500 GiB.
python manage.py archive_cold_storage offload --older-than-days 365 --max-total-gb 500 --dry-run

# Do it.
python manage.py archive_cold_storage offload --older-than-days 365 --max-total-gb 500
```

For each archive it uploads the file (multipart for files larger than `part_size_mb`, every part checked with its
MD5 and SHA-256 by the server), checks the object's size, points the archive at the cold name and deletes the local
file. An archive is only switched if its file name didn't change during the upload, and a failed upload leaves it
untouched. Archives without a CRC32 (still downloading or not processed) and archives whose file is marked as deleted
are skipped. Running it again continues where it left off.

Selection options (combinable):

| Option                              | Meaning                                                                 |
|-------------------------------------|-------------------------------------------------------------------------|
| `--pk 1 2 3`, `--pk-file ids.txt`   | Only these archives. The file has one id per line, `#` comments allowed. |
| `--older-than-days N`               | Added more than N days ago.                                              |
| `--path-prefix galleries/hath_dls/` | File name starts with this (repeatable).                                 |
| `--exclude-path-prefix P`           | Skip file names starting with this (repeatable).                         |
| `--min-size-mb N`                   | At least N MiB (uncompressed images size).                               |
| `--only-private`                    | Only archives that aren't public.                                        |
| `--order oldest\|largest`           | Which go first (default `oldest`).                                       |
| `--limit N`, `--max-total-gb N`     | Stop after N archives / N GiB.                                           |
| `--keep-local`                      | Don't delete the local files.                                            |
| `--dry-run`, `--stop-on-error`      |                                                                          |

`--pk-file` makes it easy to offload by actual traffic, for example the archives that weren't downloaded in the last
months according to the web server logs.

Each offload and restore is recorded in the event log (`COLD_STORAGE_OFFLOAD`, `COLD_STORAGE_RESTORE`).

## From the archive manager

While the cold storage is enabled and configured, staff users get these tools in `/archive-manager/` (other users
don't see them, and the backend refuses them):

- **Offload to cold storage** / **Restore from cold storage** in the actions panel, for the selected archives or
  "Run for all", and per archive in the expanded view. They queue one worker task per archive
  (`cold_storage_offload_worker`, `cold_storage_restore_worker`), following the operation reason. Archives that don't
  need it are skipped: already offloaded or not offloaded, no file, still downloading (no CRC32), file deleted.
  The page follows the queued tasks with its progress notice, like the other background actions.
- A **Storage** filter (Any / Local / Cold storage), and a "Cold" badge on offloaded archives.

The worker runs the same steps as the command and records the same event log entries, with the user and reason. One
offload and one restore run at a time, so a large batch doesn't hold up other tasks. Failures show up as failed tasks
in the workers page, with the error. The command is still the better tool for the initial bulk move: it can pick
archives by age, path or size and stop after a total size.

## Restoring and checking

```bash
python manage.py archive_cold_storage restore --pk 123 456   # or --pk-file, or --all
python manage.py archive_cold_storage check                  # HEAD every offloaded archive, list missing ones
python manage.py archive_cold_storage status                 # configuration and counts
```

`restore` downloads to `<path>.cold-part`, checks the size, moves it into place, points the archive back at it and
deletes the object (`--keep-remote` keeps it).

## What needs the file

Downloads, publishing/unpublishing, deleting (the object is deleted from the bucket) and the "missing files" listings
work without access to the file. Everything that opens the zip itself does not: extracting, page view/download of a
single image from the zip, thumbnails, image hashing, recalculating file info, cloning, splitting, the archive file
check, and the folder crawler's rematching. For those, either restore the archive first, or mount the bucket read-only
at `MEDIA_ROOT/<local_prefix>`, so the cold file names resolve to the bucket:

```bash
rclone mount ovh-cold:my-panda-archives/archives /home/panda/media/galleries/cold \
    --read-only --vfs-cache-mode full --vfs-cache-max-size 100G --dir-cache-time 1h --daemon
```

With the mount in place, `cold_storage.redirect_downloads: false` serves offloaded archives through
`X-Accel-Redirect` like local ones (through the mount, so the server's bandwidth is used again).

Keep in mind that over a mount, tools that read the whole file (a file check with CRC32, rematching) download it from
the bucket. Avoid running them over all offloaded archives.

Other notes:

- Renaming the file of an offloaded archive is refused; restore it first.
- Deleting the file of an offloaded archive needs the cold storage to be configured: if the bucket can't be reached,
  nothing is deleted and an error is raised, so the archive isn't marked as deleted while its file is still stored.
- `foldercrawler --remove-missing-files` and `--display-missing-files` skip offloaded archives, so an unmounted bucket
  doesn't look like missing files.
- While `cold_storage.enable` is false, nothing is treated as offloaded (file names under `local_prefix` are ordinary
  local files). So don't disable it while archives are offloaded: `restore --all` first.
- With `urls.external_as_main_download`, offloaded archives are still served from the bucket, since the external
  media server only has the local files.
