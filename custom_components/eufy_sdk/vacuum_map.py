"""
Render a robot vacuum's decoded map (the bridge's `device.map`, `full: true`) as PNG.

Input is the SDK's `VacuumMapSnapshot`, serialized by the bridge with Buffers as base64:

* `plane.cells`: the floor plane, four cells per byte, two bits each, low bits first,
  `i = row * width + col`; values index unknown / obstacle / free / carpet.
* `outline.cells`: one byte per cell, the room id in the upper six bits (`byte >> 2`).
  The outline carries its own origin and resolution, so a floor cell finds its room
  through world coordinates (centimetres), never by reusing the index.
* `rooms.rooms[]`: `{id, name}`; `plane.geometry.docks[].pose` and `pose`: world points.

Both axes count up in the world, so the image is flipped vertically to put a larger
`y` higher on screen, as the vendor's app draws it. Everything here is pure: no Home
Assistant imports, so it renders in an executor and tests without a running hass.
"""

from __future__ import annotations

import base64
import io
import math
from typing import Any

from PIL import Image, ImageDraw, ImageFont

CELL_UNKNOWN, CELL_OBSTACLE, CELL_FREE, CELL_CARPET = range(4)

TRANSPARENT = (0, 0, 0, 0)
WALL = (70, 72, 78, 255)
FLOOR = (205, 208, 214, 255)
DOCK = (46, 160, 67, 255)
ROBOT = (33, 99, 214, 255)
LABEL = (20, 22, 26, 255)
LABEL_HALO = (255, 255, 255, 230)

# Soft, distinct room fills; a room id picks one deterministically.
ROOM_COLOURS: tuple[tuple[int, int, int], ...] = (
    (141, 182, 236),
    (247, 186, 132),
    (146, 214, 160),
    (232, 154, 172),
    (196, 170, 232),
    (240, 214, 120),
    (128, 208, 208),
    (226, 176, 220),
)

DEFAULT_SCALE = 3

