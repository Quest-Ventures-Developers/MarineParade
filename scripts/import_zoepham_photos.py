#!/usr/bin/env python3
"""Google Drive -> local importer for Marine Parade Zoe Pham photos.

Features:
- Recursively scans Drive folder tree and detects leaf folders with images
- Imports only new event folders (stateful)
- Downloads images safely with pagination
- Resizes images by configurable percentage while preserving format when possible
- Generates HTML report and optionally sends via Resend API
- Supports dry-run and local fake simulation mode for testing
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import html
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageOps

try:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaIoBaseDownload
    from google.oauth2 import service_account
except Exception:  # pragma: no cover - handled by import-check test
    build = None
    HttpError = Exception
    MediaIoBaseDownload = None
    service_account = None


IMAGE_MIME_PREFIX = "image/"
FOLDER_MIME = "application/vnd.google-apps.folder"
SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclasses.dataclass
class Config:
    from_folder: str
    to_folder: Path
    temp_folder: Path
    credentials_json: Path
    state_file: Path
    timezone: str
    resize_percent: int
    dry_run: bool
    overwrite_existing: bool
    email_to: list[str]
    resend_api_key: str | None
    resend_from: str
    log_level: str
    crawl_limit: int | None
    run_mode: str
    fake_drive_root: Path | None
    summary_file: Path


class DriveClient:
    def find_folder_id(self, folder_name: str) -> str:
        raise NotImplementedError

    def list_children(self, folder_id: str) -> list[dict[str, str]]:
        raise NotImplementedError

    def get_folder_name(self, folder_id: str) -> str:
        raise NotImplementedError

    def list_images(self, folder_id: str) -> list[dict[str, str]]:
        raise NotImplementedError

    def download_file(self, file_id: str, output: Path) -> None:
        raise NotImplementedError


class GoogleDriveClient(DriveClient):
    def __init__(self, credentials_json: Path) -> None:
        if build is None or service_account is None or MediaIoBaseDownload is None:
            raise RuntimeError("Google API libraries are not available")

        creds = service_account.Credentials.from_service_account_file(
            str(credentials_json), scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
        self.service = build("drive", "v3", credentials=creds, cache_discovery=False)

    def _paged_list(self, query: str, fields: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        token = None
        while True:
            resp = (
                self.service.files()
                .list(
                    q=query,
                    spaces="drive",
                    fields=f"nextPageToken, files({fields})",
                    pageToken=token,
                    pageSize=1000,
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                )
                .execute()
            )
            items.extend(resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                break
        return items

    def find_folder_id(self, folder_name: str) -> str:
        safe_name = folder_name.replace("'", "\\'")
        q = (
            f"name='{safe_name}' and "
            f"mimeType='{FOLDER_MIME}' and trashed=false"
        )
        files = self._paged_list(q, "id,name")
        if not files:
            raise RuntimeError(f"Folder not found: {folder_name}")
        return files[0]["id"]

    def list_children(self, folder_id: str) -> list[dict[str, str]]:
        q = f"'{folder_id}' in parents and trashed=false"
        return self._paged_list(q, "id,name,mimeType")

    def get_folder_name(self, folder_id: str) -> str:
        meta = self.service.files().get(fileId=folder_id, fields="name", supportsAllDrives=True).execute()
        return meta["name"]

    def list_images(self, folder_id: str) -> list[dict[str, str]]:
        q = f"'{folder_id}' in parents and mimeType contains '{IMAGE_MIME_PREFIX}' and trashed=false"
        return self._paged_list(q, "id,name,mimeType")

    def download_file(self, file_id: str, output: Path) -> None:
        request = self.service.files().get_media(fileId=file_id, supportsAllDrives=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()


class FakeDriveClient(DriveClient):
    """Local filesystem-backed fake drive client for deterministic tests.

    Directory convention:
    fake_root/
      01 Events/
        Event A/
          img1.jpg
        Parent/
          Child Event/
            img2.png
    """

    def __init__(self, fake_root: Path) -> None:
        self.root = fake_root.resolve()
        self.id_to_path: dict[str, Path] = {}
        self.path_to_id: dict[Path, str] = {}
        self._index_tree()

    def _index_tree(self) -> None:
        counter = 1
        for p in sorted(self.root.rglob("*")):
            if not p.exists():
                continue
            key = f"f{counter}"
            counter += 1
            rp = p.resolve()
            self.id_to_path[key] = rp
            self.path_to_id[rp] = key
        root_id = "root"
        self.id_to_path[root_id] = self.root
        self.path_to_id[self.root] = root_id

    def _id_for(self, p: Path) -> str:
        rp = p.resolve()
        if rp in self.path_to_id:
            return self.path_to_id[rp]
        nid = f"f{len(self.id_to_path)+1}"
        self.id_to_path[nid] = rp
        self.path_to_id[rp] = nid
        return nid

    def find_folder_id(self, folder_name: str) -> str:
        matches = [p for p in self.root.iterdir() if p.is_dir() and p.name == folder_name]
        if not matches:
            raise RuntimeError(f"Folder not found in fake drive: {folder_name}")
        return self._id_for(matches[0])

    def list_children(self, folder_id: str) -> list[dict[str, str]]:
        p = self.id_to_path[folder_id]
        out: list[dict[str, str]] = []
        for child in sorted(p.iterdir()):
            if child.is_dir():
                mime = FOLDER_MIME
            else:
                mime = _mime_from_suffix(child.suffix)
            out.append({"id": self._id_for(child), "name": child.name, "mimeType": mime})
        return out

    def get_folder_name(self, folder_id: str) -> str:
        return self.id_to_path[folder_id].name

    def list_images(self, folder_id: str) -> list[dict[str, str]]:
        p = self.id_to_path[folder_id]
        out: list[dict[str, str]] = []
        for child in sorted(p.iterdir()):
            if child.is_file() and child.suffix.lower() in SUPPORTED_EXTS:
                out.append(
                    {
                        "id": self._id_for(child),
                        "name": child.name,
                        "mimeType": _mime_from_suffix(child.suffix),
                    }
                )
        return out

    def download_file(self, file_id: str, output: Path) -> None:
        src = self.id_to_path[file_id]
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, output)


def _mime_from_suffix(suffix: str) -> str:
    s = suffix.lower()
    if s in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if s == ".png":
        return "image/png"
    if s == ".webp":
        return "image/webp"
    return "application/octet-stream"


def sanitize_name(name: str, fallback: str = "event") -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_str = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_str = ascii_str.replace("'", "-")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_str).strip(".-_").lower()
    return slug or fallback


def safe_join(base: Path, *parts: str) -> Path:
    candidate = base.joinpath(*parts).resolve()
    base_resolved = base.resolve()
    if not str(candidate).startswith(str(base_resolved) + os.sep) and candidate != base_resolved:
        raise ValueError(f"Path traversal prevented: {candidate}")
    return candidate


def unique_dest_path(dest_dir: Path, stem: str, suffix: str, discriminator: str) -> Path:
    base = safe_join(dest_dir, f"{stem}{suffix}")
    if not base.exists():
        return base
    short = sanitize_name(discriminator, "id")[:12]
    return safe_join(dest_dir, f"{stem}-{short}{suffix}")


def resize_image(src: Path, dest: Path, resize_percent: int) -> tuple[bool, str]:
    """Resize image while preserving source format when possible."""
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            new_w = max(1, int(im.width * resize_percent / 100.0))
            new_h = max(1, int(im.height * resize_percent / 100.0))
            if new_w == im.width and new_h == im.height:
                shutil.copy2(src, dest)
                return True, "copied (no resize)"

            resized = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
            fmt = (im.format or "").upper()
            save_kwargs: dict[str, Any] = {}

            if fmt in {"JPEG", "JPG"}:
                if resized.mode in ("RGBA", "LA", "P"):
                    resized = resized.convert("RGB")
                save_kwargs.update({"quality": 90, "optimize": True, "progressive": True})
                out_format = "JPEG"
            elif fmt == "PNG":
                out_format = "PNG"
                save_kwargs.update({"optimize": True})
            elif fmt == "WEBP":
                out_format = "WEBP"
                save_kwargs.update({"quality": 90, "method": 6})
            else:
                # Best-effort fallback preserving extension intent.
                ext = dest.suffix.lower()
                out_format = {
                    ".jpg": "JPEG",
                    ".jpeg": "JPEG",
                    ".png": "PNG",
                    ".webp": "WEBP",
                }.get(ext, "JPEG")
                if out_format == "JPEG" and resized.mode in ("RGBA", "LA", "P"):
                    resized = resized.convert("RGB")
                save_kwargs.update({"quality": 90})

            resized.save(dest, format=out_format, **save_kwargs)
            return True, f"resized to {new_w}x{new_h}"
    except Exception as exc:
        try:
            shutil.copy2(src, dest)
            return True, f"copied fallback after resize error: {exc}"
        except Exception as copy_exc:
            return False, f"resize/copy failed: {copy_exc}"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def find_leaf_event_folders(drive: DriveClient, root_id: str, crawl_limit: int | None = None) -> dict[str, str]:
    event_folders: dict[str, str] = {}
    visited = 0

    def _walk(folder_id: str) -> None:
        nonlocal visited
        if crawl_limit is not None and visited >= crawl_limit:
            return
        visited += 1

        children = drive.list_children(folder_id)
        subfolders = [c for c in children if c.get("mimeType") == FOLDER_MIME]
        has_images = any(str(c.get("mimeType", "")).startswith(IMAGE_MIME_PREFIX) for c in children)

        if has_images and not subfolders:
            event_folders[folder_id] = drive.get_folder_name(folder_id)

        for sub in subfolders:
            _walk(sub["id"])

    _walk(root_id)
    return event_folders


def build_report_html(
    *,
    imported: list[dict[str, Any]],
    failures: list[str],
    tz_label: str,
    now_text: str,
    dry_run: bool,
) -> str:
    mode_badge = " <span style='color:#b54708'>(DRY RUN)</span>" if dry_run else ""
    lines = [
        "<div style='font-family:Arial,sans-serif'>",
        f"<h2>NEW PHOTOS IMPORTED - {html.escape(now_text)} ({html.escape(tz_label)}){mode_badge}</h2>",
    ]

    if imported:
        lines.append("<ol>")
        total = 0
        for item in imported:
            total += int(item.get("photos", 0))
            lines.append(
                f"<li><b>{html.escape(str(item['name']))}</b> - {int(item['photos'])} new photos"
                f" <small>({html.escape(str(item['imported_at']))})</small></li>"
            )
        lines.append("</ol>")
        lines.append(f"<p><b>TOTAL:</b> {total} photos from {len(imported)} event(s)</p>")
    else:
        lines.append("<p>No new event folders found.</p>")

    if failures:
        lines.append("<h3>Failure Summary</h3><ul>")
        for err in failures:
            lines.append(f"<li>{html.escape(err)}</li>")
        lines.append("</ul>")

    lines.append("</div>")
    return "\n".join(lines)


def send_resend_email(api_key: str, sender: str, recipients: list[str], subject: str, html_body: str) -> None:
    payload = {
        "from": sender,
        "to": recipients,
        "subject": subject,
        "html": html_body,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    resp = requests.post("https://api.resend.com/emails", headers=headers, json=payload, timeout=30)
    if resp.status_code >= 300:
        raise RuntimeError(f"Resend API failed ({resp.status_code}): {resp.text}")


def validate_destination(path: Path) -> None:
    # Avoid accidental root-level or dangerous write targets.
    if str(path) in {"/", ""}:
        raise ValueError("Invalid destination folder")
    path.mkdir(parents=True, exist_ok=True)


def parse_env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}


def build_config(args: argparse.Namespace) -> Config:
    to_folder = Path(args.to_folder or os.getenv("TO_FOLDER", "./export/zoepham"))
    temp_folder = Path(args.temp_folder or os.getenv("TEMP_FOLDER", "./tmp/downloads"))
    credentials_json = Path(args.credentials_json or os.getenv("GOOGLE_CREDENTIALS_JSON", "./credentials.json"))
    state_file = Path(args.state_file or os.getenv("STATE_FILE", "./zoepham_imported_state.json"))
    summary_file = Path(os.getenv("SUMMARY_FILE", "./zoepham_import_summary.json"))
    email_raw = args.email_to or os.getenv("EMAIL_TO", "")
    email_to = [x.strip() for x in email_raw.split(",") if x.strip()]

    resize_percent = int(args.resize_percent if args.resize_percent is not None else os.getenv("RESIZE_PERCENT", "70"))
    if resize_percent <= 0 or resize_percent > 100:
        raise ValueError("RESIZE_PERCENT must be in range 1..100")

    return Config(
        from_folder=args.from_folder or os.getenv("FROM_FOLDER", "01 Events"),
        to_folder=to_folder,
        temp_folder=temp_folder,
        credentials_json=credentials_json,
        state_file=state_file,
        timezone=args.timezone or os.getenv("TIMEZONE", "Asia/Ho_Chi_Minh"),
        resize_percent=resize_percent,
        dry_run=args.dry_run or parse_env_bool("DRY_RUN", False),
        overwrite_existing=parse_env_bool("OVERWRITE_EXISTING", False),
        email_to=email_to,
        resend_api_key=os.getenv("RESEND_API_KEY"),
        resend_from=os.getenv("RESEND_FROM", "NextGen Gallery Bot <no-reply@marineparade.sg>"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        crawl_limit=(
            int(os.getenv("CRAWL_LIMIT", "0")) if os.getenv("CRAWL_LIMIT") and os.getenv("CRAWL_LIMIT") != "0" else None
        ),
        run_mode=os.getenv("RUN_MODE", "live").strip().lower(),
        fake_drive_root=Path(os.getenv("FAKE_DRIVE_ROOT")).resolve() if os.getenv("FAKE_DRIVE_ROOT") else None,
        summary_file=summary_file,
    )


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def run_import(cfg: Config) -> int:
    setup_logging(cfg.log_level)
    logging.info("START scanning '%s'", cfg.from_folder)
    validate_destination(cfg.to_folder)
    cfg.temp_folder.mkdir(parents=True, exist_ok=True)

    if cfg.run_mode == "fake":
        if not cfg.fake_drive_root:
            raise ValueError("FAKE_DRIVE_ROOT is required when RUN_MODE=fake")
        drive: DriveClient = FakeDriveClient(cfg.fake_drive_root)
    else:
        if not cfg.credentials_json.exists():
            raise FileNotFoundError(f"Missing credentials file: {cfg.credentials_json}")
        drive = GoogleDriveClient(cfg.credentials_json)

    processed = load_state(cfg.state_file)
    failures: list[str] = []

    root_id = drive.find_folder_id(cfg.from_folder)
    logging.info("Root folder found: %s", root_id)

    event_folders = find_leaf_event_folders(drive, root_id, cfg.crawl_limit)
    logging.info("Total event folders found: %d", len(event_folders))

    imported_items: list[dict[str, Any]] = []
    total_added = 0

    for folder_id, event_name in event_folders.items():
        if folder_id in processed:
            logging.info("Skipping already processed event: %s", event_name)
            continue

        slug = sanitize_name(event_name, "event")
        try:
            dest_dir = safe_join(cfg.to_folder, slug)
            temp_dir = safe_join(cfg.temp_folder, f"tmp_{slug}")
        except ValueError as exc:
            failures.append(f"{event_name}: unsafe path ({exc})")
            continue

        dest_dir.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        logging.info("NEW EVENT -> %s", event_name)

        photos = drive.list_images(folder_id)
        added = 0
        for photo in photos:
            original_name = str(photo["name"])
            stem = sanitize_name(Path(original_name).stem, "photo")
            suffix = Path(original_name).suffix.lower()
            if suffix not in SUPPORTED_EXTS:
                # Only process supported extensions; still skip safely.
                continue

            try:
                dest_path = unique_dest_path(dest_dir, stem, suffix, str(photo["id"]))
                tmp_path = safe_join(temp_dir, dest_path.name)
            except ValueError as exc:
                failures.append(f"{event_name}/{original_name}: unsafe filename ({exc})")
                continue

            if dest_path.exists() and not cfg.overwrite_existing:
                logging.info("Already exists, skip: %s", dest_path.name)
                continue

            try:
                if cfg.dry_run:
                    logging.info("DRY RUN: would download %s", original_name)
                    added += 1
                    continue

                drive.download_file(photo["id"], tmp_path)
                ok, detail = resize_image(tmp_path, dest_path, cfg.resize_percent)
                if ok:
                    added += 1
                    logging.info("Imported %s (%s)", original_name, detail)
                else:
                    failures.append(f"{event_name}/{original_name}: {detail}")
            except HttpError as exc:
                failures.append(f"{event_name}/{original_name}: Drive error {exc}")
            except Exception as exc:
                failures.append(f"{event_name}/{original_name}: {exc}")
            finally:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    pass

        try:
            temp_dir.rmdir()
        except OSError:
            pass

        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        event_record = {
            "name": event_name,
            "slug": slug,
            "imported_at": now,
            "photos": added,
        }
        if not cfg.dry_run:
            processed[folder_id] = event_record
        imported_items.append(event_record)
        total_added += added
        logging.info("DONE -> %s photos", added)

    if cfg.dry_run:
        logging.info("DRY RUN: state file not updated")
    elif imported_items:
        save_state(cfg.state_file, processed)
    else:
        logging.info("No state changes to persist")

    now_text = dt.datetime.now().strftime("%d %b %Y %H:%M")
    subject = f"NEW PHOTOS IMPORTED - {dt.datetime.now().strftime('%d %b %Y')}"
    report_html = build_report_html(
        imported=imported_items,
        failures=failures,
        tz_label="GMT+7",
        now_text=now_text,
        dry_run=cfg.dry_run,
    )

    if not imported_items:
        logging.info("No new event folders found today.")

    if imported_items and cfg.email_to:
        if cfg.dry_run:
            logging.info("DRY RUN: would send email to %s", ", ".join(cfg.email_to))
        elif cfg.resend_api_key:
            send_resend_email(cfg.resend_api_key, cfg.resend_from, cfg.email_to, subject, report_html)
            logging.info("EMAIL SENT")
        else:
            logging.warning("Email recipients configured but RESEND_API_KEY is missing; skipping email")

    if failures:
        logging.warning("Completed with %d failure(s)", len(failures))
        for failure in failures:
            logging.warning("Failure: %s", failure)

    logging.info(
        "ALL DONE. Imported events=%d, total photos=%d, failures=%d",
        len(imported_items),
        total_added,
        len(failures),
    )
    summary = {
        "imported_events": len(imported_items),
        "total_photos": total_added,
        "failures": len(failures),
        "dry_run": cfg.dry_run,
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
    }
    cfg.summary_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.summary_file.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Zoe Pham photos from Google Drive")
    parser.add_argument("--from-folder", help="Source folder name in Drive")
    parser.add_argument("--to-folder", help="Local export folder")
    parser.add_argument("--temp-folder", help="Temp folder for downloads")
    parser.add_argument("--credentials-json", help="Path to Google service account JSON")
    parser.add_argument("--state-file", help="Path to state JSON file")
    parser.add_argument("--timezone", help="Timezone label")
    parser.add_argument("--resize-percent", type=int, help="Resize percentage (1..100)")
    parser.add_argument("--email-to", help="Comma-separated recipients")
    parser.add_argument("--dry-run", action="store_true", help="Do not write/import or send emails")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args = parse_args(argv)
    cfg = build_config(args)
    return run_import(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
