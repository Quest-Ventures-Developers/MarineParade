# Zoepham Google Drive Importer Migration (PHP -> Python + GitHub Actions)

This repository runs a production cron importer that replaces the legacy ICDSoft PHP job.

## Architecture

GitHub Actions (`.github/workflows/import-zoepham-photos.yml`) every 6 hours or manual dispatch:

1. Authenticates to Google Drive with a service account.
2. Runs `scripts/import_zoepham_photos.py`.
3. Recursively scans `01 Events` for **leaf folders containing images**.
4. Imports only unprocessed event folders (state tracked in `zoepham_imported_state.json`).
5. Downloads images, resizes using Pillow (configurable, default 70%), preserves format when possible (`jpg/png/webp`).
6. Sends an HTML summary email through Resend.
7. Rsyncs exported files to WordPress target directory over SSH.
8. Commits updated state JSON back to the repository.

## Files

- `.github/workflows/import-zoepham-photos.yml`
- `scripts/import_zoepham_photos.py`
- `requirements.txt`
- `.env.example`
- `.gitignore`

## Local Setup

1. Create a venv and install dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Copy `.env.example` values into your environment (or `.env` + your loader).
3. Put Google service account JSON at `./credentials.json` (or set `GOOGLE_CREDENTIALS_JSON`).
4. Run dry-run:
   ```bash
   DRY_RUN=true python scripts/import_zoepham_photos.py
   ```

## Importer Configuration

Environment variables:

- `FROM_FOLDER` default `01 Events`
- `GOOGLE_CREDENTIALS_JSON` default `./credentials.json`
- `TO_FOLDER` default `./export/zoepham`
- `TEMP_FOLDER` default `./tmp/downloads`
- `STATE_FILE` default `./zoepham_imported_state.json`
- `RESIZE_PERCENT` default `70` (1..100)
- `DRY_RUN` default `false`
- `OVERWRITE_EXISTING` default `false`
- `TIMEZONE` default `Asia/Ho_Chi_Minh`
- `EMAIL_TO` comma-separated recipients
- `RESEND_FROM` email sender
- `RESEND_API_KEY` Resend API key
- `LOG_LEVEL` default `INFO`
- `CRAWL_LIMIT` optional crawl limiter for diagnostics
- `RUN_MODE` `live` or `fake`
- `FAKE_DRIVE_ROOT` required if `RUN_MODE=fake`

## GitHub Secrets Required

Set these in repository Settings -> Secrets and variables -> Actions:

- `GOOGLE_SERVICE_ACCOUNT_JSON`: service account JSON content (full JSON string)
- `RESEND_API_KEY`: Resend API token
- `WP_SSH_PRIVATE_KEY`: private SSH key used for deploy
- `WP_SSH_KNOWN_HOSTS`: known_hosts entry for destination host
- `WP_SSH_USER`: SSH username
- `WP_SSH_HOST`: SSH hostname
- `WP_TARGET_DIR`: remote absolute path (example `/home/marineparade/www/www/wp-content/zoepham`)

## GitHub Repo Settings

- Workflow permissions: `Read and write` for `GITHUB_TOKEN` (to commit state updates).
- Branch protection: allow GitHub Actions to push state updates on selected branch, or use dedicated automation branch + PR flow.

## Deployment Steps

1. Push all files to default branch.
2. Add required secrets.
3. Run `Import Zoepham Photos` via `workflow_dispatch` once.
4. Verify:
   - workflow logs
   - files uploaded to WordPress destination
   - email received
   - `zoepham_imported_state.json` committed
5. Keep scheduled cron active (`0 */6 * * *`).

## Rollback

1. Disable GitHub Actions workflow schedule.
2. Re-enable old PHP cron on ICDSoft (if still available).
3. Restore previous state file backup if needed.
4. Remove/revoke new service account and SSH credentials if migration is aborted.

## Troubleshooting

- `Folder not found`: verify `FROM_FOLDER` exists and service account has access to shared drive/folder.
- `Missing credentials file`: check `GOOGLE_SERVICE_ACCOUNT_JSON` secret and write step.
- `Resend API failed`: verify `RESEND_API_KEY`, sender domain, and recipient policy.
- `Rsync permission denied`: verify SSH key, user, host, and `WP_TARGET_DIR` ownership.
- Duplicate behavior: ensure state file is committed and not ignored.
- Image import failures: inspect workflow logs failure summary for file-specific errors.

## Security Notes

- No secrets are hardcoded.
- Path traversal is blocked in importer path joins.
- Destination path is validated.
- Workflow validates remote target prefix before rsync.
- SSH host key pinning is required (`StrictHostKeyChecking=yes`).
- `credentials.json` is deleted at workflow end.
