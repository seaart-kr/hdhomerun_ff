"""Safe HDHomeRun TCP-control helpers for explicit frequency presets.

``/lineup.json`` is the device's configured lineup, not a cable-frequency
preset scan.  The HDHomeRun control protocol can instead tune an *idle*,
lock-key-protected tuner to ``auto:<frequency>`` and return streaminfo.  This
module intentionally does not alter the global device channel map or force a
lock held by another client.

A scan result is service-discovery data, not a durable playback URL.  Raw
frequency playback must retain a tuner lease for the lifetime of an HLS
session; that separate lifecycle belongs to the playback layer.
"""

from __future__ import annotations

from dataclasses import dataclass
import binascii
import secrets
import socket
import struct
import time
from typing import Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

try:  # FlaskFarm package import.
    from .hdhomerun import (
        HDHomeRunError,
        HDHomeRunTransportError,
        HDHomeRunValidationError,
        ScanTarget,
        is_private_or_local_host,
        normalize_base_url,
    )
except ImportError:  # Unit tests import modules directly from the plugin root.
    from hdhomerun import (  # type: ignore
        HDHomeRunError,
        HDHomeRunTransportError,
        HDHomeRunValidationError,
        ScanTarget,
        is_private_or_local_host,
        normalize_base_url,
    )


CONTROL_PORT = 65001
TYPE_GETSET_REQ = 0x0004
TYPE_GETSET_RPY = 0x0005
TAG_GETSET_NAME = 0x03
TAG_GETSET_VALUE = 0x04
TAG_ERROR_MESSAGE = 0x05
TAG_GETSET_LOCKKEY = 0x15
MAX_FRAME_PAYLOAD = 1452
MAX_CONTROL_TEXT = 512


class HDHomeRunControlError(HDHomeRunError):
    """The tuner rejected an otherwise valid control request."""


class HDHomeRunControlProtocolError(HDHomeRunControlError):
    """A TCP/65001 reply is malformed or not the expected message type."""


@dataclass(frozen=True)
class TunerLease:
    """A lock acquired by this process for an otherwise idle tuner."""

    tuner_index: int
    lockkey: int


@dataclass(frozen=True)
class TunerStatus:
    tuner_index: int
    raw: str
    fields: Mapping[str, str]

    @property
    def lock(self) -> str:
        return str(self.fields.get("lock") or "").strip().lower()

    @property
    def is_locked(self) -> bool:
        return self.lock not in {"", "none"}

    @property
    def signal_strength(self) -> int:
        return _safe_int(self.fields.get("ss"))

    @property
    def symbol_quality(self) -> int:
        return _safe_int(self.fields.get("seq"))


@dataclass(frozen=True)
class ScannedService:
    program_number: int
    virtual_channel: str
    name: str
    flags: Sequence[str]
    raw: str


@dataclass(frozen=True)
class PresetMuxScan:
    target: ScanTarget
    tuner_index: int
    status: TunerStatus
    transport_stream_id: str
    original_network_id: str
    services: Sequence[ScannedService]
    streaminfo: str


SocketFactory = Callable[[tuple[str, int], float], socket.socket]
SleepFunction = Callable[[float], None]
RandomKeyFunction = Callable[[], int]


def _safe_int(value: object) -> int:
    try:
        return max(0, int(str(value or "").strip()))
    except (TypeError, ValueError):
        return 0


def _encode_var_length(length: int) -> bytes:
    if not 0 <= length <= 0x3FFF:
        raise HDHomeRunControlProtocolError("HDHomeRun control length is invalid")
    if length <= 0x7F:
        return bytes((length,))
    return bytes(((length & 0x7F) | 0x80, length >> 7))