# Clockwise display rotations, in degrees; Pillow's transposes turn anticlockwise.
ROTATIONS: tuple[int, ...] = (0, 90, 180, 270)
_TRANSPOSE = {
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


def _b64(value: Any) -> bytes | None:
    """Decode a base64 Buffer string from the bridge; None when absent or bad."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return base64.b64decode(value)
    except ValueError:
        return None


def room_colour(room_id: int) -> tuple[int, int, int, int]:
    """Return the fill colour for a room id."""
    r, g, b = ROOM_COLOURS[(room_id - 1) % len(ROOM_COLOURS)]
    return (r, g, b, 255)


def _shade(colour: tuple[int, int, int, int], factor: float) -> tuple[int, ...]:
    return (*(int(c * factor) for c in colour[:3]), colour[3])


def room_names(snapshot: dict[str, Any]) -> dict[int, str]:
    """Return `{room id: name}`, merging repeated entries and keeping the first name."""
    names: dict[int, str | None] = {}
    for room in (snapshot.get("rooms") or {}).get("rooms") or []:
        rid = room.get("id")
        if isinstance(rid, int) and names.get(rid) is None:
            names[rid] = room.get("name") or None
    return {rid: name or f"Room {rid}" for rid, name in names.items()}


def render_map_png(  # noqa: PLR0912, PLR0915 — one linear drawing pass
    snapshot: dict[str, Any] | None,
    scale: int = DEFAULT_SCALE,
    *,
    labels: bool = True,
    rotation: int = 0,
) -> bytes | None:
    """
    Draw the map as PNG bytes; None when the snapshot has no usable floor plane.

    `rotation` turns the drawing clockwise by 0/90/180/270 degrees so it can match how
    the vendor app shows the home. Markers follow the rotation; labels stay upright.
    """
    rotation = rotation % 360 if rotation % 90 == 0 else 0
    plane = (snapshot or {}).get("plane") or {}
    geometry = plane.get("geometry") or {}
    width, height = geometry.get("width"), geometry.get("height")
    resolution = geometry.get("resolution")
    origin = geometry.get("origin") or {}
    cells = _b64(plane.get("cells"))
    if not (
        isinstance(width, int)
        and isinstance(height, int)
        and width > 0
        and height > 0
        and resolution
        and cells
        and len(cells) * 4 >= width * height
    ):
        return None
    ox, oy = origin.get("x", 0), origin.get("y", 0)

    outline = snapshot.get("outline") or {}
    room_cells = _b64(outline.get("cells"))
    o_w, o_h = outline.get("width"), outline.get("height")
    o_res = outline.get("resolution") or resolution
    o_origin = outline.get("origin") or origin
    has_rooms = bool(
        room_cells
        and isinstance(o_w, int)
        and isinstance(o_h, int)
        and len(room_cells) >= o_w * o_h
    )

    def room_at(col: int, row: int) -> int:
        if not has_rooms:
            return 0
        wx, wy = ox + col * resolution, oy + row * resolution
        rc = round((wx - o_origin.get("x", 0)) / o_res)
        rr = round((wy - o_origin.get("y", 0)) / o_res)
        if rc < 0 or rr < 0 or rc >= o_w or rr >= o_h:
            return 0
        return room_cells[rr * o_w + rc] >> 2

    names = room_names(snapshot)
    pixels: list[tuple[int, ...]] = []
    room_sums: dict[int, list[int]] = {}
    for row in range(height):
        for col in range(width):
            i = row * width + col
            value = (cells[i >> 2] >> ((i & 3) * 2)) & 3
            if value == CELL_UNKNOWN:
                pixels.append(TRANSPARENT)
                continue
            if value == CELL_OBSTACLE:
                pixels.append(WALL)
                continue
            rid = room_at(col, row)
            if rid in names:
                fill = room_colour(rid)
                acc = room_sums.setdefault(rid, [0, 0, 0])
                acc[0] += col
                acc[1] += row
                acc[2] += 1
            else:
                fill = FLOOR
            pixels.append(_shade(fill, 0.82) if value == CELL_CARPET else fill)

    image = Image.new("RGBA", (width, height))
    image.putdata(pixels)
    image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    image = image.resize((width * scale, height * scale), Image.Resampling.NEAREST)
    if rotation:
        image = image.transpose(_TRANSPOSE[rotation])
    draw = ImageDraw.Draw(image)
    span_w, span_h = width * scale, height * scale

    turns = {
        0: lambda px, py: (px, py),
        90: lambda px, py: (span_h - py, px),
        180: lambda px, py: (span_w - px, span_h - py),
        270: lambda px, py: (py, span_w - px),
    }
    turn = turns[rotation]  # maps a pixel of the unrotated drawing onto the turned one

    def to_px(x: float, y: float) -> tuple[float, float]:
        """World cm → image pixel (centre of the cell), after the flip and turn."""
        col = (x - ox) / resolution
        row = (y - oy) / resolution
        return turn((col + 0.5) * scale, (height - row - 0.5) * scale)

    for dock in geometry.get("docks") or []:
        pose = dock.get("pose") or {}
        if "x" in pose and "y" in pose:
            px, py = to_px(pose["x"], pose["y"])
            r = 2.5 * scale
            draw.rectangle((px - r, py - r, px + r, py + r), fill=DOCK, outline=LABEL)

    robot = snapshot.get("pose") or {}
    if "x" in robot and "y" in robot:
        px, py = to_px(robot["x"], robot["y"])
        r = 3 * scale
        draw.ellipse((px - r, py - r, px + r, py + r), fill=ROBOT, outline=LABEL)
        theta = robot.get("theta")
        if isinstance(theta, (int, float)):
            # Angles arrive as radians x 100. The tip is a world point one robot
            # length ahead, so it goes through the same flip and turn as the robot.
            angle = theta / 100
            reach = r * 1.6 / scale * resolution
            tx, ty = to_px(
                robot["x"] + math.cos(angle) * reach,
                robot["y"] + math.sin(angle) * reach,
            )
            draw.line((px, py, tx, ty), fill=LABEL, width=max(1, scale // 2))

    try:
        font = ImageFont.load_default(size=max(10, 5 * scale))
    except TypeError:  # very old Pillow: no sized default font
        font = ImageFont.load_default()
    for rid, (sx, sy, n) in room_sums.items() if labels else ():
        label = names[rid]
        cx, cy = turn((sx / n + 0.5) * scale, (height - sy / n - 0.5) * scale)
        draw.text(
            (cx, cy),
            label,
            fill=LABEL,
            font=font,
            anchor="mm",
            stroke_width=max(1, scale // 2),
            stroke_fill=LABEL_HALO,
        )

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
