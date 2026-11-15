"""Small, safe client for the local HDHomeRun HTTP API.

The FF plugin uses this module only to *read* a device's discovery and
lineup data.  It deliberately has no API for changing a device channel map,
tuner configuration, or firmware.  Frequency scanning is represented as a
pure data plan; the caller must later reserve an idle tuner and implement a
device-specific scanner explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import ipaddress
import json
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_TIMEOUT_SECONDS = 5.0
USER_AGENT = "hdhomerun_ff/0.1"
_SAFE_HOST_SUFFIXES = (".local", ".lan", ".home", ".internal")
_SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_MODULATIONS = {"VSB/8", "QAM/256", "QAM/64", "QAM/AUTO"}


class HDHomeRunError(RuntimeError):
    """Base class for safe, user-displayable HDHomeRun client failures."""


class HDHomeRunValidationError(HDHomeRunError):
    """Raised when a configured URL or device response is malformed."""


class HDHomeRunTransportError(HDHomeRunError):
    """Raised when the local device cannot be reached or returns bad HTTP."""


@dataclass(frozen=True)
class DeviceInfo:
    """Non-secret fields exposed by ``/discover.json``."""

    device_id: str
    friendly_name: str
    model_number: str
    firmware_version: str
    tuner_count: int
    base_url: str


@dataclass(frozen=True)
class LineupChannel:
    """A validated direct stream entry returned by ``/lineup.json``."""

    channel_id: str
    guide_number: str
    guide_name: str
    stream_url: str
    video_codec: str = ""
    audio_codec: str = ""


@dataclass(frozen=True)
class LineupResult:
    channels: Sequence[LineupChannel]
    skipped_entries: int = 0


@dataclass(frozen=True)
class ScanTarget:
    """One mux to inspect using a caller-selected *idle* tuner."""

    index: int
    frequency_hz: int
    modulation: str
    delivery_system: str


@dataclass(frozen=True)
class ScanPlan:
    """A side-effect-free plan derived from an installed frequency preset."""

    preset_id: str
    delivery_system: str
    targets: Sequence[ScanTarget]
    requires_idle_tuner: bool = True
    changes_device_channelmap: bool = False


@dataclass(frozen=True)
class DeviceSnapshot:
    device: DeviceInfo
    lineup: LineupResult


Transport = Callable[[str, float], Any]


class _NoRedirect(HTTPRedirectHandler):
    """Do not follow a device-provided redirect to an arbitrary host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _normalise_host(hostname: str) -> str:
    return str(hostname or "").strip().rstrip(".").lower()


def _is_cgnat(address: ipaddress._BaseAddress) -> bool:
    return isinstance(address, ipaddress.IPv4Address) and address in ipaddress.ip_network(
        "100.64.0.0/10"
    )


def is_private_or_local_host(hostname: str) -> bool:
    """Return whether a host is appropriate for a LAN tuner endpoint.

    Public hostnames are rejected so an administrator setting cannot turn the
    plugin into a general-purpose outbound request proxy.  Local host names
    remain useful for mDNS devices such as ``hdhomerun.local``.
    """

    host = _normalise_host(hostname)
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not _SAFE_HOST_RE.fullmatch(host):
            return False
        return "." not in host or host.endswith(_SAFE_HOST_SUFFIXES)

    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or _is_cgnat(address)
    )


