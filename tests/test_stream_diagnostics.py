from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from modules.stream_diagnostics import (
    ExtractionDiagnosticLogger,
    classify_http_403,
    configured_ffmpeg_headers,
    format_http_403_message,
    sanitize_diagnostic,
)
from modules.ffmpeg_log_filter import FilteredFFmpegPCMAudio, report_ffmpeg_stderr
from modules.youtube_extract_worker import extract_video


URL = "https://r1.googlevideo.com/videoplayback?expire=2000&c=VISIONOS&ip=192.0.2.1&sig=secret-signature"
METADATA = {
    "availability": "public", "format_id": "140", "protocol": "https",
    "yt_dlp_version": "test", "age_limit": 0, "extracted_at": 900,
    "http_headers": {"User-Agent": "extraction-agent", "Referer": "https://youtube.com/"},
    "extraction_warning_codes": [], "extraction_warning_count": 0,
}


class ClassificationTests(unittest.TestCase):
    def test_unknown_is_not_a_po_token_verdict(self):
        result = classify_http_403(URL, METADATA, now=1000,
                                   request_headers=METADATA["http_headers"])
        self.assertEqual(result.cause, "youtube_access_context_rejected")
        self.assertIn("cause_certainty=unknown", result.details)
        self.assertIn("po_token_parameter=absent", result.details)
        self.assertFalse(result.suspected)
        self.assertTrue(any("PO Token" in text for text in result.unknown))

    def test_expiry_keeps_other_evidence_and_priority(self):
        result = classify_http_403(URL, METADATA, now=2000, expected_ip="192.0.2.2",
                                   request_headers={"User-Agent": "playback-agent"})
        self.assertEqual(result.cause, "expired_stream_url")
        self.assertIn("url_remaining_seconds=0", result.details)
        self.assertIn("url_vs_configured_ip=mismatch", result.details)
        self.assertIn("header_mismatch=user-agent", result.details)
        self.assertIn("headers_not_explicitly_forwarded=referer", result.details)
        self.assertGreaterEqual(len(result.suspected), 2)

    def test_headers_compare_case_insensitively_without_publishing_values(self):
        result = classify_http_403(URL, METADATA, now=1000,
                                   request_headers={"USER-AGENT": "different-secret-agent"})
        self.assertEqual(result.cause, "request_headers_mismatch")
        self.assertNotIn("different-secret-agent", str(result))
        self.assertNotIn("extraction-agent", str(result))

    def test_missing_header_context_is_unknown(self):
        result = classify_http_403(URL, {}, now=1000)
        self.assertIn("header_comparison=unknown", result.details)
        self.assertIn("extraction_warnings=not_recorded", result.details)
        self.assertEqual(result.cause, "youtube_access_context_rejected")

    def test_configured_ip_does_not_claim_actual_egress(self):
        result = classify_http_403(URL, now=1000, expected_ip="192.0.2.2")
        self.assertEqual(result.cause, "configured_egress_ip_mismatch")
        self.assertIn("actual_egress_ip=unverified", result.details)
        self.assertNotIn("192.0.2", str(result))
        self.assertIn("미측정", " ".join(result.evidence))

    def test_equivalent_ipv6_is_not_a_mismatch(self):
        url = URL.replace("192.0.2.1", "2001:db8::1")
        result = classify_http_403(url, now=1000, expected_ip="2001:0db8:0:0:0:0:0:1")
        self.assertIn("url_vs_configured_ip=match", result.details)

    def test_restrictions_remain_suspected_causes(self):
        for metadata, cause in (
            ({"availability": "private"}, "authentication_or_entitlement_required"),
            ({"availability": "needs_auth"}, "authentication_or_entitlement_required"),
            ({"age_limit": 18}, "age_restricted_request_context"),
            ({"playable_in_embed": False}, "embed_playback_restricted"),
        ):
            with self.subTest(cause=cause):
                result = classify_http_403(URL.replace("VISIONOS", "WEB_EMBEDDED"), metadata, now=1000)
                self.assertEqual(result.cause, cause)
                self.assertIn("cause_certainty=suspected", result.details)

    def test_unlisted_and_non_embedded_are_not_restriction_verdicts(self):
        result = classify_http_403(URL, {"availability": "unlisted", "playable_in_embed": False}, now=1000)
        self.assertEqual(result.cause, "youtube_access_context_rejected")

    def test_warning_classifications(self):
        cases = {
            "Signature solving failed": "signature_extraction_warning",
            "No supported JavaScript runtime could be found": "signature_extraction_warning",
            "GVS PO Token was not provided": "po_token_warning",
            "Video is not available in your country": "region_restriction_warning",
            "HTTP Error 429: Too Many Requests": "rate_limit_warning",
            "Sign in to confirm your age": "authentication_or_entitlement_required",
        }
        for warning, cause in cases.items():
            with self.subTest(warning=warning):
                logger = ExtractionDiagnosticLogger()
                logger.warning(warning)
                result = classify_http_403(URL, {"extraction_warning_codes": logger.codes}, now=1000)
                self.assertEqual(result.cause, cause)
                self.assertIn("다른 후보 형식", " ".join(result.suspected))

    def test_invalid_or_extreme_metadata_does_not_break_reporting(self):
        for expire in ("bad", "nan", "inf", "1e99", "-1e99"):
            with self.subTest(expire=expire):
                result = classify_http_403(URL.replace("expire=2000", f"expire={expire}"),
                                           {"extracted_at": "bad", "age_limit": "nan"}, now=1000)
                self.assertTrue(result.diagnostic_id)
        self.assertEqual(classify_http_403("https://example.com/a", now=1000).cause,
                         "remote_access_denied")

    def test_ffmpeg_option_inspection(self):
        result = configured_ffmpeg_headers({"before_options":
            '-reconnect 1 -user_agent "a b" -headers "Origin: https://example.com\r\nReferer: page\r\n"'})
        self.assertEqual(result, {"user-agent": "a b", "origin": "https://example.com", "referer": "page"})
        self.assertIsNone(configured_ffmpeg_headers({"before_options": '-user_agent "unclosed'}))


