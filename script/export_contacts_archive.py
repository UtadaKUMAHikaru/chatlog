#!/usr/bin/env python3
import argparse
import csv
import html
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta


TZ_SH = timezone(timedelta(hours=8))


def sanitize_filename(name):
    name = (name or "").strip()
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name)
    return name or "unknown"


def detect_ext(data):
    if not data:
        return ".bin"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


def display_name(row):
    for key in ("remark", "nick_name", "alias", "username"):
        value = row.get(key) or ""
        if value.strip():
            return value.strip()
    return row["username"]


def parse_sns_content_desc(content):
    if not content:
        return ""
    m = re.search(r"<contentDesc>(.*?)</contentDesc>", content, flags=re.S)
    if not m:
        return ""
    text = html.unescape(m.group(1))
    text = text.replace("\r", "").strip()
    return text


def fmt_ts(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), TZ_SH).isoformat()
    except Exception:
        return ""


def main():
    parser = argparse.ArgumentParser(description="Export all contacts with avatars and optional latest SNS text.")
    parser.add_argument("--contact-db", required=True)
    parser.add_argument("--head-image-db", required=True)
    parser.add_argument("--sns-db", default="")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    avatars_dir = os.path.join(args.output_dir, "avatars")
    os.makedirs(avatars_dir, exist_ok=True)

    contact_conn = sqlite3.connect(args.contact_db)
    contact_conn.row_factory = sqlite3.Row
    head_conn = sqlite3.connect(args.head_image_db)
    head_conn.row_factory = sqlite3.Row

    sns_latest = {}
    if args.sns_db and os.path.exists(args.sns_db):
        sns_conn = sqlite3.connect(args.sns_db)
        sns_conn.row_factory = sqlite3.Row
        for row in sns_conn.execute(
            """
            SELECT user_name, content
            FROM SnsTimeLine
            ORDER BY tid DESC
            """
        ):
            user_name = row["user_name"]
            if user_name in sns_latest:
                continue
            desc = parse_sns_content_desc(row["content"])
            if desc:
                sns_latest[user_name] = desc
        sns_conn.close()

    avatars = {}
    for row in head_conn.execute("SELECT username, md5, image_buffer, update_time FROM head_image"):
        avatars[row["username"]] = row

    contacts = []
    for row in contact_conn.execute(
        """
        SELECT
          username,
          alias,
          remark,
          nick_name,
          big_head_url,
          small_head_url,
          head_img_md5
        FROM contact
        ORDER BY username
        """
    ):
        contacts.append(dict(row))

    name_counts = Counter(display_name(row) for row in contacts)
    name_seen = defaultdict(int)
    rows_out = []

    for row in contacts:
        username = row["username"]
        shown_name = display_name(row)
        name_seen[shown_name] += 1

        avatar_row = avatars.get(username)
        avatar_file = ""
        avatar_ext = ""
        avatar_mtime = ""

        if avatar_row and avatar_row["image_buffer"]:
            data = avatar_row["image_buffer"]
            avatar_ext = detect_ext(data)
            base = sanitize_filename(shown_name)
            if name_counts[shown_name] > 1:
                base = f"{base}__{sanitize_filename(username)}"
            avatar_file = base + avatar_ext
            avatar_path = os.path.join(avatars_dir, avatar_file)
            with open(avatar_path, "wb") as f:
                f.write(data)
            avatar_mtime = fmt_ts(avatar_row["update_time"])

        rows_out.append(
            {
                "username": username,
                "alias": row.get("alias", ""),
                "remark": row.get("remark", ""),
                "nick_name": row.get("nick_name", ""),
                "display_name": shown_name,
                "avatar_file": avatar_file,
                "avatar_update_time": avatar_mtime,
                "big_head_url": row.get("big_head_url", ""),
                "small_head_url": row.get("small_head_url", ""),
                "latest_sns_text": sns_latest.get(username, ""),
            }
        )

    manifest_csv = os.path.join(args.output_dir, "contacts_manifest.csv")
    with open(manifest_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "username",
                "alias",
                "remark",
                "nick_name",
                "display_name",
                "avatar_file",
                "avatar_update_time",
                "big_head_url",
                "small_head_url",
                "latest_sns_text",
            ],
        )
        writer.writeheader()
        writer.writerows(rows_out)

    summary_path = os.path.join(args.output_dir, "summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"contacts: {len(rows_out)}\n")
        f.write(f"avatars_exported: {sum(1 for r in rows_out if r['avatar_file'])}\n")
        f.write(f"latest_sns_extracted: {sum(1 for r in rows_out if r['latest_sns_text'])}\n")

    print(f"contacts={len(rows_out)} avatars_exported={sum(1 for r in rows_out if r['avatar_file'])} latest_sns_extracted={sum(1 for r in rows_out if r['latest_sns_text'])}")


if __name__ == "__main__":
    main()