def normalize_base_url(value: str) -> str:
    """Validate and canonicalise a private HDHomeRun API base URL.

    Only an origin is accepted.  Paths, credentials, queries, fragments and
    public Internet endpoints are intentionally rejected.
    """

    raw = str(value or "").strip()
    if not raw:
        raise HDHomeRunValidationError("HDHomeRun 주소를 입력하세요.")
    if "://" not in raw:
        raw = f"http://{raw}"

    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"}:
        raise HDHomeRunValidationError("HDHomeRun 주소는 http 또는 https여야 합니다.")
    if parts.username or parts.password:
        raise HDHomeRunValidationError("HDHomeRun 주소에 계정 정보를 넣을 수 없습니다.")
    if parts.query or parts.fragment:
        raise HDHomeRunValidationError("HDHomeRun 주소에는 경로 인자나 조각을 넣을 수 없습니다.")
    if parts.path not in {"", "/"}:
        raise HDHomeRunValidationError("HDHomeRun 주소에는 호스트와 포트만 입력하세요.")
    host = _normalise_host(parts.hostname or "")
    if not is_private_or_local_host(host):
        raise HDHomeRunValidationError("사설망 HDHomeRun 주소만 사용할 수 있습니다.")
    try:
        port = parts.port
    except ValueError as exc:
        raise HDHomeRunValidationError("HDHomeRun 포트가 올바르지 않습니다.") from exc
    if port is not None and not 1 <= port <= 65535:
        raise HDHomeRunValidationError("HDHomeRun 포트가 올바르지 않습니다.")

    # Keep an IPv6 host syntactically valid after lower-casing it.
    rendered_host = f"[{host}]" if ":" in host else host
    netloc = rendered_host if port is None else f"{rendered_host}:{port}"
    return urlunsplit((parts.scheme.lower(), netloc, "", "", ""))


def _safe_text(value: Any, maximum: int = 256) -> str:
    text = str(value or "").strip()
    if len(text) > maximum:
        raise HDHomeRunValidationError("HDHomeRun 응답의 텍스트 값이 너무 깁니다.")
    return text


def _safe_identifier(value: Any, fallback: str = "unknown") -> str:
    text = _safe_text(value, 128)
    text = _SAFE_ID_RE.sub("-", text).strip("-.").lower()
    return text or fallback


def _origin_parts(url: str) -> SplitResult:
    return urlsplit(url)