class OutputTests(unittest.TestCase):
    def test_cmd_and_discord_share_diagnostic_and_do_not_leak_secrets(self):
        capture, classifications = StringIO(), []
        stderr = f"HTTP error 403 Forbidden URL {URL}\nAuthorization: Bearer secret-auth\nError Cookie: secret-cookie"
        with redirect_stdout(capture):
            self.assertTrue(report_ffmpeg_stderr(stderr, play_url=URL, song_title="test",
                guild_name="guild", stream_metadata=METADATA, observed_at=1000,
                request_headers={"User-Agent": "playback-agent"}, classification_callback=classifications.append))
        result = classifications[0]
        message = format_http_403_message("test", result)
        self.assertIn(result.diagnostic_id, capture.getvalue())
        self.assertIn(result.diagnostic_id, message)
        for label in ("확인:", "추정:", "미확인:", "점검:"):
            self.assertIn(label, message)
            self.assertIn(label, capture.getvalue())
        self.assertIn("FFMPEG 403 STDERR", capture.getvalue())
        for secret in ("secret-signature", "secret-auth", "secret-cookie", "192.0.2.1"):
            self.assertNotIn(secret, capture.getvalue() + message)

    def test_sanitization_and_discord_length(self):
        text = sanitize_diagnostic("https://host/path?token=secret\nCookie: secret-cookie\n2001:db8::1\x1b")
        self.assertNotIn("secret", text)
        self.assertNotIn("2001:db8", text)
        result = classify_http_403(URL, METADATA, now=1000)
        result = replace(result, evidence=("긴 진단" * 1000,))
        message = format_http_403_message("*" * 5000, result)
        self.assertLess(len(message + "\n처리: 만료된 주소를 갱신하여 재생을 다시 시도했습니다."), 2000)
        self.assertIn(result.diagnostic_id, message)

    def test_first_error_time_survives_stderr_buffer_rollover(self):
        capture, results = StringIO(), []
        with redirect_stdout(capture):
            report_ffmpeg_stderr("unrelated final stderr", play_url=URL, song_title="test", guild_name="guild",
                                 observed_at=1000, classification_callback=results.append)
        self.assertNotEqual(results[0].cause, "expired_stream_url")
        self.assertIn("url_remaining_seconds=1000", results[0].details)

    def test_no_403_does_not_trigger_callback(self):
        callback = Mock()
        with redirect_stdout(StringIO()):
            self.assertFalse(report_ffmpeg_stderr("ordinary message", play_url=URL, song_title="test",
                                                 guild_name="guild", classification_callback=callback))
        callback.assert_not_called()

    def test_live_capture_records_first_403_only(self):
        source = object.__new__(FilteredFFmpegPCMAudio)
        source._first_http_403_at = None
        source._stderr_context = {"song_title": "test", "guild_name": "guild"}
        with patch("modules.ffmpeg_log_filter.time.time", side_effect=[1000, 3000]):
            source._report_reconnect_line("HTTP error 403 Forbidden")
            source._report_reconnect_line("HTTP error 403 Forbidden")
        self.assertEqual(source._first_http_403_at, 1000)
        # Avoid invoking Pycord cleanup on an object whose subprocess was never started.
        source._cleaned = True
        source._cleanup_lock = threading.Lock()


