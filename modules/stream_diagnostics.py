"""Evidence collected locally for playback failures; never infer a CDN verdict."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
import math
import re
import shlex
import time
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse
from uuid import uuid4


CAUSE_LABELS = {
    "remote_access_denied": "원격 서버 접근 거부 — 상세 원인 미확인",
    "expired_stream_url": "재생 주소 만료 확인",
    "configured_egress_ip_mismatch": "URL IP와 설정 IP 불일치 — 실제 경로 확인 필요",
    "authentication_or_entitlement_required": "로그인·시청 권한 제한 의심",
    "age_restricted_request_context": "연령 제한 인증 문제 의심",
    "embed_playback_restricted": "외부 플레이어 제한 의심",
    "request_headers_mismatch": "추출·재생 요청 헤더 차이 의심",
    "signature_extraction_warning": "서명·재생 URL 처리 문제 의심",
    "po_token_warning": "PO Token 관련 문제 의심",
    "region_restriction_warning": "지역 제한 의심",
    "rate_limit_warning": "요청 제한·봇 차단 의심",
    "youtube_cdn_access_denied": "YouTube 재생 요청 거부 — 상세 원인 미확인",
    "youtube_access_context_rejected": "YouTube 재생 요청 거부 — 상세 원인 미확인",
}


def sanitize_diagnostic(value: Any, limit: int = 300) -> str:
    """Redact URLs, credentials and addresses before truncation or publication."""
    text = str(value)
    text = re.sub(r"https?://[^\s<>\"']+", "[URL 비공개]", text, flags=re.I)
    text = re.sub(
        r"(?im)\b(?:cookie|set-cookie|authorization|proxy-authorization|"
        r"po[_ -]?token|visitor[_ -]?(?:data|id))\s*[:=][^\r\n]*",
        "[인증 정보 비공개]", text,
    )
    text = re.sub(
        r"(?i)\b(?:sig|signature|lsig|pot|token|ip)=[^\s&]+", "[비공개]", text,
    )
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[IP 비공개]", text)
    text = re.sub(r"(?<![\w:])(?:[a-fA-F0-9]{0,4}:){2,}[a-fA-F0-9:.%]+", "[IP 비공개]", text)
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text).replace('"', "'")[:limit]


def _tag(value: Any) -> str:
    return re.sub(r"[^a-zA-Z0-9_.:+-]", "_", sanitize_diagnostic(value, 70))


@dataclass(frozen=True)
class AccessDeniedClassification:
    cause: str
    details: tuple[str, ...]
    evidence: tuple[str, ...] = ()
    suspected: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    diagnostic_id: str = ""


def translate_http_403_cause(cause: str) -> str:
    return CAUSE_LABELS.get(cause, "접근 거부 원인 확인 불가")


def format_http_403_message(title: str | None, result: AccessDeniedClassification) -> str:
    # Shared diagnostic content with CMD; keep below Discord's 2,000-character limit.
    safe_title = sanitize_diagnostic(title or "알 수 없는 곡", 160)
    safe_title = re.sub(r"([\\`*_{}\[\]()#+.!|>~])", r"\\\1", safe_title)
    lines = [f"{safe_title} 재생에 실패했습니다. (HTTP 403)",
             f"분류: {translate_http_403_cause(result.cause)}"]
    if result.diagnostic_id:
        lines.append(f"진단 ID: {result.diagnostic_id} (CMD 로그와 동일)")
    for label, items in (("확인", result.evidence), ("추정", result.suspected),
                         ("미확인", result.unknown), ("점검", result.actions)):
        if items:
            lines.append(f"{label}: " + " / ".join(items))
    message = "\n".join(lines)
    if len(message) > 1900:
        message = message[:1840] + "\n… 나머지 진단은 동일 ID의 CMD 로그에서 확인하세요."
    return message


WARNING_RULES = {
    "signature": r"(?:signature|nsig|n challenge|javascript challenge).{0,100}(?:fail|unable|error)|(?:fail|unable).{0,100}(?:signature|nsig)",
    "js_runtime": r"no supported javascript runtime|javascript runtime.{0,80}(?:missing|not found|unavailable)",
    "po_token": r"po[ _-]?token.{0,150}(?:not provided|missing|required|invalid|failed)|(?:missing|requires?).{0,80}po[ _-]?token",
    "region": r"not (?:available|allowed) in your country|geo.?restricted|geographic restriction|blocked in your country",
    "rate_limit": r"too many requests|rate.?limit|http error 429|confirm you.re not a bot|unusual traffic",
    "authentication": r"login required|sign in to confirm your age|only available (?:to|for).{0,40}(?:members|subscribers|premium)",
}


class ExtractionDiagnosticLogger:
    """Keep bounded warning codes; worker stdout must remain valid JSON."""
    def __init__(self) -> None:
        self.codes: list[str] = []
        self.warning_count = 0

    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        self.warning_count += 1
        for code, pattern in WARNING_RULES.items():
            if code not in self.codes and re.search(pattern, str(message), re.I):
                self.codes.append(code)

    def error(self, message: str) -> None:
        self.warning(message)


def configured_ffmpeg_headers(options: Mapping[str, Any]) -> dict[str, str] | None:
    """Inspect the options passed to Pycord, not headers observed on the wire."""
    headers: dict[str, str] = {}
    try:
        arguments = shlex.split(str(options.get("before_options") or ""))
    except ValueError:
        return None
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option in {"-user_agent", "-referer", "-headers"}:
            if index + 1 == len(arguments):
                return None
            value = arguments[index + 1]
            if option == "-headers":
                for line in value.splitlines():
                    key, separator, content = line.partition(":")
                    if separator:
                        headers[key.strip().lower()] = content.strip()
            else:
                headers["user-agent" if option == "-user_agent" else "referer"] = value
            index += 2
        else:
            index += 1
    return headers


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError, OverflowError):
        return None


def classify_http_403(
    play_url: str,
    stream_metadata: Mapping[str, Any] | None = None,
    *,
    expected_ip: str | None = None,
    now: float | None = None,
    request_headers: Mapping[str, str] | None = None,
) -> AccessDeniedClassification:
    metadata = stream_metadata or {}
    try:
        parsed = urlparse(play_url)
        host = (parsed.hostname or "").lower()
        query = parse_qs(parsed.query)
    except ValueError:
        host, query = "", {}
    youtube = host == "googlevideo.com" or host.endswith(".googlevideo.com")
    def value(key: str) -> str | None:
        return (query.get(key) or [None])[0]

    observed_at = time.time() if now is None else now
    evidence: list[str] = []
    suspected: list[str] = []
    unknown: list[str] = []
    actions: list[str] = []
    candidates: list[tuple[int, str]] = []
    details = ["http_status=403", "diagnosis=local_evidence", "actual_egress_ip=unverified"]

    def suspect(priority: int, cause: str, explanation: str, action: str) -> None:
        candidates.append((priority, cause))
        suspected.append(explanation)
        if action not in actions:
            actions.append(action)

    for key in ("availability", "format_id", "protocol", "yt_dlp_version"):
        if metadata.get(key) is not None:
            details.append(f"{key}={_tag(metadata[key])}")
    client = _tag((value("c") or "unknown").upper())
    if youtube:
        details.append(f"client={client}")
    evidence.append(f"{'YouTube CDN' if youtube else '원격 서버'} 요청에서 403 수신")
    if youtube:
        evidence.append(f"클라이언트={client}, 형식={_tag(metadata.get('format_id', 'unknown'))}, "
                        f"yt-dlp={_tag(metadata.get('yt_dlp_version', 'unknown'))}")

    expires = _number(value("expire")) if youtube else None
    if expires is not None:
        remaining = int(expires - observed_at)
        details.append(f"url_remaining_seconds={remaining}")
        if observed_at >= expires:
            candidates.append((0, "expired_stream_url"))
            evidence.append(f"입력 URL 만료 ({abs(remaining)}초 경과, 403 감지 시각 기준)")
            actions.append("재생 주소 재추출")
            try:
                expired_at = datetime.fromtimestamp(expires, timezone(timedelta(hours=9)))
                details.append(f"expired_at={expired_at:%Y-%m-%d_%H:%M:%S_KST}")
            except (OverflowError, OSError, ValueError):
                pass
        else:
            evidence.append(f"입력 URL 만료까지 {remaining}초")
    elif youtube:
        details.append("url_expiry=unknown")
        unknown.append("URL 만료 시각")
    extracted_at = _number(metadata.get("extracted_at"))
    if extracted_at is not None:
        elapsed = int(observed_at - extracted_at)
        details.append(f"seconds_since_extraction={elapsed}")
        evidence.append(f"주소 추출 후 {elapsed}초 경과")

    if youtube:
        try:
            bound = ip_address(value("ip"))
            configured = ip_address(expected_ip) if expected_ip else None
        except ValueError:
            bound, configured = None, None
        comparison = "unknown" if bound is None or configured is None else (
            "match" if bound == configured else "mismatch")
        details.append(f"url_vs_configured_ip={comparison}")
        if comparison == "mismatch":
            evidence.append("URL에 연결된 IP ≠ 설정 VPN IP (실제 송신 IP는 미측정)")
            suspect(70, "configured_egress_ip_mismatch", "추출·재생 통신 경로 차이 가능",
                    "봇 서버의 VPN·프록시·IPv4/IPv6 경로 확인")
        elif comparison == "match":
            evidence.append("URL IP = 설정 VPN IP (실제 송신 경로 일치는 미확인)")
        unknown.append("실제 CDN 송신 IP·지역")

    availability = metadata.get("availability")
    if availability:
        evidence.append(f"공개 상태={_tag(availability)}")
    if availability in {"private", "premium_only", "subscriber_only", "needs_auth"}:
        suspect(10, "authentication_or_entitlement_required", "영상 메타데이터에 로그인·시청 권한 제한",
                "해당 계정의 로그인·시청 권한 확인")
    age = _number(metadata.get("age_limit"))
    if age is not None:
        details.append(f"age_limit={int(age)}")
        if age >= 18:
            evidence.append(f"연령 제한={int(age)}세")
            suspect(20, "age_restricted_request_context", "연령 제한 영상의 인증 정보 부족 가능",
                    "영상의 연령 인증·로그인 상태 확인")
    if metadata.get("playable_in_embed") is False:
        details.append("playable_in_embed=false")
        evidence.append("외부 임베드 재생 불가")
        if "EMBEDDED" in client:
            suspect(30, "embed_playback_restricted", "임베드 불가 영상에 임베드 클라이언트 사용",
                    "추출 클라이언트의 영상 재생 지원 여부 확인")

    extracted_headers = metadata.get("http_headers")
    if isinstance(extracted_headers, Mapping) and extracted_headers and request_headers is not None:
        extracted = {str(k).lower(): str(v).strip() for k, v in extracted_headers.items()}
        requested = {str(k).lower(): str(v).strip() for k, v in request_headers.items()}
        # FFmpeg supplies some defaults itself; only explicit context headers are compared.
        context_headers = {"user-agent", "referer", "origin", "cookie", "authorization"}
        mismatched = sorted(k for k in extracted.keys() & context_headers
                            if k in requested and extracted[k] != requested[k])
        missing = sorted(k for k in extracted.keys() & context_headers if k not in requested)
        details.extend((f"header_mismatch={','.join(mismatched) or 'none'}",
                        f"headers_not_explicitly_forwarded={','.join(missing) or 'none'}"))
        if mismatched or missing:
            if mismatched:
                evidence.append("추출·FFmpeg 설정 헤더 값 다름: " + ", ".join(mismatched))
            if missing:
                evidence.append("FFmpeg에 명시적으로 전달 안 된 추출 헤더: " + ", ".join(missing))
            suspect(60, "request_headers_mismatch", "요청 헤더 차이가 403에 영향을 주었을 가능성",
                    "yt-dlp 추출 헤더와 FFmpeg 요청 헤더 일치 여부 확인")
        else:
            evidence.append("주요 추출 헤더와 FFmpeg 설정 값 일치")
    else:
        details.append("header_comparison=unknown")
        unknown.append("추출·재생 헤더 비교 (기록 부족)")
    unknown.append("실제 전송 헤더·인증 유효성")

    warning_codes = metadata.get("extraction_warning_codes")
    if isinstance(warning_codes, (list, tuple)):
        known_codes = [code for code in WARNING_RULES if code in warning_codes]
        details.append("extraction_warnings=" + (",".join(known_codes) or "none_classified"))
        warning_specs = {
            "signature": (40, "signature_extraction_warning", "서명·n challenge 처리 실패 경고", "yt-dlp·JS 런타임·challenge 처리 환경 확인"),
            "js_runtime": (45, "signature_extraction_warning", "JS 런타임 부족 경고", "yt-dlp·JS 런타임·challenge 처리 환경 확인"),
            "po_token": (50, "po_token_warning", "PO Token 누락·요구·실패 경고", "선택 클라이언트의 PO Token 공급 상태 확인"),
            "region": (15, "region_restriction_warning", "지역 제한 경고", "해당 영상의 허용 지역과 서버 통신 경로 확인"),
            "rate_limit": (35, "rate_limit_warning", "요청 제한·봇 확인 경고", "요청 빈도·동시 추출 수와 IP 차단 여부 확인"),
            "authentication": (10, "authentication_or_entitlement_required", "로그인·시청 권한 경고", "해당 계정의 로그인·시청 권한 확인"),
        }
        for code in known_codes:
            priority, cause, label, action = warning_specs[code]
            evidence.append("yt-dlp: " + label)
            suspect(priority, cause, label + "의 영향 가능 (다른 후보 형식의 경고일 수 있음)", action)
        details.append(f"extraction_warning_count={_tag(metadata.get('extraction_warning_count', 'unknown'))}")
    else:
        details.append("extraction_warnings=not_recorded")
        unknown.append("추출 경고 (이전 캐시 등으로 기록 없음)")

    if youtube:
        signed = any(value(key) for key in ("sig", "signature", "lsig"))
        has_pot = bool(value("pot"))
        details.extend((f"signature_parameter={'present' if signed else 'absent'}",
                        f"po_token_parameter={'present' if has_pot else 'absent'}",
                        f"n_parameter={'present' if value('n') else 'absent'}"))
        evidence.append(f"입력 URL: 서명 파라미터 {'있음' if signed else '없음'}, "
                        f"PO Token 파라미터 {'있음' if has_pot else '없음'} (유효성 판단 불가)")
        unknown.append("서명·PO Token 필요 여부와 유효성, 지역·요청 제한의 실제 적용 여부")
    unknown.append("서버 내부의 정확한 거부 사유 (403만으로 확정 불가)")
    if not actions:
        actions.append("같은 서버에서 URL 재추출 후 재현 여부와 yt-dlp 경고 확인")
    default = "youtube_access_context_rejected" if youtube else "remote_access_denied"
    cause = min(candidates, key=lambda entry: entry[0])[1] if candidates else default
    details.append("cause_certainty=" + ("expired_url_observed" if cause == "expired_stream_url"
                                         else "suspected" if candidates else "unknown"))
    return AccessDeniedClassification(cause, tuple(details), tuple(evidence), tuple(suspected),
                                      tuple(unknown), tuple(actions), uuid4().hex[:10])
