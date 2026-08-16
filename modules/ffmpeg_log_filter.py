from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse
import os
import re
import threading
import time

import discord


KST = timezone(timedelta(hours=9))
HTTP_403_PATTERN = re.compile(r"(?:http error|returned|status(?: code)?)\D*403\b|403 forbidden", re.IGNORECASE)
GOOGLEVIDEO_URL_PATTERN = re.compile(r"https?://[^\s]*googlevideo\.com/[^\s]+", re.IGNORECASE)
MAX_STDERR_BYTES = 128 * 1024


@dataclass(frozen=True)
class AccessDeniedClassification:
    cause: str
    details: tuple[str, ...]


def _query_value(query: Mapping[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _masked_ip(ip_address: str | None) -> str:
    if not ip_address:
        return "unknown"
    parts = ip_address.split(".")
    if len(parts) == 4:
        return ".".join((*parts[:3], "xxx"))
    return "masked"


def _safe_title(title: str | None) -> str:
    return (title or "unknown").replace("\r", " ").replace("\n", " ").replace('"', "'")[:160]


def classify_http_403(
    play_url: str,
    stream_metadata: Mapping[str, Any] | None = None,
    *,
    expected_ip: str | None = None,
    now: float | None = None,
) -> AccessDeniedClassification:
    """Classify an FFmpeg HTTP 403 using only evidence available to the bot."""
    metadata = stream_metadata or {}
    parsed_url = urlparse(play_url)
    query = parse_qs(parsed_url.query)
    hostname = (parsed_url.hostname or "").lower()
    is_youtube_stream = hostname == "googlevideo.com" or hostname.endswith(".googlevideo.com")
    availability = metadata.get("availability")
    age_limit = metadata.get("age_limit")
    playable_in_embed = metadata.get("playable_in_embed")
    player_client = (_query_value(query, "c") or "unknown").upper()
    details: list[str] = []

    if availability:
        details.append(f"availability={availability}")
    if availability == "unlisted":
        details.append("unlisted_is_not_access_restriction")
    if metadata.get("yt_dlp_version"):
        details.append(f"yt_dlp={metadata['yt_dlp_version']}")

    if not is_youtube_stream:
        return AccessDeniedClassification(
            "remote_access_denied",
            (*details, "possible=permission,token,request_headers"),
        )

    expires_at = _query_value(query, "expire")
    if expires_at:
        try:
            expires_timestamp = int(expires_at)
        except ValueError:
            expires_timestamp = None
        if expires_timestamp is not None and (now if now is not None else time.time()) >= expires_timestamp:
            expired_at = datetime.fromtimestamp(expires_timestamp, KST).strftime("%Y-%m-%d_%H:%M:%S_KST")
            return AccessDeniedClassification(
                "expired_stream_url",
                (*details, f"expired_at={expired_at}", "action=refresh_url"),
            )

    bound_ip = _query_value(query, "ip")
    if bound_ip and expected_ip and bound_ip != expected_ip:
        return AccessDeniedClassification(
            "configured_egress_ip_mismatch",
            (
                *details,
                f"bound_ip={_masked_ip(bound_ip)}",
                f"configured_ip={_masked_ip(expected_ip)}",
                "possible=vpn_change,region_route_change",
            ),
        )

    entitlement_restrictions = {"private", "premium_only", "subscriber_only"}
    if availability in entitlement_restrictions:
        return AccessDeniedClassification(
            "authentication_or_entitlement_required",
            (*details, "possible=cookies,account,subscription,video_restriction"),
        )

    if isinstance(age_limit, (int, float)) and age_limit >= 18:
        return AccessDeniedClassification(
            "age_restricted_request_context",
            (*details, f"age_limit={int(age_limit)}", "possible=missing_cookies_or_headers"),
        )

    if availability == "needs_auth":
        return AccessDeniedClassification(
            "authentication_or_entitlement_required",
            (*details, "possible=cookies,account,video_restriction"),
        )

    details.append(f"client={player_client}")
    if playable_in_embed is False:
        details.append("playable_in_embed=false")

    if playable_in_embed is False and "EMBEDDED" in player_client:
        return AccessDeniedClassification(
            "embed_playback_restricted",
            (*details, "possible=selected_client_not_allowed"),
        )
    if player_client == "ANDROID_VR":
        return AccessDeniedClassification(
            "youtube_cdn_access_denied",
            (
                *details,
                "possible=yt_dlp_client_fallback,PO_token,request_headers,actual_egress_ip,"
                "region_lock,video_restriction,rate_limit",
            ),
        )

    return AccessDeniedClassification(
        "youtube_access_context_rejected",
        (
            *details,
            "possible=request_headers,actual_egress_ip,region_lock,signature,PO_token,rate_limit",
        ),
    )


def report_ffmpeg_stderr(
    stderr_output: bytes | str,
    *,
    play_url: str,
    song_title: str | None,
    guild_name: str,
    stream_metadata: Mapping[str, Any] | None = None,
    expected_ip: str | None = None,
) -> bool:
    """Hide known FFmpeg network noise and print one classified line for HTTP 403."""
    if isinstance(stderr_output, bytes):
        text = stderr_output.decode("utf-8", errors="replace")
    else:
        text = stderr_output

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    has_http_403 = any(HTTP_403_PATTERN.search(line) for line in lines)
    if has_http_403:
        classification = classify_http_403(
            play_url,
            stream_metadata,
            expected_ip=expected_ip,
        )
        detail_text = " ".join(classification.details)
        print(
            f'[FFMPEG 403] cause={classification.cause} guild="{_safe_title(guild_name)}" '
            f'title="{_safe_title(song_title)}" {detail_text}'.rstrip()
        )
        return True

    for line in lines:
        lowered = line.lower()
        if (
            "error in the pull function" in lowered
            or "io error: connection reset by peer" in lowered
            or ("will reconnect at" in lowered and "connection reset by peer" in lowered)
        ):
            continue
        print(GOOGLEVIDEO_URL_PATTERN.sub("[redacted googlevideo URL]", line))

    return False


class BoundedStderrCapture:
    """Continuously drain a pipe while retaining only the newest stderr bytes."""

    def __init__(self, max_bytes: int = MAX_STDERR_BYTES) -> None:
        self._max_bytes = max_bytes
        self._buffer = bytearray()
        self._buffer_lock = threading.Lock()
        self._finish_lock = threading.Lock()
        self._finished = False
        read_fd, write_fd = os.pipe()
        self._reader = os.fdopen(read_fd, "rb", buffering=0)
        self.writer = os.fdopen(write_fd, "wb", buffering=0)
        self._reader_thread = threading.Thread(
            target=self._drain,
            daemon=True,
            name="ffmpeg-stderr-capture",
        )
        self._reader_thread.start()

    def _drain(self) -> None:
        try:
            while chunk := self._reader.read(8192):
                with self._buffer_lock:
                    self._buffer.extend(chunk)
                    overflow = len(self._buffer) - self._max_bytes
                    if overflow > 0:
                        del self._buffer[:overflow]
        finally:
            self._reader.close()

    def finish(self) -> bytes:
        with self._finish_lock:
            if not self._finished:
                self.writer.close()
                self._reader_thread.join()
                self._finished = True

        with self._buffer_lock:
            return bytes(self._buffer)


class FilteredFFmpegPCMAudio(discord.FFmpegPCMAudio):
    """Capture FFmpeg stderr and report only useful, classified messages."""

    def __init__(
        self,
        source: str,
        *,
        song_title: str | None,
        guild_name: str,
        stream_metadata: Mapping[str, Any] | None = None,
        expected_ip: str | None = None,
        **ffmpeg_options: Any,
    ) -> None:
        self._cleanup_lock = threading.Lock()
        self._cleaned = True
        self._stderr_capture = BoundedStderrCapture()
        self._stderr_context = {
            "play_url": source,
            "song_title": song_title,
            "guild_name": guild_name,
            "stream_metadata": dict(stream_metadata or {}),
            "expected_ip": expected_ip,
        }
        try:
            super().__init__(source, stderr=self._stderr_capture.writer, **ffmpeg_options)
        except Exception:
            self._stderr_capture.finish()
            raise
        self._cleaned = False

    def cleanup(self) -> None:
        with self._cleanup_lock:
            if self._cleaned:
                return
            self._cleaned = True

        try:
            super().cleanup()
        finally:
            try:
                stderr_output = self._stderr_capture.finish()
                report_ffmpeg_stderr(stderr_output, **self._stderr_context)
            except Exception as error:
                print(f"[WARNING] Failed to inspect FFmpeg stderr: {error}")
