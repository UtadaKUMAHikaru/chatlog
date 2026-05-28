#!/usr/bin/env python3
import argparse
import csv
import glob
import json
import os
import re
import shutil
import sys
import time
from urllib import parse, request, error


LOCAL_MEDIA_TYPES = {
    "image": "/image/{key}",
    "video": "/video/{key}",
    "voice": "/voice/{key}",
    "file": "/file/{key}",
}

GLOB_CACHE = {}
PREFIX_INDEX_CACHE = {}


def slugify(text):
    text = text.strip()
    text = re.sub(r"[\\/:*?\"<>|]", "_", text)
    text = re.sub(r"\s+", "_", text)
    return text or "chat_export"


def build_url(base_url, path, params):
    query = parse.urlencode(params)
    return "{base}{path}?{query}".format(base=base_url.rstrip("/"), path=path, query=query)


def get_json(url, timeout=60):
    req = request.Request(url, headers={"User-Agent": "chatlog-export/1.0"})
    with request.urlopen(req, timeout=timeout) as resp:
        data = resp.read().decode("utf-8")
    return json.loads(data)


def fetch_all_messages(base_url, talker, start, end, page_size, sleep_sec):
    offset = 0
    messages = []
    while True:
        url = build_url(
            base_url,
            "/api/v1/chatlog",
            {
                "talker": talker,
                "time": "{0}~{1}".format(start, end),
                "format": "json",
                "limit": page_size,
                "offset": offset,
            },
        )
        batch = get_json(url)
        if not batch:
            break
        messages.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
        if sleep_sec > 0:
            time.sleep(sleep_sec)
    return messages


def build_local_media_url(base_url, kind, key_parts):
    key_parts = [part for part in key_parts if part]
    if not key_parts:
        return ""
    return "{0}{1}".format(base_url.rstrip("/"), LOCAL_MEDIA_TYPES[kind].format(key=",".join(key_parts)))


def message_text(message):
    content = message.get("content") or ""
    if content:
        return content
    contents = message.get("contents") or {}
    title = contents.get("title") or ""
    url = contents.get("url") or ""
    if title and url:
        return "[{0}]({1})".format(title, url)
    if title:
        return title
    if url:
        return url
    return ""


def collect_refs(message, base_url):
    refs = []
    msg_type = message.get("type")
    sub_type = message.get("subType")
    contents = message.get("contents") or {}

    if msg_type == 3:
        url = build_local_media_url(
            base_url,
            "image",
            [contents.get("md5"), contents.get("path"), contents.get("thumbpath")],
        )
        if url:
            refs.append(("image", "wechat_local", contents.get("md5") or "", contents.get("path") or "", url))
    elif msg_type == 43:
        url = build_local_media_url(
            base_url,
            "video",
            [contents.get("md5"), contents.get("rawmd5"), contents.get("path")],
        )
        if url:
            refs.append(("video", "wechat_local", contents.get("md5") or contents.get("rawmd5") or "", contents.get("path") or "", url))
    elif msg_type == 34:
        voice_key = contents.get("voice")
        if voice_key:
            url = build_local_media_url(base_url, "voice", [voice_key])
            refs.append(("voice", "wechat_local", voice_key, "", url))
    elif msg_type == 49:
        if sub_type == 6:
            md5_value = contents.get("md5")
            if md5_value:
                url = build_local_media_url(base_url, "file", [md5_value])
                refs.append(("file", "wechat_local", md5_value, "", url))
        elif sub_type in (4, 5, 51, 63, 92):
            if contents.get("url"):
                refs.append(("share_link", "external", "", "", contents["url"]))
        elif sub_type == 57:
            refer = contents.get("refer") or {}
            refer_content = refer.get("content") or ""
            if refer_content:
                refs.append(("quote", "inline", "", "", refer_content))
        elif sub_type == 19:
            refs.append(("merge_forward", "inline", "", "", "recordInfo"))

    return refs


def flatten_message(message, refs):
    contents = message.get("contents") or {}
    urls = [ref[4] for ref in refs if ref[1] in ("wechat_local", "external")]
    return {
        "time": message.get("time", ""),
        "seq": message.get("seq", ""),
        "sender_name": message.get("senderName", ""),
        "sender": message.get("sender", ""),
        "talker_name": message.get("talkerName", ""),
        "talker": message.get("talker", ""),
        "type": message.get("type", ""),
        "sub_type": message.get("subType", ""),
        "text": message_text(message),
        "title": contents.get("title", ""),
        "url": contents.get("url", ""),
        "media_urls": "\n".join(urls),
    }


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_csv(path, rows, fieldnames):
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_markdown(path, messages, refs_by_seq):
    with open(path, "w", encoding="utf-8") as f:
        for message in messages:
            f.write("## {0} {1}\n".format(message.get("time", ""), message.get("senderName") or message.get("sender") or ""))
            f.write("\n")
            f.write("- type: {0}/{1}\n".format(message.get("type", ""), message.get("subType", "")))
            text = message_text(message)
            if text:
                f.write("- text:\n\n")
                f.write("{0}\n\n".format(text))
            refs = refs_by_seq.get(message.get("seq"), [])
            if refs:
                f.write("- refs:\n")
                for ref in refs:
                    f.write("  - [{0}] {1}\n".format(ref[0], ref[4]))
                f.write("\n")


