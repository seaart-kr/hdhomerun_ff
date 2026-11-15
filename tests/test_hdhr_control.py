import unittest

from hdhomerun import ScanTarget
from hdhr_control import (
    HDHomeRunControlClient,
    HDHomeRunControlError,
    HDHomeRunControlProtocolError,
    TAG_ERROR_MESSAGE,
    TAG_GETSET_NAME,
    TAG_GETSET_VALUE,
    TYPE_GETSET_REQ,
    TYPE_GETSET_RPY,
    _decode_frame,
    _decode_tlvs,
    _encode_frame,
    _encode_tlv,
    parse_streaminfo,
)


class FakeSocket:
    def __init__(self, response, chunks=None):
        self.response = response
        self.chunks = list(chunks or [])
        self.sent = b""
        self.timeout = None
        self.closed = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, value):
        self.sent += value

    def recv(self, length):
        if self.chunks:
            part = self.chunks.pop(0)
            if len(part) > length:
                self.chunks.insert(0, part[length:])
                return part[:length]
            return part
        if not self.response:
            return b""
        part, self.response = self.response[:length], self.response[length:]
        return part

    def close(self):
        self.closed = True


class ControlCodecTest(unittest.TestCase):
    def test_get_request_and_fragmented_reply(self):
        reply = _encode_frame(
            TYPE_GETSET_RPY,
            _encode_tlv(TAG_GETSET_VALUE, b"none\0"),
        )
        fake = FakeSocket(reply, chunks=[reply[:2], reply[2:7], reply[7:]])
        client = HDHomeRunControlClient(
            "192.168.2.17",
            socket_factory=lambda address, timeout: fake,
        )

        self.assertEqual(client.get("/tuner0/lockkey"), "none")
        packet_type, payload = _decode_frame(fake.sent)
        self.assertEqual(packet_type, TYPE_GETSET_REQ)
        tlvs = _decode_tlvs(payload)
        self.assertEqual(tlvs[0], (TAG_GETSET_NAME, b"/tuner0/lockkey\0"))
        self.assertTrue(fake.closed)

    def test_device_error_becomes_control_error(self):
        reply = _encode_frame(
            TYPE_GETSET_RPY,
            _encode_tlv(TAG_ERROR_MESSAGE, b"tuner locked\0"),
        )
        client = HDHomeRunControlClient(
            "192.168.2.17",
            socket_factory=lambda address, timeout: FakeSocket(reply),
        )
        with self.assertRaises(HDHomeRunControlError):
            client.get("/tuner0/lockkey")

    def test_bad_crc_is_rejected(self):
        frame = bytearray(_encode_frame(TYPE_GETSET_RPY, b""))
        frame[-1] ^= 0x01
        with self.assertRaises(HDHomeRunControlProtocolError):
            _decode_frame(bytes(frame))


class StreamInfoTest(unittest.TestCase):
    def test_missing_names_are_not_status_flags_or_zero(self):
        _, _, services = parse_streaminfo('111: 0 (encrypted)\n41: 0\n9: (control)\n21: 0 (no data)\n')
        self.assertEqual([s.name for s in services], ['Program 111', 'Program 41', 'Program 9', 'Program 21'])
        self.assertEqual([s.flags for s in services], [('encrypted',), (), ('control',), ('no data',)])
        self.assertTrue(all(not s.virtual_channel for s in services))

    def test_parses_transport_and_programs(self):
        tsid, onid, services = parse_streaminfo(
            "tsid=0x3a\n"
            "onid=0x10\n"
            "3: 5.1 KBS 1 (encrypted)\n"
            "4: 5.2 KBS 1 UHD\n"
            "7: data (control)\n"
        )
        self.assertEqual(tsid, "0x3a")
        self.assertEqual(onid, "0x10")
        self.assertEqual([item.program_number for item in services], [3, 4, 7])
        self.assertEqual(services[0].virtual_channel, "5.1")
        self.assertEqual(services[0].name, "KBS 1")
        self.assertEqual(services[0].flags, ("encrypted",))
        self.assertEqual(services[2].flags, ("control",))


class ScriptedScanner(HDHomeRunControlClient):
    def __init__(self):
        self.commands = []
        self.values = {
            "/tuner0/lockkey": "none",
            "/tuner0/target": "none",
            "/tuner0/status": "ch=auto:267000000 lock=8vsb ss=90 snq=100 seq=100",
            "/tuner0/streaminfo": "tsid=0x44\n10: 7.1 Test Channel\n",
        }
        self._random_key = lambda: 0x12345678
        self._sleep = lambda seconds: None

    def get(self, name):
        self.commands.append(("get", name, "", 0))
        return self.values[name]

    def set(self, name, value, *, lockkey=0):
        self.commands.append(("set", name, value, lockkey))
        if name.endswith("/lockkey") and value != "none":
            self.values[name] = value
        if name.endswith("/lockkey") and value == "none":
            self.values[name] = "none"
        return value


class PresetScanTest(unittest.TestCase):
    def test_scans_only_given_frequency_and_releases_lease(self):
        client = ScriptedScanner()
        target = ScanTarget(
            index=0,
            frequency_hz=267000000,
            modulation="VSB/8",
            delivery_system="ATSC-T",
        )
        results = client.scan_targets(
            [target],
            tuner_count=1,
            settle_seconds=0,
            lock_timeout=0,
            streaminfo_timeout=0,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].target.frequency_hz, 267000000)
        self.assertEqual(results[0].services[0].program_number, 10)
        self.assertTrue(results[0].status.is_locked)
        self.assertIn(
            ("set", "/tuner0/channel", "8vsb:267000000", 0x12345678),
            client.commands,
        )
        self.assertEqual(
            client.commands[-2:],
            [
                ("set", "/tuner0/channel", "none", 0x12345678),
                ("set", "/tuner0/lockkey", "none", 0x12345678),
            ],
        )

    def test_refuses_when_no_idle_tuner(self):
        client = ScriptedScanner()
        client.values["/tuner0/lockkey"] = "49201"
        with self.assertRaises(HDHomeRunControlError):
            client.acquire_idle_tuner(1)

    def test_qam_target_uses_explicit_modulation(self):
        client = ScriptedScanner()
        client.scan_targets([ScanTarget(0, 627000000, 'QAM/256', 'ATSC-T')],
                            tuner_count=1, settle_seconds=0, lock_timeout=0, streaminfo_timeout=0)
        self.assertIn(('set', '/tuner0/channel', 'qam256:627000000', 0x12345678), client.commands)


if __name__ == "__main__":
    unittest.main()
