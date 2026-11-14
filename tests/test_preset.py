from __future__ import annotations

import json
import unittest
from pathlib import Path

from preset import (
    PRESET_DIRECTORY,
    PresetValidationError,
    dump_preset,
    list_bundled_presets,
    load_bundled_preset,
    parse_tvh_initial_tuning,
    validate_preset,
)


class PresetTest(unittest.TestCase):
    def test_bundled_cmb_preset_preserves_user_supplied_muxes(self):
        preset = load_bundled_preset("kr-cable-cmb-v1")

        self.assertEqual(preset["version"], 1)
        self.assertEqual(preset["country"], "KR")
        self.assertEqual(preset["delivery_system"], "ATSC-T")
        self.assertEqual(len(preset["muxes"]), 73)
        self.assertEqual(preset["muxes"][0], {
            "frequency_hz": 267000000,
            "modulation": "VSB/8",
            "inversion": "AUTO",
        })
        self.assertEqual(preset["muxes"][-1], {
            "frequency_hz": 741000000,
            "modulation": "QAM/256",
            "inversion": "AUTO",
        })
        self.assertEqual(
            sum(mux["modulation"] == "VSB/8" for mux in preset["muxes"]),
            53,
        )
        self.assertEqual(
            sum(mux["modulation"] == "QAM/256" for mux in preset["muxes"]),
            20,
        )

    def test_lists_bundled_preset_without_exposing_full_scan_plan(self):
        self.assertEqual(list_bundled_presets(), [{
            "id": "kr-cable-cmb-v1",
            "name": "Korea Digital Cable CMB",
            "country": "KR",
            "delivery_system": "ATSC-T",
            "source": "cable",
            "mux_count": 73,
        }])

    def test_imports_tvh_channel_sections_and_maps_atsc_name(self):
        text = """
        # a comment
        [CHANNEL]
        DELIVERY_SYSTEM = ATSC
        FREQUENCY = 267000000
        MODULATION = VSB/8
        INVERSION = AUTO

        [CHANNEL]
        DELIVERY_SYSTEM = ATSC
        FREQUENCY = 627000000
        MODULATION = QAM/256
        """

        preset = parse_tvh_initial_tuning(
            text,
            preset_id="kr-test-v1",
            name="Korea test",
            country="kr",
            source="Custom",
        )

        self.assertEqual(preset["delivery_system"], "ATSC-T")
        self.assertEqual(preset["country"], "KR")
        self.assertEqual(preset["source"], "custom")
        self.assertEqual(preset["muxes"], [
            {"frequency_hz": 267000000, "modulation": "VSB/8", "inversion": "AUTO"},
            {"frequency_hz": 627000000, "modulation": "QAM/256"},
        ])

    def test_rejects_duplicate_mux_and_missing_scan_fields(self):
        with self.assertRaisesRegex(PresetValidationError, "duplicate"):
            validate_preset({
                "version": 1,
                "id": "duplicate-v1",
                "name": "Duplicate",
                "country": "KR",
                "delivery_system": "ATSC-T",
                "source": "custom",
                "muxes": [
                    {"frequency_hz": 267000000, "modulation": "VSB/8"},
                    {"frequency_hz": 267000000, "modulation": "VSB/8"},
                ],
            })

        with self.assertRaisesRegex(PresetValidationError, "requires"):
            parse_tvh_initial_tuning("[CHANNEL]\nFREQUENCY=267000000\n")

    def test_export_is_versioned_json_that_round_trips(self):
        preset = load_bundled_preset("kr-cable-cmb-v1")
        exported = dump_preset(preset)
        self.assertEqual(validate_preset(json.loads(exported)), preset)


if __name__ == "__main__":
    unittest.main()
