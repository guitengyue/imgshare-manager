import os
import shutil
import sqlite3
import time
import json
from datetime import datetime
from pathlib import Path

import requests
from PIL import Image, ImageOps, UnidentifiedImageError
from flask import abort, Flask, redirect, render_template, request, send_file, url_for


app = Flask(__name__)

MICROBIN_URL = os.environ.get("MICROBIN_URL", "http://microbin:8080").rstrip("/")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "/data/database.sqlite")

EXPIRATIONS = [
    ("3days", "3 天", 3 * 24 * 60 * 60, "3days"),
    ("30days", "30 天", 30 * 24 * 60 * 60, "1week"),
    ("3months", "3 个月", 90 * 24 * 60 * 60, "1week"),
    ("6months", "6 个月", 180 * 24 * 60 * 60, "1week"),
    ("1year", "1 年", 365 * 24 * 60 * 60, "1week"),
    ("3years", "3 年", 3 * 365 * 24 * 60 * 60, "1week"),
]

EXPIRATION_MAP = {item[0]: item for item in EXPIRATIONS}
ATTACHMENTS_PATH = os.environ.get("ATTACHMENTS_PATH", "/data/attachments")
THUMBNAILS_PATH = os.environ.get("THUMBNAILS_PATH", "/data/imgshare-thumbnails")
DEFAULT_TOPIC = "未分类"
DEFAULT_HOME_IMAGE_LIMIT = 10
THUMBNAIL_MAX_SIZE = (480, 360)


def env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


HOME_IMAGE_LIMIT = env_int("HOME_IMAGE_LIMIT", DEFAULT_HOME_IMAGE_LIMIT)
IMAGE_INDEX_TTL_SECONDS = env_int("IMAGE_INDEX_TTL_SECONDS", 300)
STATS_CACHE_SECONDS = env_int("STATS_CACHE_SECONDS", 300)
PAGE_SIZE_OPTIONS = sorted({10, 20, 50, 100, HOME_IMAGE_LIMIT})