def _decode_var_length(payload: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(payload):
        raise HDHomeRunControlProtocolError("HDHomeRun control TLV is truncated")
    length = payload[offset]
    offset += 1
    if length & 0x80:
        if offset >= len(payload):
            raise HDHomeRunControlProtocolError("HDHomeRun control TLV is truncated")
        length = (length & 0x7F) | (payload[offset] << 7)
        offset += 1
    return length, offset


def _encode_tlv(tag: int, value: bytes) -> bytes:
    if not 0 <= tag <= 0xFF:
        raise HDHomeRunControlProtocolError("HDHomeRun control tag is invalid")
    return bytes((tag,)) + _encode_var_length(len(value)) + value


def _decode_tlvs(payload: bytes) -> list[tuple[int, bytes]]:
    result: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(payload):
        tag = payload[offset]
        offset += 1
        length, offset = _decode_var_length(payload, offset)
        end = offset + length
        if end > len(payload):
            raise HDHomeRunControlProtocolError("HDHomeRun control TLV exceeds packet")
        result.append((tag, payload[offset:end]))
        offset = end
    return result


def _encode_frame(packet_type: int, payload: bytes) -> bytes:
    if not 0 <= packet_type <= 0xFFFF or len(payload) > MAX_FRAME_PAYLOAD:
        raise HDHomeRunControlProtocolError("HDHomeRun control frame is invalid")
    header = struct.pack(">HH", packet_type, len(payload))
    crc = binascii.crc32(header + payload) & 0xFFFFFFFF
    return header + payload + struct.pack("<I", crc)


def _decode_frame(data: bytes) -> tuple[int, bytes]:
    if len(data) < 8:
        raise HDHomeRunControlProtocolError("HDHomeRun control frame is too short")
    packet_type, length = struct.unpack(">HH", data[:4])
    expected_length = 4 + length + 4
    if length > MAX_FRAME_PAYLOAD or len(data) != expected_length:
        raise HDHomeRunControlProtocolError("HDHomeRun control frame has an invalid length")
    received_crc = struct.unpack("<I", data[-4:])[0]
    expected_crc = binascii.crc32(data[:-4]) & 0xFFFFFFFF
    if received_crc != expected_crc:
        raise HDHomeRunControlProtocolError("HDHomeRun control frame CRC mismatch")
    return packet_type, data[4:-4]


def _control_text(value: object, field: str) -> str:
    text = str(value or "")
    if not text or "\x00" in text or len(text) > MAX_CONTROL_TEXT:
        raise HDHomeRunValidationError(f"HDHomeRun {field} is invalid")
    return text


def _decode_control_text(value: bytes) -> str:
    if value.endswith(b"\x00"):
        value = value[:-1]
    if b"\x00" in value or len(value) > MAX_CONTROL_TEXT:
        raise HDHomeRunControlProtocolError("HDHomeRun control response text is invalid")
    return value.decode("utf-8", "replace")


def _parse_status_fields(value: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in str(value or "").split():
        key, marker, field_value = part.partition("=")
        if marker and key and len(key) <= 32 and len(field_value) <= 256:
            fields[key.lower()] = field_value
    return fields


def _looks_like_virtual_channel(value: str) -> bool:
    parts = value.split(".")
    return bool(parts) and all(part.isdigit() and int(part) <= 65535 for part in parts)


def _split_service_flags(value: str) -> tuple[str, list[str]]:
    text = str(value or "").strip()
    flags: list[str] = []
    while text.endswith(")"):
        start = text.rfind(" (")
        if start < 0:
            break
        flag = text[start + 2:-1].strip().lower()
        if not flag or len(flag) > 64:
            break
        flags.insert(0, flag)
        text = text[:start].rstrip()
    return text, flags


def parse_streaminfo(value: str) -> tuple[str, str, tuple[ScannedService, ...]]:
    """Parse documented ``/tunerN/streaminfo`` text conservatively."""

    tsid = ""
    onid = ""
    services: list[ScannedService] = []
    seen_programs: set[int] = set()
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if lowered.startswith("tsid="):
            tsid = line.partition("=")[2][:64]
            continue
        if lowered.startswith("onid="):
            onid = line.partition("=")[2][:64]
            continue
        program_text, marker, description = line.partition(":")
        if not marker:
            continue
        try:
            program_number = int(program_text.strip(), 10)
        except ValueError:
            continue
        if not 0 < program_number <= 0xFFFF or program_number in seen_programs:
            continue
        seen_programs.add(program_number)
        description = description.strip()
        candidate, separator, remainder = description.partition(" ")
        virtual_channel = ""
        name_and_flags = description
        if separator and _looks_like_virtual_channel(candidate):
            virtual_channel = candidate
            name_and_flags = remainder.strip()
        name, flags = _split_service_flags(name_and_flags)
        services.append(
            ScannedService(
                program_number=program_number,
                virtual_channel=virtual_channel,
                name=(name or f"Program {program_number}")[:256],
                flags=tuple(flags),
                raw=line[:512],
            )
        )
    return tsid, onid, tuple(services)


class HDHomeRunControlClient:
    """TCP/65001 client restricted to a configured private/local device."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 3.0,
        socket_factory: Optional[SocketFactory] = None,
        sleep_fn: SleepFunction = time.sleep,
        random_key: RandomKeyFunction = lambda: secrets.randbits(31) or 1,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.host = str(urlsplit(self.base_url).hostname or "")
        if not is_private_or_local_host(self.host):
            raise HDHomeRunValidationError("사설망 HDHomeRun 주소만 사용할 수 있습니다.")
        try:
            self.timeout = max(0.5, min(float(timeout), 15.0))
        except (TypeError, ValueError) as exc:
            raise HDHomeRunValidationError("HDHomeRun 제어 시간 제한이 올바르지 않습니다.") from exc
        self._socket_factory = socket_factory or socket.create_connection
        self._sleep = sleep_fn
        self._random_key = random_key

    def _connect(self):
        try:
            sock = self._socket_factory((self.host, CONTROL_PORT), self.timeout)
            sock.settimeout(self.timeout)
            return sock
        except OSError as exc:
            raise HDHomeRunTransportError(f"HDHomeRun 제어 연결 실패: {exc}") from exc

    @staticmethod
    def _recv_exact(sock, length: int) -> bytes:
        result: list[bytes] = []
        remaining = length
        while remaining:
            try:
                chunk = sock.recv(remaining)
            except OSError as exc:
                raise HDHomeRunTransportError(f"HDHomeRun 제어 응답 실패: {exc}") from exc
            if not chunk:
                raise HDHomeRunTransportError("HDHomeRun 제어 연결이 닫혔습니다.")
            result.append(chunk)
            remaining -= len(chunk)
        return b"".join(result)

    def _request(self, payload: bytes) -> list[tuple[int, bytes]]:
        sock = self._connect()
        try:
            sock.sendall(_encode_frame(TYPE_GETSET_REQ, payload))
            header = self._recv_exact(sock, 4)
            _packet_type, payload_length = struct.unpack(">HH", header)
            if payload_length > MAX_FRAME_PAYLOAD:
                raise HDHomeRunControlProtocolError("HDHomeRun 제어 응답이 너무 큽니다.")
            packet_type, response_payload = _decode_frame(
                header + self._recv_exact(sock, payload_length + 4)
            )
            if packet_type != TYPE_GETSET_RPY:
                raise HDHomeRunControlProtocolError("HDHomeRun 제어 응답 종류가 올바르지 않습니다.")
            return _decode_tlvs(response_payload)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _reply_value(tlvs: Iterable[tuple[int, bytes]]) -> str:
        error = ""
        for tag, raw_value in tlvs:
            if tag == TAG_GETSET_VALUE:
                return _decode_control_text(raw_value)
            if tag == TAG_ERROR_MESSAGE:
                error = _decode_control_text(raw_value)
        if error:
            raise HDHomeRunControlError(f"HDHomeRun 제어 거부: {error}")
        raise HDHomeRunControlProtocolError("HDHomeRun 제어 응답에 값이 없습니다.")

    def get(self, name: str) -> str:
        text = _control_text(name, "제어 이름")
        payload = _encode_tlv(TAG_GETSET_NAME, text.encode("ascii") + b"\x00")
        return self._reply_value(self._request(payload))

    def set(self, name: str, value: str, *, lockkey: int = 0) -> str:
        safe_name = _control_text(name, "제어 이름")
        safe_value = _control_text(value, "제어 값")
        payload = (
            _encode_tlv(TAG_GETSET_NAME, safe_name.encode("ascii") + b"\x00")
            + _encode_tlv(TAG_GETSET_VALUE, safe_value.encode("ascii") + b"\x00")
        )
        if lockkey:
            if not 0 < int(lockkey) <= 0xFFFFFFFF:
                raise HDHomeRunValidationError("HDHomeRun lock key가 올바르지 않습니다.")
            payload += _encode_tlv(TAG_GETSET_LOCKKEY, struct.pack(">I", int(lockkey)))
        return self._reply_value(self._request(payload))

    @staticmethod
    def _validate_tuner_index(tuner_index: int) -> int:
        try:
            tuner_index = int(tuner_index)
        except (TypeError, ValueError) as exc:
            raise HDHomeRunValidationError("HDHomeRun 튜너 번호가 올바르지 않습니다.") from exc
        if not 0 <= tuner_index <= 31:
            raise HDHomeRunValidationError("HDHomeRun 튜너 번호가 올바르지 않습니다.")
        return tuner_index

    @staticmethod
    def _validate_tuner_count(tuner_count: int) -> int:
        try:
            tuner_count = int(tuner_count)
        except (TypeError, ValueError) as exc:
            raise HDHomeRunValidationError("HDHomeRun 튜너 수가 올바르지 않습니다.") from exc
        if not 1 <= tuner_count <= 32:
            raise HDHomeRunValidationError("HDHomeRun 튜너 수가 올바르지 않습니다.")
        return tuner_count

    def tuner_status(self, tuner_index: int) -> TunerStatus:
        tuner_index = self._validate_tuner_index(tuner_index)
        raw = self.get(f"/tuner{tuner_index}/status")
        return TunerStatus(tuner_index=tuner_index, raw=raw, fields=_parse_status_fields(raw))

    def tuner_is_idle(self, tuner_index: int) -> bool:
        tuner_index = self._validate_tuner_index(tuner_index)
        lockkey = self.get(f"/tuner{tuner_index}/lockkey").strip().lower()
        target = self.get(f"/tuner{tuner_index}/target").strip().lower()
        return lockkey in {"", "none"} and target in {"", "none"}

    def acquire_idle_tuner(self, tuner_count: int) -> TunerLease:
        """Claim a free tuner without ever force-unlocking another client."""

        for tuner_index in range(self._validate_tuner_count(tuner_count)):
            if not self.tuner_is_idle(tuner_index):
                continue
            lockkey = int(self._random_key()) & 0xFFFFFFFF
            if not lockkey:
                lockkey = 1
            try:
                self.set(f"/tuner{tuner_index}/lockkey", str(lockkey))
            except HDHomeRunControlError:
                # Another consumer can claim it in the tiny interval after the
                # read-only idle check; move on rather than stealing it.
                continue
            return TunerLease(tuner_index=tuner_index, lockkey=lockkey)
        raise HDHomeRunControlError("스캔에 사용할 유휴 HDHomeRun 튜너가 없습니다.")

    def release_tuner(self, lease: TunerLease) -> None:
        tuner_index = self._validate_tuner_index(lease.tuner_index)
        try:
            self.set(f"/tuner{tuner_index}/channel", "none", lockkey=lease.lockkey)
        finally:
            self.set(f"/tuner{tuner_index}/lockkey", "none", lockkey=lease.lockkey)

    def scan_targets(
        self,
        targets: Sequence[ScanTarget],
        *,
        tuner_count: int,
        settle_seconds: float = 0.75,
        streaminfo_timeout: float = 5.0,
        poll_seconds: float = 0.25,
    ) -> tuple[PresetMuxScan, ...]:
        """Perform an explicit scan with one leased idle tuner.

        Each target's frequency comes exclusively from the selected preset.
        The tuner auto-detects VSB/QAM; its actual lock type is returned in
        ``status`` for the caller to compare with the expected modulation.
        """

        if not targets:
            return ()
        if settle_seconds < 0 or streaminfo_timeout < 0 or poll_seconds <= 0:
            raise HDHomeRunValidationError("스캔 대기 시간이 올바르지 않습니다.")
        lease = self.acquire_idle_tuner(tuner_count)
        results: list[PresetMuxScan] = []
        try:
            for target in targets:
                self.set(
                    f"/tuner{lease.tuner_index}/channel",
                    f"auto:{int(target.frequency_hz)}",
                    lockkey=lease.lockkey,
                )
                if settle_seconds:
                    self._sleep(settle_seconds)
                status = self.tuner_status(lease.tuner_index)
                streaminfo = ""
                if status.is_locked:
                    deadline = time.monotonic() + streaminfo_timeout
                    previous = None
                    quiet_until = time.monotonic()
                    while True:
                        streaminfo = self.get(f"/tuner{lease.tuner_index}/streaminfo")
                        now = time.monotonic()
                        if streaminfo != previous:
                            previous = streaminfo
                            quiet_until = now + poll_seconds
                        if now >= quiet_until or now >= deadline:
                            break
                        self._sleep(min(poll_seconds, max(0.0, deadline - now)))
                tsid, onid, services = parse_streaminfo(streaminfo)
                results.append(
                    PresetMuxScan(
                        target=target,
                        tuner_index=lease.tuner_index,
                        status=status,
                        transport_stream_id=tsid,
                        original_network_id=onid,
                        services=services,
                        streaminfo=streaminfo,
                    )
                )
        finally:
            self.release_tuner(lease)
        return tuple(results)