class ExtractionTests(unittest.TestCase):
    def test_worker_transports_warning_codes_without_raw_credentials(self):
        def fake_ydl(options):
            options["logger"].warning("GVS PO Token was not provided https://example.com/?token=secret")
            client = Mock()
            client.__enter__ = Mock(return_value=client)
            client.__exit__ = Mock(return_value=False)
            client.extract_info.return_value = {"title": "test", "url": URL, "http_headers": {"User-Agent": "agent"}}
            return client
        with patch("modules.youtube_extract_worker.yt_dlp.YoutubeDL", side_effect=fake_ydl):
            result = extract_video("https://youtube.com/watch?v=test", "fallback")
        self.assertEqual(len(result), 5)
        self.assertEqual(result[4]["extraction_warning_codes"], ["po_token"])
        self.assertEqual(result[4]["extraction_warning_count"], 1)
        self.assertIn("extracted_at", result[4])
        self.assertNotIn("token=secret", str(result[4]))


class DiscordFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from cogs import Audio_player
        self.player = Audio_player
        self.state = Audio_player.ServerInfo()
        self.state.voice_client = Mock()
        self.state.embed_channel = SimpleNamespace(send=AsyncMock())
        self.song = {"title": "test"}
        self.state.song_cache = self.song
        self.state.playback_id = 1
        self.state.playback_end_token = object()
        Audio_player.server_info_dict[999] = self.state

    async def asyncTearDown(self):
        self.player.server_info_dict.pop(999, None)

    async def handle(self, result):
        await self.player._handle_playback_end(999, Mock(), self.state, self.song, 1,
                                               self.state.playback_end_token, result)

    async def test_failure_advances_queue_then_sends_full_diagnostic(self):
        result = classify_http_403(URL, METADATA, now=1000)
        order = []
        async def advance(*args):
            order.append("advance")
        async def send(message, **kwargs):
            order.append("send")
            self.assertIn(result.diagnostic_id, message)
            self.assertIn("미확인:", message)
            self.assertFalse(kwargs["allowed_mentions"].everyone)
        self.state.embed_channel.send.side_effect = send
        with patch.object(self.player, "play_loop", side_effect=advance):
            await self.handle(result)
        self.assertEqual(order, ["advance", "send"])
        self.assertIsNone(self.state.song_cache)

    async def test_expiry_retry_also_notifies_discord(self):
        result = classify_http_403(URL, METADATA, now=2001)
        with patch.object(self.player, "_refresh_and_retry_expired_stream", new=AsyncMock(return_value=True)):
            await self.handle(result)
        message = self.state.embed_channel.send.call_args.args[0]
        self.assertIn(result.diagnostic_id, message)
        self.assertIn("다시 시도", message)

    async def test_stopped_refresh_does_not_send_stale_message(self):
        result = classify_http_403(URL, METADATA, now=2001)
        with patch.object(self.player, "_refresh_and_retry_expired_stream", new=AsyncMock(return_value=None)):
            await self.handle(result)
        self.state.embed_channel.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
