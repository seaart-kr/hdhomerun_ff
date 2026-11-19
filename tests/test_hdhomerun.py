import unittest

from hdhomerun import (
    HDHomeRunClient,
    HDHomeRunValidationError,
    is_private_or_local_host,
    normalize_base_url,
    stable_channel_id,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def __call__(self, url, timeout):
        self.urls.append((url, timeout))
        return self.responses[url]


class BaseUrlTest(unittest.TestCase):
    def test_normalizes_lan_origin_without_path(self):
        self.assertEqual(normalize_base_url("192.168.1.10"), "http://192.168.1.10")
        self.assertEqual(normalize_base_url("HTTPS://HDHOMERUN.local/"), "https://hdhomerun.local")
        self.assertTrue(is_private_or_local_host("100.92.1.1"))

    def test_rejects_public_origin_credentials_and_paths(self):
        for value in [
            "https://example.com",
            "http://user:pass@192.168.1.10",
            "http://192.168.1.10/discover.json",
            "ftp://192.168.1.10",
        ]:
            with self.subTest(value=value):
                with self.assertRaises(HDHomeRunValidationError):
                    normalize_base_url(value)


class ClientTest(unittest.TestCase):
    def make_client(self):
        base = "http://192.168.1.20"
        transport = FakeTransport(
            {
                f"{base}/discover.json": {
                    "DeviceID": "1234ABCD",
                    "FriendlyName": "Living Room HDHomeRun",
                    "ModelNumber": "HDHR4",
                    "FirmwareVersion": "20240101",
                    "TunerCount": 4,
                },
                f"{base}/lineup.json": [
                    {
                        "GuideNumber": "11.1",
                        "GuideName": "KBS 1",
                        "URL": f"{base}:5004/auto/v11.1",
                        "VideoCodec": "MPEG2",
                        "AudioCodec": "AC3",
                    },
                    {
                        "GuideNumber": "7",
                        "GuideName": "MBC",
                        "URL": f"{base}:5004/auto/v7",
                        "VideoCodec": "H264",
                        "AudioCodec": "AC3",
                    },
                    {
                        "GuideNumber": "bad",
                        "GuideName": "Outside URL",
                        "URL": "http://example.com/auto/v1",
                    },
                ],
            }
        )
        return HDHomeRunClient(base, transport=transport), transport

    def test_fetch_snapshot_returns_only_device_local_channels(self):
        client, transport = self.make_client()
        snapshot = client.fetch_snapshot()

        self.assertEqual(snapshot.device.device_id, "1234abcd")
        self.assertEqual(snapshot.device.tuner_count, 4)
        self.assertEqual([item.guide_number for item in snapshot.lineup.channels], ["7", "11.1"])
        self.assertEqual(snapshot.lineup.skipped_entries, 1)
        self.assertTrue(snapshot.lineup.channels[0].channel_id.startswith("hdhr:1234abcd:7:"))
        self.assertEqual(len(transport.urls), 2)

    def test_stable_id_does_not_depend_on_display_name(self):
        url = "http://192.168.1.20:5004/auto/v7"
        self.assertEqual(
            stable_channel_id("1234ABCD", "7", url),
            stable_channel_id("1234ABCD", "7", url),
        )

    def test_unnamed_native_channels_are_kept_for_preview_and_drm_is_explicit(self):
        from task import snapshot_channel_rows
        client, transport = self.make_client()
        transport.responses['http://192.168.1.20/lineup.json'] = [
            {'GuideNumber':'1','URL':'http://192.168.1.20:5004/auto/v1'},
            {'GuideNumber':'2','GuideName':'0','URL':'http://192.168.1.20:5004/auto/v2','DRM':'0'},
            {'GuideNumber':'3','GuideName':'Encrypted','URL':'http://192.168.1.20:5004/auto/v3','DRM':1},
        ]
        snapshot = client.fetch_snapshot()
        self.assertEqual(len(snapshot.lineup.channels), 3)
        self.assertEqual(snapshot.lineup.channels[0].guide_name, '채널 1')
        self.assertTrue(snapshot.lineup.channels[0].name_missing)
        rows = snapshot_channel_rows(snapshot)
        self.assertEqual(rows[1]['raw_data']['flags'], [])
        self.assertEqual(rows[2]['raw_data']['flags'], ['encrypted'])
        self.assertFalse(rows[2]['enabled'])


class ScanPlanTest(unittest.TestCase):
    def test_plan_is_pure_and_deduplicates_muxes(self):
        plan = HDHomeRunClient.build_initial_scan_plan(
            {
                "id": "kr-cable-atsc-v1",
                "delivery_system": "ATSC-T",
                "muxes": [
                    {"frequency_hz": 267000000, "modulation": "VSB/8"},
                    {"frequency_hz": 267000000, "modulation": "VSB/8"},
                    {"frequency_hz": 627000000, "modulation": "QAM/256"},
                ],
            }
        )
        self.assertEqual(plan.preset_id, "kr-cable-atsc-v1")
        self.assertEqual(len(plan.targets), 2)
        self.assertTrue(plan.requires_idle_tuner)
        self.assertFalse(plan.changes_device_channelmap)

    def test_plan_rejects_unknown_modulation(self):
        with self.assertRaises(HDHomeRunValidationError):
            HDHomeRunClient.build_initial_scan_plan(
                {
                    "id": "bad",
                    "delivery_system": "ATSC-T",
                    "muxes": [{"frequency_hz": 267000000, "modulation": "QAM/16"}],
                }
            )


if __name__ == "__main__":
    unittest.main()
