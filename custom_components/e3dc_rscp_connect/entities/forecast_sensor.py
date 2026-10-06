"""Daily forecast estimates, separate from measured production counters."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfEnergy
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from ..const import DOMAIN


class ForecastSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator, entry, serial: str, day_offset: int):
        super().__init__(coordinator)
        self._day_offset = day_offset
        label = "today" if day_offset == 0 else "tomorrow"
        self._attr_translation_key = f"pv_forecast_{label}"
        self._attr_unique_id = (
            serial.lower().replace("-", "_") + f"_pv_forecast_{label}"
        )
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    def _day(self):
        zone = ZoneInfo(self.coordinator.hass.config.time_zone)
        day = dt_util.now().astimezone(zone).date() + timedelta(days=self._day_offset)
        return day, zone

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        day, zone = self._day()
        return self.coordinator.data.for_day(day, zone)[0]

    @property
    def available(self):
        # A failed cloud request must not erase the last usable forecast.
        return self.native_value is not None

    @property
    def extra_state_attributes(self):
        if self.coordinator.data is None:
            return {}
        day, zone = self._day()
        return {
            "forecast_date": day.isoformat(),
            "last_update": self.coordinator.data.fetched_at.isoformat(),
            "stale": not self.coordinator.last_update_success,
            "hourly": self.coordinator.data.for_day(day, zone)[1],
        }

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_change(
                self.hass, self._midnight_update, hour=0, minute=0, second=0
            )
        )

    @callback
    def _midnight_update(self, now):
        self.async_write_ha_state()
