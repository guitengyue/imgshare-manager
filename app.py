import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, redirect, render_template, request, url_for


app = Flask(__name__)

MICROBIN_URL = os.environ.get("MICROBIN_URL", "http://microbin:8080").rstrip("/")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "/data/database.sqlite")

EXPIRATIONS = [
    ("1min", "1 分钟", 60, "1min"),
    ("10min", "10 分钟", 10 * 60, "10min"),
    ("1hour", "1 小时", 60 * 60, "1hour"),
    ("24hour", "24 小时", 24 * 60 * 60, "24hour"),
    ("3days", "3 天", 3 * 24 * 60 * 60, "3days"),
    ("1week", "1 周", 7 * 24 * 60 * 60, "1week"),
    ("3months", "3 个月", 90 * 24 * 60 * 60, "1week"),
    ("6months", "6 个月", 180 * 24 * 60 * 60, "1week"),
    ("12months", "12 个月", 365 * 24 * 60 * 60, "1week"),
]

EXPIRATION_MAP = {item[0]: item for item in EXPIRATIONS}
ATTACHMENTS_PATH = os.environ.get("ATTACHMENTS_PATH", "/data/attachments")


def update_expiration(filename: str, file_size: int, desired_seconds: int) -> None:
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


def list_images():
    attachments_root = Path(ATTACHMENTS_PATH)
    if not attachments_root.exists():
        return []

    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        db_rows = cur.execute(
            """
            SELECT id, file_name, file_size, created, expiration, read_count
            FROM pasta
            ORDER BY created DESC
            """
        ).fetchall()

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
        ext = file_path.suffix.lower()
        is_image = ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif"}

        items.append(
            {
                "slug": folder.name,
                "filename": filename,
                "size": stat.st_size,
                "created_ts": created_ts,
                "created_at": datetime.fromtimestamp(created_ts).strftime("%Y-%m-%d %H:%M"),
                "expiration_at": datetime.fromtimestamp(expiration_ts).strftime("%Y-%m-%d %H:%M")
                if expiration_ts
                else "未知",
                "is_image": is_image,
                "file_url": f"/file/{folder.name}",
                "share_url": f"/upload/{folder.name}",
                "remove_url": url_for("delete_image", slug=folder.name),
            }
        )

    items.sort(key=lambda item: item["created_ts"], reverse=True)
    return items


@app.get("/")
def index():
    return render_template(
        "index.html",
        expirations=EXPIRATIONS,
        default_expiration="3months",
        items=list_images(),
    )


@app.post("/upload")
def upload():
    selected = request.form.get("expiration", "24hour")
    item = EXPIRATION_MAP.get(selected, EXPIRATION_MAP["24hour"])
    _, _, desired_seconds, microbin_expiration = item

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

        update_expiration(upload.filename, len(file_bytes), desired_seconds)
        last_location = response.headers.get("Location", "/")

    location = last_location
    return redirect(location, code=302)


@app.post("/delete/<slug>")
def delete_image(slug: str):
    folder = Path(ATTACHMENTS_PATH) / slug
    filename = None
    created_guess = int(time.time())

    if folder.exists() and folder.is_dir():
        files = [item for item in folder.iterdir() if item.is_file()]
        if files:
            filename = files[0].name
            created_guess = int(files[0].stat().st_mtime)

    if filename:
        with sqlite3.connect(DATABASE_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
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
                cur.execute("DELETE FROM pasta WHERE id = ?", (matched["id"],))
                conn.commit()

    if folder.exists() and folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)

    return redirect(url_for("index"), code=302)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081, debug=False)
