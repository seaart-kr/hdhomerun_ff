"""Frequency-preset helpers for the HDHomeRun FF plugin.

The plugin deliberately keeps broadcast plans in ordinary JSON instead of
depending on a TVHeadend installation.  A small importer is included for the
``[CHANNEL]`` files that TVHeadend uses for initial tuning data.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1
PRESET_DIRECTORY = Path(__file__).resolve().parent / "presets"
_PRESET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


class PresetValidationError(ValueError):
    """Raised when a preset cannot be safely used for a tuner scan."""


def _required_text(value: Any, field: str, *, limit: int = 128) -> str:
    text = str(value or "").strip()
    if not text:
        raise PresetValidationError(f"{field} is required")
    if len(text) > limit:
        raise PresetValidationError(f"{field} is too long")
    return text


def _normalise_delivery_system(value: Any) -> str:
    value = _required_text(value, "delivery_system", limit=32).upper()
    # TVHeadend initial tuning files name ATSC terrestrial/cable sources
    # simply "ATSC".  The plugin uses the more explicit spelling internally.
    if value == "ATSC":
        return "ATSC-T"
    return value


def _normalise_mux(value: Mapping[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PresetValidationError(f"muxes[{index}] must be an object")
    try:
        frequency = int(value.get("frequency_hz"))
    except (TypeError, ValueError):
        raise PresetValidationError(f"muxes[{index}].frequency_hz must be an integer")

    # RF tuners use Hz.  This broad guard rejects malformed values without
    # baking one country's spectrum into a generic import path.
    if not 1_000_000 <= frequency <= 3_000_000_000:
        raise PresetValidationError(f"muxes[{index}].frequency_hz is outside tuner range")

    modulation = _required_text(value.get("modulation"), f"muxes[{index}].modulation", limit=32).upper()
    result: dict[str, Any] = {
        "frequency_hz": frequency,
        "modulation": modulation,
    }
    inversion = str(value.get("inversion") or "").strip().upper()
    if inversion:
        if len(inversion) > 32:
            raise PresetValidationError(f"muxes[{index}].inversion is too long")
        result["inversion"] = inversion
    return result


def validate_preset(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalise a preset into the public v1 schema.

    The returned object is safe to persist as a user's scan plan.  Ordering of
    muxes is preserved because it provides a predictable, resumable scan order.
    """

    if not isinstance(raw, Mapping):
        raise PresetValidationError("preset must be an object")

    try:
        version = int(raw.get("version"))
    except (TypeError, ValueError):
        raise PresetValidationError("version must be an integer")
    if version != SCHEMA_VERSION:
        raise PresetValidationError(f"unsupported preset version: {version}")

    preset_id = _required_text(raw.get("id"), "id", limit=64).lower()
    if not _PRESET_ID_RE.fullmatch(preset_id):
        raise PresetValidationError("id must use lowercase letters, numbers, dots, underscores, or hyphens")

    muxes_raw = raw.get("muxes")
    if not isinstance(muxes_raw, list) or not muxes_raw:
        raise PresetValidationError("muxes must be a non-empty array")

    muxes = [_normalise_mux(mux, index) for index, mux in enumerate(muxes_raw)]
    keys = [(mux["frequency_hz"], mux["modulation"]) for mux in muxes]
    if len(keys) != len(set(keys)):
        raise PresetValidationError("muxes must not contain duplicate frequency/modulation pairs")

    country = _required_text(raw.get("country"), "country", limit=8).upper()
    if not re.fullmatch(r"[A-Z]{2,3}", country):
        raise PresetValidationError("country must be an ISO-like uppercase code")

    return {
        "version": version,
        "id": preset_id,
        "name": _required_text(raw.get("name"), "name", limit=160),
        "country": country,
        "delivery_system": _normalise_delivery_system(raw.get("delivery_system")),
        "source": _required_text(raw.get("source"), "source", limit=64).lower(),
        "muxes": muxes,
    }


def load_preset(path: str | Path) -> dict[str, Any]:
    """Load and validate one JSON preset from disk."""

    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return validate_preset(json.load(handle))
    except json.JSONDecodeError as exc:
        raise PresetValidationError(f"invalid preset JSON: {exc.msg}") from exc
    except OSError as exc:
        raise PresetValidationError(f"cannot read preset: {exc}") from exc