def download_file(url, dest_path, timeout=120):
    req = request.Request(url, headers={"User-Agent": "chatlog-export/1.0"})
    with request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    with open(dest_path, "wb") as f:
        f.write(data)


def guess_extension(content_type, url):
    if "image/jpeg" in content_type:
        return ".jpg"
    if "image/png" in content_type:
        return ".png"
    if "image/gif" in content_type:
        return ".gif"
    if "video/mp4" in content_type:
        return ".mp4"
    if "audio/mpeg" in content_type:
        return ".mp3"
    parsed = parse.urlparse(url)
    _, ext = os.path.splitext(parsed.path)
    return ext or ".bin"


def sniff_extension_from_bytes(data, fallback=".bin"):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if data[4:8] == b"ftyp":
        return ".mp4"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"ID3") or data[:2] == b"\xff\xfb":
        return ".mp3"
    return fallback


def cached_glob(pattern, recursive=False):
    key = (pattern, recursive)
    if key not in GLOB_CACHE:
        GLOB_CACHE[key] = glob.glob(pattern, recursive=recursive)
    return list(GLOB_CACHE[key])


def find_by_prefix(root_dir, prefix):
    if not root_dir or not prefix or not os.path.isdir(root_dir):
        return []

    root_dir = os.path.abspath(root_dir)
    index = PREFIX_INDEX_CACHE.get(root_dir)
    if index is None:
        index = {}
        for current_root, _, filenames in os.walk(root_dir):
            for filename in filenames:
                key = filename[:8]
                index.setdefault(key, []).append(os.path.join(current_root, filename))
        PREFIX_INDEX_CACHE[root_dir] = index

    return [path for path in index.get(prefix[:8], []) if os.path.basename(path).startswith(prefix)]


