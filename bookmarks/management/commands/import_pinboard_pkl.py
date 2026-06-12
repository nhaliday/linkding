import datetime
import logging
import math
from dataclasses import dataclass

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from bookmarks.models import Bookmark, Tag, sanitize_tag_name
from bookmarks.services import tasks
from bookmarks.services.importer import TagCache
from bookmarks.utils import normalize_url

logger = logging.getLogger(__name__)

BATCH_SIZE = 200
MAX_TAG_LENGTH = 64


class _DryRunRollback(Exception):
    pass


@dataclass
class _Result:
    total: int = 0
    created: int = 0
    skipped: int = 0
    failed: int = 0
    tags_created: int = 0


class Command(BaseCommand):
    help = "Import a Pinboard pandas DataFrame pickle (pin.pkl) into linkding."

    def add_arguments(self, parser):
        parser.add_argument("pkl_path", type=str, help="Path to the .pkl file")
        parser.add_argument(
            "username", type=str, help="Owner of the imported bookmarks"
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Run the import inside a transaction and roll back at the end",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Only process the first N rows",
        )

    def handle(self, *args, **opts):
        try:
            import pandas as pd
        except ImportError as e:
            raise CommandError(
                "pandas is required to read the pickle. Re-run with: "
                "uv run --with pandas manage.py import_pinboard_pkl ..."
            ) from e

        pkl_path = opts["pkl_path"]
        username = opts["username"]
        dry_run = opts["dry_run"]
        limit = opts["limit"]

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist as e:
            raise CommandError(f"User {username!r} does not exist") from e

        self.stdout.write(f"Reading {pkl_path} ...")
        df = pd.read_pickle(pkl_path)
        if limit is not None:
            df = df.head(limit)
        rows = list(df.to_dict(orient="records"))
        self.stdout.write(f"Loaded {len(rows)} rows.")

        result = _Result(total=len(rows))

        try:
            with transaction.atomic():
                tag_cache, result.tags_created = self._ensure_tags(rows, user)
                for batch_index, start in enumerate(range(0, len(rows), BATCH_SIZE)):
                    batch = rows[start : start + BATCH_SIZE]
                    self._import_batch(batch, user, tag_cache, result)
                    self.stdout.write(
                        f"  batch {batch_index + 1}/"
                        f"{math.ceil(len(rows) / BATCH_SIZE)}: "
                        f"created={result.created} "
                        f"skipped={result.skipped} "
                        f"failed={result.failed}"
                    )
                if dry_run:
                    raise _DryRunRollback()
        except _DryRunRollback:
            self.stdout.write(self.style.WARNING("Dry run — all DB writes rolled back."))

        if not dry_run and result.created > 0:
            tasks.schedule_bookmarks_without_favicons(user)
            tasks.schedule_bookmarks_without_previews(user)

        self._print_summary(result)

    def _ensure_tags(self, rows: list[dict], user: User) -> tuple[TagCache, int]:
        cache = TagCache(user)
        to_create: list[Tag] = []
        seen: set[str] = set()
        now = timezone.now()

        for row in rows:
            for name in _row_tag_names(row):
                if len(name) > MAX_TAG_LENGTH:
                    logger.warning(
                        f"Ignoring tag {name!r} (length {len(name)}) — exceeds {MAX_TAG_LENGTH} chars"
                    )
                    continue
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                if cache.get(name) is None:
                    tag = Tag(name=name, owner=user, date_added=now)
                    to_create.append(tag)
                    cache.put(tag)

        if to_create:
            Tag.objects.bulk_create(to_create)
        return cache, len(to_create)

    def _import_batch(
        self,
        batch: list[dict],
        user: User,
        tag_cache: TagCache,
        result: _Result,
    ):
        batch_urls = [_clean_str(row.get("href")) for row in batch]

        # Existing bookmarks for this user that match anything in the batch
        existing = set(
            Bookmark.objects.filter(owner=user, url__in=batch_urls).values_list(
                "url", flat=True
            )
        )
        # Also check normalized URL collisions one-by-one (cheap; ~200 rows)
        rows_to_create: list[tuple[dict, Bookmark]] = []
        for row in batch:
            url = _clean_str(row.get("href"))
            if not url:
                result.failed += 1
                continue
            if url in existing or Bookmark.query_existing(user, url).exists():
                result.skipped += 1
                continue
            try:
                bookmark = _build_bookmark(row, user)
                bookmark.clean_fields(exclude=["owner"])
            except Exception:
                logger.exception(f"Failed to build bookmark for url={url!r}")
                result.failed += 1
                continue
            rows_to_create.append((row, bookmark))

        if not rows_to_create:
            return

        Bookmark.objects.bulk_create([b for _, b in rows_to_create])
        result.created += len(rows_to_create)

        # Re-query to pick up auto-generated PKs, then assign tags.
        created_urls = [b.url for _, b in rows_to_create]
        by_url = {
            b.url: b
            for b in Bookmark.objects.filter(owner=user, url__in=created_urls)
        }

        Through = Bookmark.tags.through
        relationships = []
        for row, _ in rows_to_create:
            bookmark = by_url.get(_clean_str(row["href"]))
            if bookmark is None:
                continue
            for tag in tag_cache.get_all(_row_tag_names(row)):
                relationships.append(Through(bookmark_id=bookmark.id, tag_id=tag.id))
        if relationships:
            Through.objects.bulk_create(relationships, ignore_conflicts=True)

    def _print_summary(self, result: _Result):
        self.stdout.write("")
        self.stdout.write(f"Total rows:       {result.total}")
        self.stdout.write(self.style.SUCCESS(f"Created:          {result.created}"))
        self.stdout.write(self.style.WARNING(f"Skipped (exists): {result.skipped}"))
        if result.failed:
            self.stdout.write(self.style.ERROR(f"Failed:           {result.failed}"))
        else:
            self.stdout.write("Failed:           0")
        self.stdout.write(f"Tags created:     {result.tags_created}")


def _clean_str(value) -> str:
    if value is None:
        return ""
    try:
        # pandas NaN check without importing pandas here
        if value != value:  # noqa: PLR0124
            return ""
    except Exception:
        pass
    return str(value).strip()


def _row_tag_names(row: dict) -> list[str]:
    raw = row.get("taglist")
    names: list[str] = []
    if isinstance(raw, (list, tuple)):
        names = [str(t) for t in raw]
    else:
        tags_str = _clean_str(row.get("tags"))
        if tags_str:
            names = tags_str.split()
    cleaned = []
    seen = set()
    for name in names:
        name = sanitize_tag_name(name)
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
    return cleaned


def _build_bookmark(row: dict, user: User) -> Bookmark:
    url = _clean_str(row["href"])
    title = _clean_str(row.get("description"))
    notes = _clean_str(row.get("extended"))
    shared = _clean_str(row.get("shared")).lower() == "yes"
    unread = _clean_str(row.get("toread")).lower() == "yes"

    dt = row.get("datetime")
    if dt is None or (hasattr(dt, "__ne__") and dt != dt):
        date_added = timezone.now()
    else:
        if hasattr(dt, "to_pydatetime"):
            dt = dt.to_pydatetime()
        if dt.tzinfo is None:
            dt = timezone.make_aware(dt, datetime.UTC)
        date_added = dt

    return Bookmark(
        url=url,
        url_normalized=normalize_url(url),
        title=title[:512],
        notes=notes,
        unread=unread,
        shared=shared,
        is_archived=False,
        date_added=date_added,
        date_modified=date_added,
        owner=user,
    )
