from __future__ import annotations

import json
import os
import re
import shutil
import sys
import traceback

import yt_dlp


def _find_js_runtime() -> dict:
    runtime_candidates = (
        ("deno", os.path.expanduser("~/.deno/bin/deno")),
        ("deno", shutil.which("deno")),
        ("node", shutil.which("node")),
        ("node", shutil.which("nodejs")),
        ("bun", shutil.which("bun")),
        ("quickjs", shutil.which("qjs")),
    )

    for runtime, path in runtime_candidates:
        if path and os.path.exists(path):
            return {runtime: {"path": path}}
    return {"deno": {}}


BASE_YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'quiet': True,
    'listformats': False,
    'postprocessor_args': [],
    'js_runtimes': _find_js_runtime(),
}

YDL_OPTIONS = {
    **BASE_YDL_OPTIONS,
    'noplaylist': True,
}

YDL_PLAYLIST_OPTIONS = {
    **BASE_YDL_OPTIONS,
    'extract_flat': 'in_playlist',
    'ignoreerrors': True,
}


def extract_video(url: str, fallback_thumbnail: str) -> list:
    with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
        info = ydl.extract_info(url, download=False)

    stream_metadata = {
        'availability': info.get('availability'),
        'age_limit': info.get('age_limit'),
        'playable_in_embed': info.get('playable_in_embed'),
        'format_id': info.get('format_id'),
        'yt_dlp_version': getattr(getattr(yt_dlp, 'version', None), '__version__', 'unknown'),
        'http_headers': dict(info.get('http_headers') or {}),
    }
    return [
        info.get('title', '제목 없음'),
        info.get('url'),
        info.get('thumbnail', fallback_thumbnail),
        info.get('duration', 0),
        stream_metadata,
    ]


def extract_playlist(playlist_url: str) -> list | bool:
    video_list = []
    with yt_dlp.YoutubeDL(YDL_PLAYLIST_OPTIONS) as ydl:
        playlist_info = ydl.extract_info(playlist_url, download=False)

    for entry in playlist_info.get('entries') or []:
        if not entry or not entry.get('title'):
            continue

        entry_url = entry.get('webpage_url') or entry.get('original_url') or entry.get('url')
        if entry_url and not re.compile(r'^(http|https)://').match(entry_url):
            entry_id = entry.get('id') or entry_url
            entry_url = f"https://www.youtube.com/watch?v={entry_id}"

        if entry_url:
            video_list.append([entry_url, entry['title']])

    return video_list if video_list else False


def main() -> int:
    try:
        operation = sys.argv[1]
        request = json.load(sys.stdin)
        url = request['url']

        if operation == 'video':
            result = extract_video(url, request['fallback_thumbnail'])
        elif operation == 'playlist':
            result = extract_playlist(url)
        else:
            raise ValueError(f"Unknown extraction operation: {operation}")

        json.dump({'ok': True, 'result': result}, sys.stdout, ensure_ascii=False)
        return 0
    except Exception:
        json.dump({'ok': False, 'error': traceback.format_exc()}, sys.stdout, ensure_ascii=False)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
