"""Grid voltage sensor entity."""

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfElectricPotential

from ..coordinator import E3dcRscpCoordinator
from .entity import E3dcConnectEntity


class VoltageSensor(E3dcConnectEntity, SensorEntity):
    """A phase voltage measured by the root power meter."""

    def __init__(self, coordinator: E3dcRscpCoordinator, entry, phase: str) -> None:
        super().__init__(coordinator, entry)
        self._phase = phase
        self._attr_name = f"Grid Voltage {phase.upper()}"
        serial = coordinator.storage.serial.lower().replace("-", "_")
        self._attr_unique_id = f"{serial}_grid_voltage_{phase}"
        self._attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
        self._attr_device_class = SensorDeviceClass.VOLTAGE
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        return self.coordinator.storage.grid_voltages.get(self._phase)
