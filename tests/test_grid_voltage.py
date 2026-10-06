"""Grid voltage readings must come from the root power meter."""

from types import SimpleNamespace
import struct
from unittest.mock import Mock

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfElectricPotential
from rscp_lib.RscpValue import RscpValue

from e3dc_rscp_connect.const import DOMAIN
from e3dc_rscp_connect.e3dc_rscp_api.model.StorageRscpModel import StorageRscpModel
from e3dc_rscp_connect.sensor import async_setup_entry


def meter_response(index=0, meter_type=1, **voltages):
    return RscpValue.construct_rscp_value(
        "TAG_PM_DATA",
        [("TAG_PM_INDEX", index), ("TAG_PM_TYPE", meter_type)]
        + [(f"TAG_PM_VOLTAGE_{phase.upper()}", value) for phase, value in voltages.items()],
    )


def test_requests_root_meter_voltages_on_every_poll():
    model = StorageRscpModel(serial="S10-123")
    for _ in range(2):
        meters = [t for t in model.get_rscp_tags() if t.getTagName() == "TAG_PM_REQ_DATA"]
        assert len(meters) == 1
        meter = meters[0]
        assert meter.get_child("TAG_PM_INDEX").getValue() == 0
        for name in ("TYPE", "VOLTAGE_L1", "VOLTAGE_L2", "VOLTAGE_L3"):
            assert meter.get_child(f"TAG_PM_REQ_{name}") is not None


def test_reads_three_phase_voltages_from_root_meter():
    model = StorageRscpModel(serial="S10-123")
    assert model.handle_rscp_data(meter_response(l1=230.1, l2=231.2, l3=229.3))
    assert model.get_model().grid_voltages == pytest.approx(
        {"l1": 230.1, "l2": 231.2, "l3": 229.3}
    )


def test_other_meter_does_not_overwrite_root_voltages():
    model = StorageRscpModel(serial="S10-123")
    model.handle_rscp_data(meter_response(l1=230.0))
    model.handle_rscp_data(meter_response(index=1, meter_type=3, l1=240.0))
    assert model.get_model().grid_voltages["l1"] == 230.0


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf")])
def test_unusable_voltage_is_unknown(value):
    model = StorageRscpModel(serial="S10-123")
    model.handle_rscp_data(meter_response(l1=value, l2=230.0))
    assert model.get_model().grid_voltages["l1"] is None
    assert model.get_model().grid_voltages["l2"] == 230.0


def test_missing_phase_clears_previous_reading():
    model = StorageRscpModel(serial="S10-123")
    model.handle_rscp_data(meter_response(l1=230.0, l2=231.0, l3=232.0))
    model.handle_rscp_data(meter_response(l1=229.0))
    assert model.get_model().grid_voltages == {"l1": 229.0, "l2": None, "l3": None}


def test_phase_error_code_is_not_a_voltage():
    model = StorageRscpModel(serial="S10-123")
    error = RscpValue().withBuffer(struct.pack("<IBHI", 0x05800011, 0xFF, 4, 6))
    response = meter_response(l2=230.0)
    response.getValue().append(error)
    model.handle_rscp_data(response)
    assert model.get_model().grid_voltages["l1"] is None
    assert model.get_model().grid_voltages["l2"] == 230.0


def test_meter_error_clears_previous_reading():
    model = StorageRscpModel(serial="S10-123")
    model.handle_rscp_data(meter_response(l1=230.0))
    error = RscpValue().withBuffer(struct.pack("<IBHI", 0x05040000, 0xFF, 4, 6))
    assert model.handle_rscp_data(error)
    assert all(value is None for value in model.get_model().grid_voltages.values())


def test_poll_without_meter_response_does_not_keep_old_readings():
    model = StorageRscpModel(serial="S10-123")
    model.handle_rscp_data(meter_response(l1=230.0))
    model.get_rscp_tags()
    assert all(value is None for value in model.get_model().grid_voltages.values())


def test_non_root_type_at_index_zero_clears_readings():
    model = StorageRscpModel(serial="S10-123")
    model.handle_rscp_data(meter_response(l1=230.0))
    model.handle_rscp_data(meter_response(meter_type=3, l1=240.0))
    assert all(value is None for value in model.get_model().grid_voltages.values())


@pytest.mark.asyncio
async def test_setup_exposes_live_grid_voltage_sensors():
    model = StorageRscpModel(serial="S10-123")
    coordinator = SimpleNamespace(storage=model.get_model(), wallboxes=[], data={})
    entry = SimpleNamespace(entry_id="test-entry")
    hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: {"coordinator": coordinator}}})
    add_entities = Mock()
    await async_setup_entry(hass, entry, add_entities)
    sensors = [s for s in add_entities.call_args.args[0] if s.device_class == SensorDeviceClass.VOLTAGE]
    assert len(sensors) == 3
    assert [s.name for s in sensors] == ["Grid Voltage L1", "Grid Voltage L2", "Grid Voltage L3"]
    assert len({s.unique_id for s in sensors}) == 3
    for sensor in sensors:
        assert sensor.native_unit_of_measurement == UnitOfElectricPotential.VOLT
        assert sensor.state_class == SensorStateClass.MEASUREMENT
        assert sensor.native_value is None
    model.handle_rscp_data(meter_response(l1=230.1, l2=231.2, l3=229.3))
    assert [s.native_value for s in sensors] == pytest.approx([230.1, 231.2, 229.3])
