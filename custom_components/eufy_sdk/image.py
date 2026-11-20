"""Image platform — the latest detected-event thumbnail, one per camera."""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.components.image import ImageEntity
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .api import EufySdkApiClientError
from .const import CONF_HOST, CONF_PORT, EVENT_TYPE
from .entity import EufySdkDeviceEntity, has_capability
from .pushmap import EVENT_IMAGE_REFRESH, THUMBNAIL_EVENTS
from .vacuum_map import render_map_png

if TYPE_CHECKING:
    from homeassistant.core import Event, HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import EufySdkDataUpdateCoordinator
    from .data import EufySdkConfigEntry

# Bridge device events are re-fired on the HA bus under this type (see __init__.py).


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufySdkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a latest-event image for every device the bridge exposes as a camera."""
    coordinator = entry.runtime_data.coordinator
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    async_add_entities(
        EufySdkEventImage(hass, coordinator, sn, host, port)
        for sn, dev in coordinator.data.items()
        if dev.get("stream")
    )
    async_add_entities(
        EufySdkVacuumMapImage(hass, coordinator, sn)
        for sn, dev in coordinator.data.items()
        if has_capability(dev, "vacuum_clean")
    )


# How often the map is re-read without a trigger, and the least time between two
# re-reads that device updates ask for (a clean pushes state every few seconds).
MAP_REFRESH_INTERVAL = timedelta(minutes=5)
MAP_MIN_REFRESH_SECS = 20.0


class EufySdkVacuumMapImage(EufySdkDeviceEntity, ImageEntity):
    """
    A robot vacuum's map, drawn from the bridge's decoded `device.map` snapshot.

    The robot streams its map only when asked (the vendor app opening it), so the
    bridge serves its latest or saved copy; this redraws it on device updates
    (throttled) and on a slow timer, stamping `image_last_updated` only when the
    drawing changes. Drawing runs in an executor: it walks every cell of the map.
    """

    _attr_translation_key = "map"
    _attr_content_type = "image/png"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: EufySdkDataUpdateCoordinator,
        sn: str,
    ) -> None:
        """Bind to a robot serial."""
        EufySdkDeviceEntity.__init__(self, coordinator, sn)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"{sn}_map"
        self._image: bytes | None = None
        self._hash: str | None = None
        self._last_refresh = 0.0
        self._refresh_lock = asyncio.Lock()

    async def async_added_to_hass(self) -> None:
        """Draw the map now, then keep it fresh on a slow timer."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_timed_refresh, MAP_REFRESH_INTERVAL
            )
        )
        await self.async_refresh_map()

    async def _async_timed_refresh(self, _now: Any) -> None:
        await self.async_refresh_map()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Redraw after a device update, at most once per `MAP_MIN_REFRESH_SECS`."""
        super()._handle_coordinator_update()
        if time.monotonic() - self._last_refresh >= MAP_MIN_REFRESH_SECS:
            self._last_refresh = time.monotonic()
            self.hass.async_create_task(self.async_refresh_map())

    async def async_refresh_map(self) -> None:
        """Fetch the full map, redraw it, and publish only if the drawing changed."""
        async with self._refresh_lock:
            self._last_refresh = time.monotonic()
            try:
                snapshot = await self.client.vacuum_map(self._sn, full=True)
            except EufySdkApiClientError:
                return
            data = await self.hass.async_add_executor_job(render_map_png, snapshot)
            if data is None:
                return
            digest = hashlib.sha1(data, usedforsecurity=False).hexdigest()
            if digest == self._hash:
                return
            self._image = data
            self._hash = digest
            self._attr_image_last_updated = dt_util.utcnow()
            self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        """Return the last drawing (HA's image proxy calls this)."""
        return self._image


class EufySdkEventImage(EufySdkDeviceEntity, ImageEntity):
    """
    Latest-detection-event thumbnail for one device.

    The SDK downloads and retains each event's thumbnail; the bridge serves the
    retained bytes at `/event-image/<sn>`. This entity fetches from the bridge (never
    the raw cloud URL — that needs the SDK's auth/decode). It refreshes on a
    detection push for the device (events that carry a new thumbnail — not
    telemetry like ptzNotify) and on the coordinator poll, stamping a new
    `image_last_updated` only when the bytes change — so an already-retained
    thumbnail shows even before the first event.
    """

    _attr_translation_key = "last_event"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: EufySdkDataUpdateCoordinator,
        sn: str,
        host: str,
        port: int,
    ) -> None:
        """Bind to a device serial + the bridge address."""
        EufySdkDeviceEntity.__init__(self, coordinator, sn)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"{sn}_last_event"
        self._url = f"http://{host}:{port}/event-image/{sn}"
        self._image: bytes | None = None
        self._hash: str | None = None
        # A detection push and the eventImageUpdated nudge both trigger a refresh.
        # Without this the two fetches race: an earlier fetch of the OLD bytes can
        # finish last and overwrite the new image while stamping a fresh time (time
        # moves, picture stale). Serialise so the last write is always the newest fetch.
        self._refresh_lock = asyncio.Lock()

    async def async_added_to_hass(self) -> None:
        """Subscribe to bridge events and pull any already-retained thumbnail."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen(EVENT_TYPE, self._handle_event))
        await self._refresh()

    @callback
    def _handle_event(self, event: Event) -> None:
        """
        Re-pull the thumbnail on a detection or the bridge's image-refresh nudge.

        Fires on a detection event for this device, or on the bridge's
        `eventImageUpdated` nudge once it has a fresh local cover. Telemetry/state
        events the bridge also forwards (ptzNotify, batteryLevel, armingModeChanged,
        …) carry no new thumbnail, so they must not stamp a new "Last event" or spam
        the bridge with refetches. The coordinator poll (`_handle_coordinator_update`)
        still catches any thumbnail no event announced.
        """
        data = event.data
        if data.get("deviceSn") != self._sn:
            return
        ev = data.get("event")
        if ev not in THUMBNAIL_EVENTS and ev != EVENT_IMAGE_REFRESH:
            return
        self.hass.async_create_task(self._refresh())

    @callback
    def _handle_coordinator_update(self) -> None:
        """Piggyback the poll cadence to catch a thumbnail no event announced."""
        self.hass.async_create_task(self._refresh())
        super()._handle_coordinator_update()

    async def _refresh(self) -> None:
        """
        Fetch the retained thumbnail; stamp a new time only when the bytes change.

        Serialised: concurrent refreshes (detection + nudge) must not interleave, or an
        old-bytes fetch that finishes last would clobber the new one.
        """
        async with self._refresh_lock:
            data = await self._fetch()
            if data is None:
                return
            digest = hashlib.sha1(data, usedforsecurity=False).hexdigest()
            if digest == self._hash:
                return
            self._image = data
            self._hash = digest
            self._attr_image_last_updated = dt_util.utcnow()
            self.async_write_ha_state()

    async def _fetch(self) -> bytes | None:
        """GET the retained thumbnail bytes from the bridge, or None if unavailable."""
        session = async_get_clientsession(self.hass)
        try:
            resp = await session.get(self._url, timeout=aiohttp.ClientTimeout(total=15))
            if resp.status != HTTPStatus.OK:
                return None
            return await resp.read()
        except (aiohttp.ClientError, TimeoutError):
            return None

    async def async_image(self) -> bytes | None:
        """Return the last thumbnail we fetched (HA's image proxy calls this)."""
        return self._image