def list_bundled_presets() -> list[dict[str, Any]]:
    """Return metadata for valid preset files shipped with the plugin."""

    presets: list[dict[str, Any]] = []
    if not PRESET_DIRECTORY.is_dir():
        return presets
    for path in sorted(PRESET_DIRECTORY.glob("*.json")):
        preset = load_preset(path)
        presets.append({
            "id": preset["id"],
            "name": preset["name"],
            "country": preset["country"],
            "delivery_system": preset["delivery_system"],
            "source": preset["source"],
            "mux_count": len(preset["muxes"]),
        })
    return presets


def load_bundled_preset(preset_id: str) -> dict[str, Any]:
    """Load a bundled preset by its stable public identifier."""

    wanted = str(preset_id or "").strip().lower()
    for path in PRESET_DIRECTORY.glob("*.json"):
        preset = load_preset(path)
        if preset["id"] == wanted:
            return preset
    raise PresetValidationError(f"bundled preset not found: {wanted or '<empty>'}")


def parse_tvh_initial_tuning(
    text: str,
    *,
    preset_id: str = "imported-tvh-preset",
    name: str = "Imported TVHeadend preset",
    country: str = "KR",
    source: str = "custom",
) -> dict[str, Any]:
    """Import TVHeadend's INI-like initial tuning format.

    Only ``[CHANNEL]`` sections are accepted.  Unknown fields are ignored, but
    the delivery system, frequency, modulation, and inversion are retained.
    """

    if not isinstance(text, str) or not text.strip():
        raise PresetValidationError("initial tuning file is empty")

    sections: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            if current is not None:
                sections.append(current)
            current = {} if line[1:-1].strip().upper() == "CHANNEL" else None
            continue
        if current is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        current[key.strip().upper()] = value.strip()
    if current is not None:
        sections.append(current)

    if not sections:
        raise PresetValidationError("initial tuning file has no [CHANNEL] sections")

    muxes: list[dict[str, Any]] = []
    delivery_values: set[str] = set()
    for index, section in enumerate(sections):
        frequency = section.get("FREQUENCY")
        modulation = section.get("MODULATION")
        delivery = section.get("DELIVERY_SYSTEM")
        if not frequency or not modulation or not delivery:
            raise PresetValidationError(
                f"[CHANNEL] section {index + 1} requires DELIVERY_SYSTEM, FREQUENCY, and MODULATION"
            )
        delivery_values.add(_normalise_delivery_system(delivery))
        mux: dict[str, Any] = {
            "frequency_hz": frequency,
            "modulation": modulation,
        }
        if section.get("INVERSION"):
            mux["inversion"] = section["INVERSION"]
        muxes.append(mux)

    if len(delivery_values) != 1:
        raise PresetValidationError("initial tuning file mixes delivery systems")

    return validate_preset({
        "version": SCHEMA_VERSION,
        "id": preset_id,
        "name": name,
        "country": country,
        "delivery_system": delivery_values.pop(),
        "source": source,
        "muxes": muxes,
    })


def dump_preset(preset: Mapping[str, Any]) -> str:
    """Return canonical, user-exportable JSON for a validated preset."""

    return json.dumps(validate_preset(preset), ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def korean_full_scan_preset() -> dict[str, Any]:
    """Korean RF grid, testing VSB and QAM explicitly at every frequency."""
    ranges = ((2, 4, 57_000_000), (5, 6, 79_000_000),
              (7, 13, 177_000_000), (14, 22, 123_000_000), (23, 153, 219_000_000))
    frequencies = sorted({start + (ch-first)*6_000_000
                          for first, last, start in ranges for ch in range(first, last+1)})
    return validate_preset({
        'version': 1, 'id': 'kr-full-auto', 'name': '한국 전체 자동 탐색',
        'country': 'KR', 'delivery_system': 'ATSC-T', 'source': 'cable',
        'muxes': [{'frequency_hz': f, 'modulation': m}
                  for f in frequencies for m in ('VSB/8', 'QAM/AUTO')],
    })

