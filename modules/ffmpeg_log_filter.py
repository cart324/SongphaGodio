from __future__ import annotations

from typing import Any, Callable, Mapping
import os
import re
import threading
import time

import discord

from modules.stream_diagnostics import (
    AccessDeniedClassification,
    classify_http_403,
    configured_ffmpeg_headers,
    sanitize_diagnostic,
    translate_http_403_cause,
)


HTTP_403_PATTERN = re.compile(r"(?:http error|returned|status(?: code)?)\D*403\b|403 forbidden", re.IGNORECASE)
GOOGLEVIDEO_URL_PATTERN = re.compile(r"https?://[^\s]*googlevideo\.com/[^\s]+", re.IGNORECASE)
RECONNECT_PATTERN = re.compile(
    r"will reconnect at\s+(?P<offset>\d+)\s+in\s+(?P<delay>\d+)\s+second\(s\),\s*"
    r"error=connection reset by peer",
    re.IGNORECASE,
)
MAX_STDERR_BYTES = 128 * 1024
AUDIO_FRAME_DURATION = 0.020


def _safe_title(title: str | None) -> str:
    return sanitize_diagnostic(title or "unknown", 160)


def _format_reconnect_log(line: str, *, song_title: str | None, guild_name: str) -> str | None:
    match = RECONNECT_PATTERN.search(line)
    if match is None:
        return None
    return (
        f'[FFMPEG RECONNECT] guild="{_safe_title(guild_name)}" '
        f'title="{_safe_title(song_title)}" reason=connection_reset '
        f'offset={match.group("offset")} delay={match.group("delay")}s'
    )


def report_ffmpeg_stderr(
    stderr_output: bytes | str,
    *,
    play_url: str,
    song_title: str | None,
    guild_name: str,
    stream_metadata: Mapping[str, Any] | None = None,
    expected_ip: str | None = None,
    request_headers: Mapping[str, str] | None = None,
    observed_at: float | None = None,
    classification_callback: Callable[[AccessDeniedClassification], None] | None = None,
) -> bool:
    """Report local evidence and bounded, redacted error lines for HTTP 403."""
    if isinstance(stderr_output, bytes):
        text = stderr_output.decode("utf-8", errors="replace")
    else:
        text = stderr_output

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    has_http_403 = observed_at is not None or any(HTTP_403_PATTERN.search(line) for line in lines)
    if has_http_403:
        classification = classify_http_403(
            play_url,
            stream_metadata,
            expected_ip=expected_ip,
            request_headers=request_headers,
            now=observed_at,
        )
        detail_text = " ".join(classification.details)
        print(
            f'[FFMPEG 403] id={classification.diagnostic_id} cause={classification.cause} '
            f'guild="{_safe_title(guild_name)}" '
            f'title="{_safe_title(song_title)}" {detail_text}'.rstrip()
        )
        for label, items in (("확인", classification.evidence), ("추정", classification.suspected),
                             ("미확인", classification.unknown), ("점검", classification.actions)):
            if items:
                print(f"[FFMPEG 403 DETAIL] id={classification.diagnostic_id} {label}: " + " / ".join(items))
        # Bound and deduplicate the actual error lines, masking signed URLs and credentials.
        error_lines = list(dict.fromkeys(
            sanitize_diagnostic(line) for line in lines
            if HTTP_403_PATTERN.search(line) or "error" in line.lower() or "failed" in line.lower()
        ))
        for line in error_lines[:3]:
            print(f"[FFMPEG 403 STDERR] id={classification.diagnostic_id} {line}")
        if classification_callback is not None:
            classification_callback(classification)
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

    def __init__(
        self,
        max_bytes: int = MAX_STDERR_BYTES,
        line_callback: Callable[[str], None] | None = None,
    ) -> None:
        self._max_bytes = max_bytes
        self._line_callback = line_callback
        self._line_buffer = bytearray()
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
                self._handle_lines(chunk)
                with self._buffer_lock:
                    self._buffer.extend(chunk)
                    overflow = len(self._buffer) - self._max_bytes
                    if overflow > 0:
                        del self._buffer[:overflow]
        finally:
            if self._line_callback is not None and self._line_buffer:
                self._emit_line(bytes(self._line_buffer))
                self._line_buffer.clear()
            self._reader.close()

    def _handle_lines(self, chunk: bytes) -> None:
        if self._line_callback is None:
            return

        self._line_buffer.extend(chunk)
        while (newline_index := self._line_buffer.find(b"\n")) >= 0:
            line = bytes(self._line_buffer[:newline_index])
            del self._line_buffer[:newline_index + 1]
            self._emit_line(line)

        if len(self._line_buffer) > self._max_bytes:
            del self._line_buffer[:-self._max_bytes]

    def _emit_line(self, line: bytes) -> None:
        try:
            self._line_callback(line.decode("utf-8", errors="replace").rstrip("\r"))
        except Exception as error:
            print(f"[WARNING] Failed to inspect live FFmpeg stderr: {error}")

    def finish(self) -> bytes:
        with self._finish_lock:
            if not self._finished:
                self.writer.close()
                self._reader_thread.join()
                self._finished = True

        with self._buffer_lock:
            return bytes(self._buffer)


class FilteredFFmpegPCMAudio(discord.FFmpegPCMAudio):
    """Capture FFmpeg stderr, pace PCM frames, and report useful messages."""

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
        self._classification_lock = threading.Lock()
        self._cleaned = True
        self._access_denied_classification: AccessDeniedClassification | None = None
        self._next_frame_at: float | None = None
        self._first_http_403_at: float | None = None
        self._stderr_context = {
            "play_url": source,
            "song_title": song_title,
            "guild_name": guild_name,
            "stream_metadata": dict(stream_metadata or {}),
            "expected_ip": expected_ip,
            "request_headers": configured_ffmpeg_headers(ffmpeg_options),
        }
        self._stderr_capture = BoundedStderrCapture(line_callback=self._report_reconnect_line)
        try:
            super().__init__(source, stderr=self._stderr_capture.writer, **ffmpeg_options)
        except Exception:
            self._stderr_capture.finish()
            raise
        self._cleaned = False

    def _remember_access_denied(self, classification: AccessDeniedClassification) -> None:
        with self._classification_lock:
            self._access_denied_classification = classification

    def _report_reconnect_line(self, line: str) -> None:
        if self._first_http_403_at is None and HTTP_403_PATTERN.search(line):
            self._first_http_403_at = time.time()
        message = _format_reconnect_log(
            line,
            song_title=self._stderr_context["song_title"],
            guild_name=self._stderr_context["guild_name"],
        )
        if message is not None:
            print(message)

    def consume_access_denied_classification(self) -> AccessDeniedClassification | None:
        with self._classification_lock:
            classification = self._access_denied_classification
            self._access_denied_classification = None
            return classification

    def read(self) -> bytes:
        frame = super().read()
        if not frame:
            return frame

        now = time.perf_counter()
        if self._next_frame_at is None or now >= self._next_frame_at:
            self._next_frame_at = now + AUDIO_FRAME_DURATION
            return frame

        time.sleep(self._next_frame_at - now)
        self._next_frame_at += AUDIO_FRAME_DURATION
        return frame

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
                    report_ffmpeg_stderr(
                        stderr_output,
                        **self._stderr_context,
                        observed_at=self._first_http_403_at,
                        classification_callback=self._remember_access_denied,
                    )
                except Exception as error:
                    print(f"[WARNING] Failed to inspect FFmpeg stderr: {error}")
