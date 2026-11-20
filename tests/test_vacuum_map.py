# ruff: noqa: ANN201, D100, D102, INP001, PT009

import base64
import io
import unittest
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from PIL import Image

from custom_components.eufy_sdk.api import EufySdkApiClientError
from custom_components.eufy_sdk.image import EufySdkVacuumMapImage, async_setup_entry
from custom_components.eufy_sdk.vacuum_map import (
    CELL_CARPET,
    CELL_FREE,
    CELL_OBSTACLE,
    CELL_UNKNOWN,
    TRANSPARENT,
    WALL,
    render_map_png,
    room_colour,
    room_names,
)

ROBOT = "T2292B0000000001"
SCALE = 2

# A 4x2 plane at 5 cm per cell, origin (0, 0). Row 0 is the LOWER row in the world.
PLANE = [
    [CELL_OBSTACLE, CELL_FREE, CELL_FREE, CELL_UNKNOWN],
    [CELL_FREE, CELL_CARPET, CELL_FREE, CELL_FREE],
]
# Room ids per cell (0 = none); the outline stores `id << 2`.
ROOMS = [
    [0, 3, 3, 0],
    [2, 2, 2, 0],
]


def _pack_plane(rows: list[list[int]]) -> str:
    flat = [v for row in rows for v in row]
    out = bytearray((len(flat) + 3) // 4)
    for i, v in enumerate(flat):
        out[i >> 2] |= v << ((i & 3) * 2)
    return base64.b64encode(bytes(out)).decode()


def _pack_outline(rows: list[list[int]]) -> str:
    return base64.b64encode(bytes(v << 2 for row in rows for v in row)).decode()


def _snapshot(**overrides: Any) -> dict:
    snap = {
        "mapId": 7,
        "plane": {
            "geometry": {
                "width": 4,
                "height": 2,
                "resolution": 5,
                "origin": {"x": 0, "y": 0},
                "docks": [{"kind": "charger", "pose": {"x": 0, "y": 0}}],
            },
            "cells": _pack_plane(PLANE),
        },
        "outline": {
            "width": 4,
            "height": 2,
            "resolution": 5,
            "origin": {"x": 0, "y": 0},
            "cells": _pack_outline(ROOMS),
        },
        "rooms": {
            "rooms": [
                {"id": 2, "name": "Kitchen"},
                {"id": 3, "name": None},
                {"id": 3, "name": "Hall"},
            ]
        },
        "pose": None,
    }
    snap.update(overrides)
    return snap


def _pixel(png: bytes, col: int, world_row: int, height: int = 2) -> tuple:
    """Colour at the centre of a map cell, undoing the vertical flip."""
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    x = col * SCALE + SCALE // 2
    y = (height - 1 - world_row) * SCALE + SCALE // 2
    return image.getpixel((x, y))


class RenderTest(unittest.TestCase):
    """The drawing places walls, rooms, carpet and unknown cells correctly."""

    def setUp(self):
        # Labels and markers draw over cells; turn them off to read raw cell colours.
        self.snap = _snapshot(rooms={"rooms": []})
        self.snap["plane"]["geometry"]["docks"] = []

    def test_size_and_flip(self):
        png = render_map_png(self.snap, scale=SCALE)
        image = Image.open(io.BytesIO(png))
        self.assertEqual(image.size, (4 * SCALE, 2 * SCALE))
        # World row 0 is drawn at the BOTTOM of the image.
        self.assertEqual(_pixel(png, 0, 0), WALL)
        self.assertEqual(_pixel(png, 3, 0), TRANSPARENT)

    def test_rooms_are_coloured_and_carpet_is_darker(self):
        snap = _snapshot()
        snap["plane"]["geometry"]["docks"] = []
        png = render_map_png(snap, scale=SCALE, labels=False)
        kitchen = _pixel(png, 0, 1)  # room 2
        carpet = _pixel(png, 1, 1)  # room 2, carpet
        self.assertEqual(kitchen, room_colour(2))
        self.assertTrue(
            all(c < k for c, k in zip(carpet[:3], kitchen[:3], strict=True))
        )

    def test_room_names_merge_by_id(self):
        self.assertEqual(room_names(_snapshot()), {2: "Kitchen", 3: "Hall"})

    def test_unusable_snapshots_give_none(self):
        self.assertIsNone(render_map_png(None))
        self.assertIsNone(render_map_png({"plane": {}}))
        bad = _snapshot()
        bad["plane"]["geometry"]["resolution"] = None
        self.assertIsNone(render_map_png(bad))
        short = _snapshot()
        short["plane"]["cells"] = base64.b64encode(b"\x00").decode()
        self.assertIsNone(render_map_png(short))

    def test_draws_without_an_outline(self):
        snap = _snapshot(outline=None)
        self.assertIsNotNone(render_map_png(snap, scale=SCALE))

    def test_markers_and_labels_draw(self):
        snap = _snapshot(pose={"x": 10, "y": 5, "theta": 157})
        png = render_map_png(snap, scale=8)
        self.assertTrue(png.startswith(b"\x89PNG"))


def _coordinator(*, vacuum: bool = True) -> Mock:
    coord = Mock()
    caps = ["vacuum_clean"] if vacuum else ["camera"]
    coord.data = {ROBOT: {"name": "Robovac", "capabilities": caps}}
    coord.config_entry.runtime_data.client.vacuum_map = AsyncMock(
        return_value=_snapshot()
    )
    return coord


def _hass() -> MagicMock:
    hass = MagicMock()

    async def run(func: Callable[..., Any], *args: Any) -> Any:
        return func(*args)

    hass.async_add_executor_job = run
    return hass


class MapImageEntityTest(unittest.IsolatedAsyncioTestCase):
    """The entity fetches the full map, draws it, and only re-stamps on change."""

    async def test_setup_adds_a_map_for_vacuums_only(self):
        for vacuum, expected in ((True, 1), (False, 0)):
            entry = Mock()
            entry.data = {"host": "h", "port": 3000}
            entry.runtime_data.coordinator = _coordinator(vacuum=vacuum)
            added: list = []
            await async_setup_entry(_hass(), entry, added.extend)
            maps = [e for e in added if isinstance(e, EufySdkVacuumMapImage)]
            self.assertEqual(len(maps), expected)

    async def test_refresh_draws_and_skips_unchanged(self):
        coord = _coordinator()
        hass = _hass()
        entity = EufySdkVacuumMapImage(hass, coord, ROBOT)
        entity.hass = hass
        with patch.object(entity, "async_write_ha_state") as write:
            await entity.async_refresh_map()
            first = entity.image_last_updated
            self.assertIsNotNone(await entity.async_image())
            self.assertEqual(write.call_count, 1)
            await entity.async_refresh_map()
            self.assertEqual(write.call_count, 1)
            self.assertEqual(entity.image_last_updated, first)
        client = coord.config_entry.runtime_data.client
        client.vacuum_map.assert_awaited_with(ROBOT, full=True)

    async def test_no_map_or_bridge_error_keeps_no_image(self):
        coord = _coordinator()
        client = coord.config_entry.runtime_data.client
        hass = _hass()
        entity = EufySdkVacuumMapImage(hass, coord, ROBOT)
        entity.hass = hass
        with patch.object(entity, "async_write_ha_state") as write:
            client.vacuum_map.return_value = None
            await entity.async_refresh_map()
            client.vacuum_map.side_effect = EufySdkApiClientError("unknown cmd")
            await entity.async_refresh_map()
            write.assert_not_called()
        self.assertIsNone(await entity.async_image())


if __name__ == "__main__":
    unittest.main()