def _stream_url_for_origin(value: Any, base_url: str) -> str:
    """Accept only a device-local HTTP stream URL from a lineup response."""

    raw = _safe_text(value, 2048)
    if not raw:
        raise HDHomeRunValidationError("채널 스트림 주소가 없습니다.")
    parts = urlsplit(raw)
    base = urlsplit(base_url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise HDHomeRunValidationError("채널 스트림 주소 형식이 올바르지 않습니다.")
    if parts.username or parts.password or parts.fragment:
        raise HDHomeRunValidationError("채널 스트림 주소 형식이 올바르지 않습니다.")
    if _normalise_host(parts.hostname) != _normalise_host(base.hostname or ""):
        raise HDHomeRunValidationError("다른 호스트의 채널 스트림 주소는 허용하지 않습니다.")
    try:
        stream_port = parts.port
        base_port = base.port
    except ValueError as exc:
        raise HDHomeRunValidationError("채널 스트림 포트가 올바르지 않습니다.") from exc
    if stream_port is not None and not 1 <= stream_port <= 65535:
        raise HDHomeRunValidationError("채널 스트림 포트가 올바르지 않습니다.")

    # HDHomeRun lineup entries use /auto/...; a tuner endpoint is also local
    # but not a general URL proxy.  Accept only these two documented shapes.
    path = parts.path or ""
    if not (path.startswith("/auto/") or path.startswith("/tuner")):
        raise HDHomeRunValidationError("알 수 없는 HDHomeRun 스트림 경로입니다.")
    if stream_port is None:
        stream_port = base_port
    rendered_host = f"[{_normalise_host(parts.hostname)}]" if ":" in _normalise_host(parts.hostname) else _normalise_host(parts.hostname)
    netloc = rendered_host if stream_port is None else f"{rendered_host}:{stream_port}"
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def stable_channel_id(device_id: str, guide_number: str, stream_url: str) -> str:
    """Make a persistent DB ID that survives display-name changes."""

    parsed = urlsplit(stream_url)
    path_key = f"{parsed.path}?{parsed.query}"
    digest = sha256(
        f"{_safe_identifier(device_id)}\0{guide_number.strip()}\0{path_key}".encode("utf-8")
    ).hexdigest()[:16]
    return f"hdhr:{_safe_identifier(device_id)}:{_safe_identifier(guide_number, 'channel')}:{digest}"


def _default_transport(url: str, timeout: float) -> Any:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            status = getattr(response, "status", response.getcode())
            if status != 200:
                raise HDHomeRunTransportError(f"HDHomeRun 응답 오류: HTTP {status}")
            return json.loads(response.read().decode("utf-8"))
    except HDHomeRunTransportError:
        raise
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise HDHomeRunTransportError(f"HDHomeRun 연결 또는 JSON 응답 실패: {exc}") from exc


class HDHomeRunClient:
    """Read-only client for a configured local HDHomeRun device."""

    def __init__(
        self,
        base_url: str,
        transport: Optional[Transport] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self._transport = transport or _default_transport
        try:
            self.timeout = max(1.0, min(float(timeout), 30.0))
        except (TypeError, ValueError) as exc:
            raise HDHomeRunValidationError("HDHomeRun 시간 제한이 올바르지 않습니다.") from exc

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            raise ValueError("internal endpoint must start with /")
        return f"{self.base_url}{path}"

    def _fetch(self, path: str) -> Any:
        try:
            return self._transport(self._url(path), self.timeout)
        except HDHomeRunError:
            raise
        except Exception as exc:
            raise HDHomeRunTransportError(f"HDHomeRun 요청 실패: {exc}") from exc

    def fetch_discover(self) -> DeviceInfo:
        raw = self._fetch("/discover.json")
        if not isinstance(raw, Mapping):
            raise HDHomeRunValidationError("discover.json 응답이 객체가 아닙니다.")

        # DeviceID is normally always supplied.  If a nonstandard device does
        # omit it, derive a local-only stable fallback so two configured boxes
        # cannot silently share channel IDs in the FF database.
        device_fallback = f"device-{sha256(self.base_url.encode('utf-8')).hexdigest()[:12]}"
        device_id = _safe_identifier(raw.get("DeviceID"), device_fallback)
        friendly_name = _safe_text(raw.get("FriendlyName") or raw.get("DeviceType"), 256)
        model_number = _safe_text(raw.get("ModelNumber"), 256)
        firmware_version = _safe_text(raw.get("FirmwareVersion"), 256)
        try:
            tuner_count = int(raw.get("TunerCount", 0))
        except (TypeError, ValueError) as exc:
            raise HDHomeRunValidationError("HDHomeRun 튜너 수가 올바르지 않습니다.") from exc
        if not 1 <= tuner_count <= 32:
            raise HDHomeRunValidationError("HDHomeRun 튜너 수가 올바르지 않습니다.")
        return DeviceInfo(
            device_id=device_id,
            friendly_name=friendly_name,
            model_number=model_number,
            firmware_version=firmware_version,
            tuner_count=tuner_count,
            base_url=self.base_url,
        )

    def fetch_lineup(self, device_id: str) -> LineupResult:
        raw = self._fetch("/lineup.json")
        if not isinstance(raw, list):
            raise HDHomeRunValidationError("lineup.json 응답이 목록이 아닙니다.")
        if len(raw) > 10000:
            raise HDHomeRunValidationError("HDHomeRun 채널 수가 비정상적으로 많습니다.")

        channels: List[LineupChannel] = []
        skipped = 0
        seen = set()
        for item in raw:
            if not isinstance(item, Mapping):
                skipped += 1
                continue
            try:
                guide_number = _safe_text(item.get("GuideNumber"), 64)
                guide_name = _safe_text(item.get("GuideName"), 256)
                if not guide_number or not guide_name:
                    raise HDHomeRunValidationError("채널 번호 또는 이름이 없습니다.")
                stream_url = _stream_url_for_origin(item.get("URL"), self.base_url)
                channel_id = stable_channel_id(device_id, guide_number, stream_url)
                if channel_id in seen:
                    raise HDHomeRunValidationError("중복 채널입니다.")
                seen.add(channel_id)
                channels.append(
                    LineupChannel(
                        channel_id=channel_id,
                        guide_number=guide_number,
                        guide_name=guide_name,
                        stream_url=stream_url,
                        video_codec=_safe_text(item.get("VideoCodec"), 64).lower(),
                        audio_codec=_safe_text(item.get("AudioCodec"), 64).lower(),
                    )
                )
            except HDHomeRunValidationError:
                skipped += 1

        channels.sort(key=lambda row: _guide_number_sort_key(row.guide_number))
        return LineupResult(channels=tuple(channels), skipped_entries=skipped)

    def fetch_snapshot(self) -> DeviceSnapshot:
        device = self.fetch_discover()
        lineup = self.fetch_lineup(device.device_id)
        return DeviceSnapshot(device=device, lineup=lineup)

    @staticmethod
    def build_initial_scan_plan(preset: Mapping[str, Any]) -> ScanPlan:
        """Validate a preset and return an immutable plan without device I/O.

        The resulting plan intentionally has no side effects.  An eventual
        scanner must choose an idle tuner, acquire a lock and inspect each
        target; it must never alter the device-wide channel map automatically.
        """

        if not isinstance(preset, Mapping):
            raise HDHomeRunValidationError("주파수 프리셋 형식이 올바르지 않습니다.")
        preset_id = _safe_identifier(preset.get("id"), "custom-preset")
        delivery_system = _safe_text(preset.get("delivery_system"), 32).upper()
        if delivery_system not in {"ATSC", "ATSC-T"}:
            raise HDHomeRunValidationError("현재는 ATSC 주파수 프리셋만 지원합니다.")
        muxes = preset.get("muxes")
        if not isinstance(muxes, list) or not muxes:
            raise HDHomeRunValidationError("주파수 프리셋에 mux 목록이 없습니다.")
        if len(muxes) > 1000:
            raise HDHomeRunValidationError("주파수 프리셋 mux 수가 너무 많습니다.")

        targets: List[ScanTarget] = []
        seen = set()
        for index, mux in enumerate(muxes, start=1):
            if not isinstance(mux, Mapping):
                raise HDHomeRunValidationError(f"{index}번 mux 형식이 올바르지 않습니다.")
            try:
                frequency_hz = int(mux.get("frequency_hz"))
            except (TypeError, ValueError) as exc:
                raise HDHomeRunValidationError(f"{index}번 mux 주파수가 올바르지 않습니다.") from exc
            if not 40_000_000 <= frequency_hz <= 1_000_000_000:
                raise HDHomeRunValidationError(f"{index}번 mux 주파수 범위를 확인하세요.")
            modulation = _safe_text(mux.get("modulation"), 32).upper()
            if modulation not in _SAFE_MODULATIONS:
                raise HDHomeRunValidationError(f"{index}번 mux 변조 방식이 지원되지 않습니다.")
            key = (frequency_hz, modulation)
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                ScanTarget(
                    index=len(targets) + 1,
                    frequency_hz=frequency_hz,
                    modulation=modulation,
                    delivery_system=delivery_system,
                )
            )
        if not targets:
            raise HDHomeRunValidationError("사용 가능한 mux가 없습니다.")
        return ScanPlan(
            preset_id=preset_id,
            delivery_system=delivery_system,
            targets=tuple(targets),
        )


def _guide_number_sort_key(value: str) -> tuple:
    """Sort 2, 2.1 and nonnumeric guide numbers predictably."""

    parts = str(value or "").split(".")
    numeric: List[int] = []
    for part in parts:
        if not part.isdigit():
            return (1, str(value or "").casefold())
        numeric.append(int(part))
    return (0, tuple(numeric), str(value))