def resolve_local_media_path(media_root, row):
    if not media_root:
        return ""

    rel_path = row.get("path") or ""
    ref_kind = row.get("ref_kind") or ""
    ref_key = row.get("ref_key") or ""

    candidates = []
    if rel_path:
        abs_base = os.path.join(media_root, rel_path)
        candidates.extend(cached_glob(abs_base))
        candidates.extend(cached_glob(abs_base + "*"))

    if ref_kind == "video" and ref_key:
        candidates.extend(cached_glob(os.path.join(media_root, "msg", "video", "*", ref_key + "*")))
    if ref_kind == "file" and ref_key:
        candidates.extend(find_by_prefix(os.path.join(media_root, "msg", "file"), ref_key))
    if ref_kind == "voice" and ref_key:
        candidates.extend(find_by_prefix(media_root, ref_key))

    deduped = []
    seen = set()
    for candidate in candidates:
        if candidate in seen or not os.path.isfile(candidate):
            continue
        seen.add(candidate)
        deduped.append(candidate)

    if not deduped:
        return ""

    def pick_by_suffix(suffixes):
        for suffix in suffixes:
            for candidate in deduped:
                name = os.path.basename(candidate)
                if name.endswith(suffix):
                    return candidate
        return ""

    if ref_kind == "image":
        full = pick_by_suffix(["_h_M.dat", "_h.dat", "_M.dat", ".dat", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"])
        if full and "_t_M.dat" not in full and "_t.dat" not in full:
            return full
        thumb = pick_by_suffix(["_t_M.dat", "_t.dat"])
        if thumb:
            return thumb
    elif ref_kind == "video":
        full = pick_by_suffix([".mp4", ".mov", ".m4v"])
        if full:
            return full
        thumb = pick_by_suffix(["_thumb.jpg", ".jpg"])
        if thumb:
            return thumb
    elif ref_kind == "file":
        return deduped[0]

    return deduped[0]


def download_media(media_rows, output_dir, max_items, media_root):
    media_dir = os.path.join(output_dir, "media")
    if not os.path.isdir(media_dir):
        os.makedirs(media_dir)

    downloaded = []
    seen = set()
    used_names = set()
    local_rows = [row for row in media_rows if row["source"] == "wechat_local" and row["url"]]
    for row in local_rows:
        if max_items and len(downloaded) >= max_items:
            break
        if row["url"] in seen:
            continue
        seen.add(row["url"])
        data = None
        ext = ".bin"
        local_path = resolve_local_media_path(media_root, row)
        if local_path:
            with open(local_path, "rb") as f:
                data = f.read()
            ext = sniff_extension_from_bytes(data, os.path.splitext(local_path)[1] or ".bin")
        else:
            req = request.Request(row["url"], headers={"User-Agent": "chatlog-export/1.0"})
            try:
                with request.urlopen(req, timeout=120) as resp:
                    data = resp.read()
                    ext = guess_extension(resp.headers.get("Content-Type", ""), row["url"])
            except error.HTTPError:
                continue
            except error.URLError:
                continue
        base_filename = "{0}_{1}".format(row["time"].replace(":", "-"), row["seq"])
        disambiguator = row.get("ref_key") or row.get("path") or row.get("url") or ""
        if disambiguator:
            disambiguator = slugify(disambiguator)[-24:]
            base_filename = "{0}_{1}".format(base_filename, disambiguator)
        filename = slugify(base_filename + ext)
        if filename in used_names:
            suffix = 2
            stem, ext_part = os.path.splitext(filename)
            while True:
                candidate = "{0}_{1}{2}".format(stem, suffix, ext_part)
                if candidate not in used_names:
                    filename = candidate
                    break
                suffix += 1
        used_names.add(filename)
        dest_path = os.path.join(media_dir, filename)
        if local_path and os.path.abspath(local_path) != os.path.abspath(dest_path):
            with open(dest_path, "wb") as f:
                f.write(data)
        elif data is not None:
            with open(dest_path, "wb") as f:
                f.write(data)
        new_row = dict(row)
        new_row["downloaded_path"] = os.path.relpath(dest_path, output_dir)
        downloaded.append(new_row)
    return downloaded


def main():
    parser = argparse.ArgumentParser(description="Export one WeChat chatroom via chatlog HTTP API.")
    parser.add_argument("--talker", required=True, help="Chatroom username, for example 34536853767@chatroom")
    parser.add_argument("--name", default="", help="Human-readable chatroom name")
    parser.add_argument("--start", default="2000-01-01", help="Start date, YYYY-MM-DD")
    parser.add_argument("--end", default="2099-12-31", help="End date, YYYY-MM-DD")
    parser.add_argument("--base-url", default="http://127.0.0.1:5030", help="chatlog HTTP base URL")
    parser.add_argument("--page-size", type=int, default=500, help="API page size")
    parser.add_argument("--sleep", type=float, default=0.0, help="Sleep seconds between pages")
    parser.add_argument("--output-dir", default="", help="Export directory")
    parser.add_argument("--download-media", action="store_true", help="Download local image/video/file/voice attachments")
    parser.add_argument("--download-limit", type=int, default=0, help="Max local attachments to download, 0 means no limit")
    parser.add_argument("--media-root", default="", help="WeChat account root dir containing msg/, cache/, db_storage/")
    args = parser.parse_args()

    display_name = args.name or args.talker
    output_dir = args.output_dir or os.path.join(os.getcwd(), "exports", slugify(display_name))
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)

    messages = fetch_all_messages(args.base_url, args.talker, args.start, args.end, args.page_size, args.sleep)
    refs_by_seq = {}
    media_rows = []
    flat_rows = []

    for message in messages:
        refs = collect_refs(message, args.base_url)
        refs_by_seq[message.get("seq")] = refs
        for ref in refs:
            media_rows.append(
                {
                    "time": message.get("time", ""),
                    "seq": message.get("seq", ""),
                    "sender_name": message.get("senderName", ""),
                    "sender": message.get("sender", ""),
                    "type": message.get("type", ""),
                    "sub_type": message.get("subType", ""),
                    "ref_kind": ref[0],
                    "source": ref[1],
                    "ref_key": ref[2],
                    "path": ref[3],
                    "url": ref[4],
                    "downloaded_path": "",
                }
            )
        flat_rows.append(flatten_message(message, refs))

    summary = {
        "talker": args.talker,
        "name": display_name,
        "start": args.start,
        "end": args.end,
        "message_count": len(messages),
        "media_ref_count": len(media_rows),
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    if args.download_media:
        downloaded_rows = download_media(media_rows, output_dir, args.download_limit, args.media_root)
        downloaded_map = {}
        for row in downloaded_rows:
            downloaded_map[(row["seq"], row["url"])] = row["downloaded_path"]
        for row in media_rows:
            row["downloaded_path"] = downloaded_map.get((row["seq"], row["url"]), "")
        summary["downloaded_media_count"] = len(downloaded_rows)

    write_json(os.path.join(output_dir, "summary.json"), summary)
    write_json(os.path.join(output_dir, "messages.json"), messages)
    write_csv(
        os.path.join(output_dir, "messages.csv"),
        flat_rows,
        ["time", "seq", "sender_name", "sender", "talker_name", "talker", "type", "sub_type", "text", "title", "url", "media_urls"],
    )
    write_csv(
        os.path.join(output_dir, "media_manifest.csv"),
        media_rows,
        ["time", "seq", "sender_name", "sender", "type", "sub_type", "ref_kind", "source", "ref_key", "path", "url", "downloaded_path"],
    )
    write_markdown(os.path.join(output_dir, "messages.md"), messages, refs_by_seq)

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