def request_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(request.values.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def selected_page_size() -> int:
    requested = request_int("per_page", HOME_IMAGE_LIMIT)
    if requested in PAGE_SIZE_OPTIONS:
        return requested
    return HOME_IMAGE_LIMIT


def pagination_numbers(current_page: int, total_pages: int) -> list[int | None]:
    if total_pages <= 7:
        return list(range(1, total_pages + 1))

    pages = {
        1,
        total_pages,
        current_page - 2,
        current_page - 1,
        current_page,
        current_page + 1,
        current_page + 2,
    }
    ordered = sorted(page for page in pages if 1 <= page <= total_pages)

    result: list[int | None] = []
    previous = 0
    for page in ordered:
        if previous and page - previous > 1:
            result.append(None)
        result.append(page)
        previous = page
    return result


def index_redirect(topic: str | None = None, page: int | None = None, per_page: int | None = None):
    params = {}
    if topic:
        params["topic"] = topic
    if page:
        params["page"] = page
    if per_page:
        params["per_page"] = per_page
    return redirect(url_for("index", **params), code=302)


def format_time(timestamp: int | None) -> str:
    if not timestamp:
        return "未知"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def retention_label(seconds: int | None) -> str:
    if not seconds or seconds <= 0:
        return "未知"

    best = min(EXPIRATIONS, key=lambda item: abs(item[2] - seconds))
    if abs(best[2] - seconds) <= 24 * 60 * 60:
        return best[1]

    days = max(1, round(seconds / (24 * 60 * 60)))
    return f"{days} 天"


def format_size(size: int) -> str:
    value = float(size)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024


def normalize_topic(topic: str | None) -> str:
    cleaned = (topic or "").strip()
    if not cleaned:
        return DEFAULT_TOPIC
    return cleaned[:80]


def ensure_metadata_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS imgshare_topics (
            pasta_id INTEGER PRIMARY KEY,
            topic TEXT NOT NULL,
            updated INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS imgshare_images (
            slug TEXT PRIMARY KEY,
            pasta_id INTEGER,
            filename TEXT NOT NULL,
            size INTEGER NOT NULL,
            created_ts INTEGER NOT NULL,
            expiration_ts INTEGER,
            file_mtime INTEGER NOT NULL,
            is_image INTEGER NOT NULL,
            updated INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_imgshare_images_created
        ON imgshare_images(created_ts DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_imgshare_images_pasta
        ON imgshare_images(pasta_id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS imgshare_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM imgshare_meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return row[0]


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO imgshare_meta (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def clear_stats_cache(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM imgshare_meta WHERE key IN ('stats_cached_at', 'stats_payload')")


def is_image_file(file_path: Path) -> bool:
    return file_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif"}


def first_file_in_folder(folder: Path) -> Path | None:
    files = [item for item in folder.iterdir() if item.is_file()]
    if not files:
        return None
    return files[0]


def rows_by_file_name(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    conn.row_factory = sqlite3.Row
    if not table_exists(conn, "pasta"):
        return {}

    rows = conn.execute(
        """
        SELECT id, file_name, file_size, created, expiration, read_count
        FROM pasta
        WHERE file_name IS NOT NULL
        ORDER BY created DESC
        """
    ).fetchall()

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["file_name"], []).append(row)
    return grouped


def image_record_for_folder(folder: Path, db_rows_by_name: dict[str, list[sqlite3.Row]]) -> dict | None:
    file_path = first_file_in_folder(folder)
    if file_path is None:
        return None

    stat = file_path.stat()
    filename = file_path.name
    created_guess = int(stat.st_mtime)
    candidates = db_rows_by_name.get(filename, [])
    matched = None
    if candidates:
        matched = min(candidates, key=lambda row: abs(row["created"] - created_guess))

    return {
        "slug": folder.name,
        "pasta_id": matched["id"] if matched else None,
        "filename": filename,
        "size": stat.st_size,
        "created_ts": matched["created"] if matched else created_guess,
        "expiration_ts": matched["expiration"] if matched else None,
        "file_mtime": created_guess,
        "is_image": 1 if is_image_file(file_path) else 0,
        "updated": int(time.time()),
    }


def upsert_image_record(conn: sqlite3.Connection, record: dict) -> None:
    conn.execute(
        """
        INSERT INTO imgshare_images (
            slug, pasta_id, filename, size, created_ts,
            expiration_ts, file_mtime, is_image, updated
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            pasta_id = excluded.pasta_id,
            filename = excluded.filename,
            size = excluded.size,
            created_ts = excluded.created_ts,
            expiration_ts = excluded.expiration_ts,
            file_mtime = excluded.file_mtime,
            is_image = excluded.is_image,
            updated = excluded.updated
        """,
        (
            record["slug"],
            record["pasta_id"],
            record["filename"],
            record["size"],
            record["created_ts"],
            record["expiration_ts"],
            record["file_mtime"],
            record["is_image"],
            record["updated"],
        ),
    )


def sync_image_index(force: bool = False) -> None:
    db_path = Path(DATABASE_PATH)
    attachments_root = Path(ATTACHMENTS_PATH)
    if not db_path.exists() or not attachments_root.exists():
        return

    now = int(time.time())
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        ensure_metadata_table(conn)
        last_sync = int(get_meta(conn, "image_index_synced_at") or 0)
        indexed_count = conn.execute("SELECT COUNT(*) FROM imgshare_images").fetchone()[0]
        if not force and indexed_count and now - last_sync < IMAGE_INDEX_TTL_SECONDS:
            return

        db_rows_by_name = rows_by_file_name(conn)
        seen_slugs = []
        for folder in attachments_root.iterdir():
            if not folder.is_dir():
                continue
            record = image_record_for_folder(folder, db_rows_by_name)
            if record is None:
                continue
            seen_slugs.append(record["slug"])
            upsert_image_record(conn, record)

        seen_slug_set = set(seen_slugs)
        existing_slugs = [
            row["slug"]
            for row in conn.execute("SELECT slug FROM imgshare_images").fetchall()
        ]
        for slug in existing_slugs:
            if slug not in seen_slug_set:
                conn.execute("DELETE FROM imgshare_images WHERE slug = ?", (slug,))

        set_meta(conn, "image_index_synced_at", str(now))
        clear_stats_cache(conn)
        conn.commit()


def refresh_image_index_for_slug(slug: str) -> None:
    db_path = Path(DATABASE_PATH)
    folder = Path(ATTACHMENTS_PATH) / slug
    if not db_path.exists():
        return

    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        ensure_metadata_table(conn)
        if not folder.exists() or not folder.is_dir():
            conn.execute("DELETE FROM imgshare_images WHERE slug = ?", (slug,))
            clear_stats_cache(conn)
            conn.commit()
            return

        record = image_record_for_folder(folder, rows_by_file_name(conn))
        if record is None:
            conn.execute("DELETE FROM imgshare_images WHERE slug = ?", (slug,))
        else:
            upsert_image_record(conn, record)
        clear_stats_cache(conn)
        conn.commit()


def item_from_index_row(row: sqlite3.Row) -> dict:
    topic = normalize_topic(row["topic"] or DEFAULT_TOPIC)
    expiration_ts = row["expiration_ts"]
    created_ts = row["created_ts"]
    saved_seconds = expiration_ts - created_ts if expiration_ts and created_ts else None
    slug = row["slug"]
    is_image = bool(row["is_image"])
    return {
        "slug": slug,
        "filename": row["filename"],
        "topic": topic,
        "size": row["size"],
        "created_ts": created_ts,
        "created_at": format_time(created_ts),
        "retention_label": retention_label(saved_seconds),
        "expiration_at": format_time(expiration_ts),
        "is_image": is_image,
        "file_url": f"/file/{slug}",
        "thumb_url": url_for("thumbnail", slug=slug) if is_image else "",
        "share_url": f"/upload/{slug}",
        "renew_url": url_for("renew_image", slug=slug),
        "topic_url": url_for("update_topic", slug=slug),
        "remove_url": url_for("delete_image", slug=slug),
    }


def indexed_images_page(selected_topic: str | None, page: int, page_size: int) -> tuple[list[dict], int]:
    sync_image_index()
    selected_topic = normalize_topic(selected_topic) if selected_topic else None
    offset = (page - 1) * page_size

    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        ensure_metadata_table(conn)
        topic_expr = "COALESCE(t.topic, ?)"
        where = ""
        where_params: list = []
        if selected_topic:
            where = f"WHERE {topic_expr} = ?"
            where_params = [DEFAULT_TOPIC, selected_topic]

        total_count = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM imgshare_images i
            LEFT JOIN imgshare_topics t ON t.pasta_id = i.pasta_id
            {where}
            """,
            where_params,
        ).fetchone()[0]

        rows = conn.execute(
            f"""
            SELECT i.*, {topic_expr} AS topic
            FROM imgshare_images i
            LEFT JOIN imgshare_topics t ON t.pasta_id = i.pasta_id
            {where}
            ORDER BY i.created_ts DESC, i.slug DESC
            LIMIT ? OFFSET ?
            """,
            [DEFAULT_TOPIC] + where_params + [page_size, offset],
        ).fetchall()

    return [item_from_index_row(row) for row in rows], total_count


def source_file_for_slug(slug: str) -> Path | None:
    if slug != Path(slug).name:
        return None
    folder = Path(ATTACHMENTS_PATH) / slug
    if not folder.exists() or not folder.is_dir():
        return None
    return first_file_in_folder(folder)


def thumbnail_path_for_slug(slug: str) -> Path:
    return Path(THUMBNAILS_PATH) / f"{slug}.jpg"


def delete_thumbnail(slug: str) -> None:
    thumbnail_path = thumbnail_path_for_slug(slug)
    if thumbnail_path.exists():
        thumbnail_path.unlink(missing_ok=True)


def ensure_thumbnail(slug: str, source_path: Path) -> Path | None:
    if source_path.suffix.lower() == ".svg":
        return None

    thumbnail_path = thumbnail_path_for_slug(slug)
    source_mtime = source_path.stat().st_mtime
    if thumbnail_path.exists() and thumbnail_path.stat().st_mtime >= source_mtime:
        return thumbnail_path

    thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(source_path) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail(THUMBNAIL_MAX_SIZE)
            if image.mode not in {"RGB", "L"}:
                background = Image.new("RGB", image.size, (255, 250, 242))
                if image.mode in {"RGBA", "LA"}:
                    background.paste(image, mask=image.getchannel("A"))
                    image = background
                else:
                    image = image.convert("RGB")
            image.save(thumbnail_path, "JPEG", quality=82, optimize=True)
    except (OSError, UnidentifiedImageError):
        return None

    return thumbnail_path


def delete_image_by_slug(slug: str) -> None:
    folder = Path(ATTACHMENTS_PATH) / slug
    filename = None
    created_guess = int(time.time())
    pasta_id = None

    if folder.exists() and folder.is_dir():
        file_path = first_file_in_folder(folder)
        if file_path:
            filename = file_path.name
            created_guess = int(file_path.stat().st_mtime)

    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        ensure_metadata_table(conn)
        cur = conn.cursor()
        if filename and table_exists(conn, "pasta"):
            rows = cur.execute(
                """
                SELECT id, created
                FROM pasta
                WHERE file_name = ?
                ORDER BY created DESC
                """,
                (filename,),
            ).fetchall()
            if rows:
                matched = min(rows, key=lambda row: abs(row["created"] - created_guess))
                pasta_id = matched["id"]
                cur.execute("DELETE FROM pasta WHERE id = ?", (pasta_id,))

        if pasta_id is not None:
            cur.execute("DELETE FROM imgshare_topics WHERE pasta_id = ?", (pasta_id,))
        cur.execute("DELETE FROM imgshare_images WHERE slug = ?", (slug,))
        clear_stats_cache(conn)
        conn.commit()

    if folder.exists() and folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
    delete_thumbnail(slug)


def get_topics() -> list[str]:
    db_path = Path(DATABASE_PATH)
    if not db_path.exists():
        return [DEFAULT_TOPIC]

    with sqlite3.connect(DATABASE_PATH) as conn:
        ensure_metadata_table(conn)
        rows = conn.execute(
            """
            SELECT DISTINCT topic
            FROM imgshare_topics
            WHERE topic IS NOT NULL AND trim(topic) <> ''
            ORDER BY topic COLLATE NOCASE
            """
        ).fetchall()

    topics = [row[0] for row in rows]
    if DEFAULT_TOPIC not in topics:
        topics.insert(0, DEFAULT_TOPIC)
    return topics


def set_topic(pasta_id: int, topic: str) -> None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        ensure_metadata_table(conn)
        conn.execute(
            """
            INSERT INTO imgshare_topics (pasta_id, topic, updated)
            VALUES (?, ?, ?)
            ON CONFLICT(pasta_id) DO UPDATE SET
                topic = excluded.topic,
                updated = excluded.updated
            """,
            (pasta_id, normalize_topic(topic), int(time.time())),
        )
        clear_stats_cache(conn)
        conn.commit()


def update_expiration(filename: str, file_size: int, desired_seconds: int) -> int:
    db_path = Path(DATABASE_PATH)
    if not db_path.exists():
        raise RuntimeError(f"database not found: {db_path}")

    now = int(time.time())
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.cursor()
        row = cur.execute(
            """
            SELECT id, created
            FROM pasta
            WHERE file_name = ?
              AND file_size = ?
              AND created >= ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (filename, file_size, now - 300),
        ).fetchone()
        if row is None:
            raise RuntimeError("uploaded row not found in database")

        pasta_id, created = row
        expiration = created + desired_seconds
        cur.execute(
            "UPDATE pasta SET expiration = ? WHERE id = ?",
            (expiration, pasta_id),
        )
        conn.commit()
        return pasta_id


def find_pasta_for_slug(slug: str):
    folder = Path(ATTACHMENTS_PATH) / slug
    if not folder.exists() or not folder.is_dir():
        return None, None

    files = [item for item in folder.iterdir() if item.is_file()]
    if not files:
        return None, None

    file_path = files[0]
    created_guess = int(file_path.stat().st_mtime)

    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT id, file_name, file_size, created, expiration, read_count
            FROM pasta
            WHERE file_name = ?
            ORDER BY created DESC
            """,
            (file_path.name,),
        ).fetchall()

    if not rows:
        return None, file_path

    matched = min(rows, key=lambda row: abs(row["created"] - created_guess))
    return matched, file_path


def list_images(selected_topic: str | None = None, limit: int | None = None):
    attachments_root = Path(ATTACHMENTS_PATH)
    if not attachments_root.exists():
        return []

    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        ensure_metadata_table(conn)
        cur = conn.cursor()
        db_rows = cur.execute(
            """
            SELECT id, file_name, file_size, created, expiration, read_count
            FROM pasta
            ORDER BY created DESC
            """
        ).fetchall()
        topic_rows = cur.execute(
            """
            SELECT pasta_id, topic
            FROM imgshare_topics
            """
        ).fetchall()

    topics_by_id = {row["pasta_id"]: row["topic"] for row in topic_rows}
    selected_topic = normalize_topic(selected_topic) if selected_topic else None

    rows_by_name = {}
    for row in db_rows:
        rows_by_name.setdefault(row["file_name"], []).append(row)

    items = []
    for folder in attachments_root.iterdir():
        if not folder.is_dir():
            continue

        files = [item for item in folder.iterdir() if item.is_file()]
        if not files:
            continue

        file_path = files[0]
        stat = file_path.stat()
        filename = file_path.name
        created_guess = int(stat.st_mtime)
        matched = None
        candidates = rows_by_name.get(filename, [])

        if candidates:
            matched = min(candidates, key=lambda row: abs(row["created"] - created_guess))

        created_ts = matched["created"] if matched else created_guess
        expiration_ts = matched["expiration"] if matched else None
        saved_seconds = expiration_ts - created_ts if expiration_ts and created_ts else None
        pasta_id = matched["id"] if matched else None
        topic = normalize_topic(topics_by_id.get(pasta_id) if pasta_id else DEFAULT_TOPIC)
        if selected_topic and topic != selected_topic:
            continue
        ext = file_path.suffix.lower()
        is_image = ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif"}

        items.append(
            {
                "slug": folder.name,
                "filename": filename,
                "topic": topic,
                "size": stat.st_size,
                "created_ts": created_ts,
                "created_at": format_time(created_ts),
                "retention_label": retention_label(saved_seconds),
                "expiration_at": format_time(expiration_ts),
                "is_image": is_image,
                "file_url": f"/file/{folder.name}",
                "share_url": f"/upload/{folder.name}",
                "renew_url": url_for("renew_image", slug=folder.name),
                "topic_url": url_for("update_topic", slug=folder.name),
                "remove_url": url_for("delete_image", slug=folder.name),
            }
        )

    items.sort(key=lambda item: item["created_ts"], reverse=True)
    if limit is not None:
        return items[:limit]
    return items


def collect_stats():
    sync_image_index()
    now = int(time.time())

    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        ensure_metadata_table(conn)
        cached_at = int(get_meta(conn, "stats_cached_at") or 0)
        payload = get_meta(conn, "stats_payload")
        if payload and now - cached_at < STATS_CACHE_SECONDS:
            return json.loads(payload)

        summary = conn.execute(
            """
            SELECT COUNT(*) AS total_count, COALESCE(SUM(size), 0) AS total_size
            FROM imgshare_images
            """
        ).fetchone()
        rows = conn.execute(
            """
            SELECT COALESCE(t.topic, ?) AS topic,
                   COUNT(*) AS count,
                   COALESCE(SUM(i.size), 0) AS size
            FROM imgshare_images i
            LEFT JOIN imgshare_topics t ON t.pasta_id = i.pasta_id
            GROUP BY COALESCE(t.topic, ?)
            ORDER BY size DESC
            """,
            (DEFAULT_TOPIC, DEFAULT_TOPIC),
        ).fetchall()

        topic_rows = []
        for row in rows:
            topic_rows.append(
                {
                    "topic": normalize_topic(row["topic"]),
                    "count": row["count"],
                    "size": row["size"],
                    "size_label": format_size(row["size"]),
                }
            )

        stats = {
            "total_count": summary["total_count"],
            "total_size": summary["total_size"],
            "total_size_label": format_size(summary["total_size"]),
            "topic_rows": topic_rows,
            "cached_at": format_time(now),
        }
        set_meta(conn, "stats_cached_at", str(now))
        set_meta(conn, "stats_payload", json.dumps(stats, ensure_ascii=False))
        conn.commit()
        return stats


@app.get("/")
def index():
    selected_topic = request.args.get("topic", "").strip()
    page_size = selected_page_size()
    requested_page = request_int("page", 1)
    page_items, total_count = indexed_images_page(selected_topic or None, requested_page, page_size)
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    page = min(requested_page, total_pages)
    if page != requested_page:
        page_items, total_count = indexed_images_page(selected_topic or None, page, page_size)
    start = (page - 1) * page_size
    end = start + page_size
    pagination = {
        "page": page,
        "page_size": page_size,
        "page_size_options": PAGE_SIZE_OPTIONS,
        "total_count": total_count,
        "total_pages": total_pages,
        "start_index": start + 1 if total_count else 0,
        "end_index": min(end, total_count),
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_page": max(1, page - 1),
        "next_page": min(total_pages, page + 1),
        "pages": pagination_numbers(page, total_pages),
    }
    return render_template(
        "index.html",
        expirations=EXPIRATIONS,
        default_expiration="3years",
        topics=get_topics(),
        selected_topic=selected_topic,
        items=page_items,
        pagination=pagination,
        display_limit=page_size,
    )


@app.get("/stats")
def stats():
    return render_template("stats.html", stats=collect_stats())


@app.get("/thumb/<slug>")
def thumbnail(slug: str):
    source_path = source_file_for_slug(slug)
    if source_path is None:
        abort(404)

    thumbnail_path = ensure_thumbnail(slug, source_path)
    if thumbnail_path is None:
        return redirect(f"/file/{slug}", code=302)

    return send_file(thumbnail_path, mimetype="image/jpeg", conditional=True, max_age=86400)


@app.post("/upload")
def upload():
    selected = request.form.get("expiration", "3years")
    item = EXPIRATION_MAP.get(selected, EXPIRATION_MAP["3years"])
    _, _, desired_seconds, microbin_expiration = item
    topic = normalize_topic(request.form.get("topic"))

    uploads = [item for item in request.files.getlist("file") if item and item.filename]
    if not uploads:
        return "请选择图片文件", 400

    last_location = "/"
    for upload in uploads:
        file_bytes = upload.read()
        files = {
            "file": (upload.filename, file_bytes, upload.mimetype or "application/octet-stream"),
        }
        data = {
            "content": "",
            "expiration": microbin_expiration,
        }

        response = requests.post(
            f"{MICROBIN_URL}/upload",
            data=data,
            files=files,
            allow_redirects=False,
            timeout=120,
        )
        response.raise_for_status()

        pasta_id = update_expiration(upload.filename, len(file_bytes), desired_seconds)
        set_topic(pasta_id, topic)
        last_location = response.headers.get("Location", "/")

    sync_image_index(force=True)
    location = last_location
    return redirect(location, code=302)


@app.post("/topic/<slug>")
def update_topic(slug: str):
    topic = normalize_topic(request.form.get("topic"))
    per_page = request_int("return_per_page", HOME_IMAGE_LIMIT)
    matched, _ = find_pasta_for_slug(slug)
    if matched is None:
        return "找不到这张图片的记录", 404

    set_topic(matched["id"], topic)
    return index_redirect(topic=topic, per_page=per_page)


@app.post("/bulk")
def bulk_update():
    slugs = [slug for slug in request.form.getlist("slugs") if slug.strip()]
    action = request.form.get("action", "")
    return_topic = request.form.get("return_topic", "").strip()
    return_page = request_int("return_page", 1)
    return_per_page = request_int("return_per_page", HOME_IMAGE_LIMIT)

    if not slugs:
        return "请先选择图片", 400

    if action == "renew":
        selected = request.form.get("expiration", "3years")
        item = EXPIRATION_MAP.get(selected, EXPIRATION_MAP["3years"])
        _, _, desired_seconds, _ = item
        expiration = int(time.time()) + desired_seconds
        renewed_slugs = []

        with sqlite3.connect(DATABASE_PATH) as conn:
            cur = conn.cursor()
            for slug in slugs:
                matched, _ = find_pasta_for_slug(slug)
                if matched is None:
                    continue
                cur.execute(
                    "UPDATE pasta SET expiration = ? WHERE id = ?",
                    (expiration, matched["id"]),
                )
                renewed_slugs.append(slug)
            conn.commit()

        for slug in renewed_slugs:
            refresh_image_index_for_slug(slug)

        return index_redirect(topic=return_topic, page=return_page, per_page=return_per_page)

    if action == "topic":
        topic = normalize_topic(request.form.get("topic"))
        for slug in slugs:
            matched, _ = find_pasta_for_slug(slug)
            if matched is None:
                continue
            set_topic(matched["id"], topic)

        return index_redirect(topic=topic, per_page=return_per_page)

    if action == "delete":
        for slug in slugs:
            delete_image_by_slug(slug)

        return index_redirect(topic=return_topic, page=return_page, per_page=return_per_page)

    return "未知的批量操作", 400


@app.post("/renew/<slug>")
def renew_image(slug: str):
    selected = request.form.get("expiration", "3years")
    item = EXPIRATION_MAP.get(selected, EXPIRATION_MAP["3years"])
    _, _, desired_seconds, _ = item
    return_topic = request.form.get("return_topic", "").strip()
    return_page = request_int("return_page", 1)
    return_per_page = request_int("return_per_page", HOME_IMAGE_LIMIT)

    matched, _ = find_pasta_for_slug(slug)
    if matched is None:
        return "找不到这张图片的记录", 404

    expiration = int(time.time()) + desired_seconds
    with sqlite3.connect(DATABASE_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE pasta SET expiration = ? WHERE id = ?",
            (expiration, matched["id"]),
        )
        conn.commit()

    refresh_image_index_for_slug(slug)
    return index_redirect(topic=return_topic, page=return_page, per_page=return_per_page)


@app.post("/delete/<slug>")
def delete_image(slug: str):
    return_topic = request.form.get("return_topic", "").strip()
    return_page = request_int("return_page", 1)
    return_per_page = request_int("return_per_page", HOME_IMAGE_LIMIT)
    delete_image_by_slug(slug)

    return index_redirect(topic=return_topic, page=return_page, per_page=return_per_page)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081, debug=False)
