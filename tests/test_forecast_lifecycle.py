"""Forecast behavior at Home Assistant's entity and entry boundaries."""

import asyncio
from datetime import UTC, date, datetime, timedelta
import logging
from unittest.mock import AsyncMock, Mock, patch
import uuid
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_component import EntityComponent
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

import e3dc_rscp_connect as integration
from e3dc_rscp_connect.entities.forecast_sensor import ForecastSensor
from e3dc_rscp_connect.forecast import ForecastCoordinator, ForecastData, PortalError


def daily_points(day, power=1000):
    zone = ZoneInfo("Europe/Berlin")
    start = datetime.combine(day, datetime.min.time(), zone).astimezone(UTC)
    end = datetime.combine(
        day + timedelta(days=1), datetime.min.time(), zone
    ).astimezone(UTC)
    return [
        {"time": (start + timedelta(hours=i)).isoformat(), "power": power}
        for i in range(int((end - start).total_seconds() / 3600))
    ]


async def add_sensor(hass, coordinator):
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    await component.async_setup({})
    sensor = ForecastSensor(coordinator, Mock(entry_id="test"), "S10-123456789012", 0)
    sensor.entity_id = "sensor.pv_forecast_today"
    await component.async_add_entities([sensor])
    return component


async def remove_sensors(component):
    for sensor in tuple(component.entities):
        await sensor.async_remove()


@pytest.mark.asyncio
async def test_cloud_failure_keeps_published_state_and_recovery_clears_stale(
    hass, freezer
):
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to("2026-10-05T10:00:00Z")
    client = Mock(
        async_forecast=AsyncMock(return_value=daily_points(date(2026, 10, 5)))
    )
    coordinator = ForecastCoordinator(hass, client)
    await coordinator.async_refresh()
    component = await add_sensor(hass, coordinator)
    before = hass.states.get("sensor.pv_forecast_today")
    assert before.state == "24.0"
    assert before.attributes["stale"] is False

    client.async_forecast.side_effect = PortalError("Portal unavailable")
    await coordinator.async_refresh()
    after = hass.states.get("sensor.pv_forecast_today")
    assert after.state == "24.0"
    assert after.attributes["stale"] is True
    assert after.attributes["last_update"] == before.attributes["last_update"]

    freezer.move_to("2026-10-05T10:01:00Z")
    client.async_forecast.side_effect = None
    client.async_forecast.return_value = daily_points(date(2026, 10, 5), 2000)
    await coordinator.async_refresh()
    recovered = hass.states.get("sensor.pv_forecast_today")
    assert recovered.state == "48.0"
    assert recovered.attributes["stale"] is False
    assert recovered.attributes["last_update"] != before.attributes["last_update"]
    await remove_sensors(component)
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_incomplete_forecast_is_published_as_unavailable(hass, freezer):
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to("2026-10-05T10:00:00Z")
    points = daily_points(date(2026, 10, 5))
    del points[12]
    client = Mock(async_forecast=AsyncMock(return_value=points))
    coordinator = ForecastCoordinator(hass, client)
    await coordinator.async_refresh()
    component = await add_sensor(hass, coordinator)
    state = hass.states.get("sensor.pv_forecast_today")
    assert state.state == "unavailable"
    await remove_sensors(component)
    await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_midnight_changes_published_day_without_portal_request(hass, freezer):
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to("2026-10-05T21:59:59Z")
    client = Mock(
        async_forecast=AsyncMock(
            return_value=(
                daily_points(date(2026, 10, 5)) + daily_points(date(2026, 10, 6), 2000)
            )
        )
    )
    coordinator = ForecastCoordinator(hass, client)
    await coordinator.async_refresh()
    component = await add_sensor(hass, coordinator)
    assert hass.states.get("sensor.pv_forecast_today").state == "24.0"

    midnight = datetime(2026, 10, 5, 22, 0, 1, tzinfo=UTC)
    freezer.move_to(midnight)
    async_fire_time_changed(hass, midnight)
    await hass.async_block_till_done()
    after = hass.states.get("sensor.pv_forecast_today")
    assert after.state == "48.0"
    assert after.attributes["forecast_date"] == "2026-10-06"
    assert client.async_forecast.await_count == 1

    await remove_sensors(component)
    await coordinator.async_shutdown()
    removed = hass.states.get("sensor.pv_forecast_today")
    assert removed.state == "unavailable"
    tomorrow = datetime(2026, 10, 6, 22, 0, 1, tzinfo=UTC)
    freezer.move_to(tomorrow)
    async_fire_time_changed(hass, tomorrow)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.pv_forecast_today") is removed
    assert client.async_forecast.await_count == 1


@pytest.mark.parametrize("pending_request", ["initial", "hourly"])
@pytest.mark.asyncio
async def test_local_start_and_unload_with_pending_portal_request(
    hass, freezer, pending_request
):
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to("2026-10-05T10:00:00Z")
    started, cancelled = asyncio.Event(), asyncio.Event()
    calls = 0

    async def fetch(now):
        nonlocal calls
        calls += 1
        if pending_request == "hourly" and calls == 1:
            return daily_points(date(2026, 10, 5))
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cancelled.set()

    client = Mock(async_forecast=fetch)
    entry = MockConfigEntry(
        domain=integration.DOMAIN,
        title="Storage",
        state=ConfigEntryState.LOADED,
        data={
            "forecast_enabled": True,
            "portal_username": uuid.uuid4().hex,
            "portal_password": uuid.uuid4().hex,
        },
    )
    entry.add_to_hass(hass)
    local = Mock(
        storage=Mock(serial="S10-123456789012"),
        async_connect=AsyncMock(),
        async_config_entry_first_refresh=AsyncMock(),
        stop_remote_control=AsyncMock(),
    )
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    await component.async_setup({})

    class LocalSensor(SensorEntity):
        _attr_native_value = 123
        _attr_should_poll = False

    async def forward(entry, platforms):
        sensor = LocalSensor()
        sensor.entity_id = "sensor.local_rscp"
        await component.async_add_entities([sensor])
        forecast = hass.data[integration.DOMAIN][entry.entry_id]["forecast"]
        sensor = ForecastSensor(forecast, entry, "S10-123456789012", 0)
        sensor.entity_id = "sensor.pv_forecast_today"
        await component.async_add_entities([sensor])

    async def unload(entry, platforms):
        await remove_sensors(component)
        return True

    shared_session = async_get_clientsession(hass)
    with (
        patch.object(integration, "E3dcRscpCoordinator", return_value=local),
        patch.object(integration, "PortalClient", return_value=client),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", side_effect=forward
        ),
        patch.object(hass.config_entries, "async_unload_platforms", side_effect=unload),
    ):
        setup_task = asyncio.create_task(integration.async_setup_entry(hass, entry))
        request_started = asyncio.create_task(started.wait())
        try:
            await asyncio.wait(
                [setup_task, request_started], return_when=asyncio.FIRST_COMPLETED
            )
            assert setup_task.done(), "Local setup waits for the portal request"
            assert setup_task.result()
        finally:
            setup_task.cancel()
            request_started.cancel()
            await asyncio.gather(setup_task, request_started, return_exceptions=True)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.local_rscp").state == "123"
        session = hass.data[integration.DOMAIN][entry.entry_id]["forecast_session"]
        if pending_request == "hourly":
            later = datetime(2026, 10, 5, 11, 0, 1, tzinfo=UTC)
            freezer.move_to(later)
            async_fire_time_changed(hass, later)
            await hass.async_block_till_done()
        assert started.is_set()
        assert await integration.async_unload_entry(hass, entry)
        await entry._async_process_on_unload(hass)
        assert cancelled.is_set()
        assert session.closed
        assert not shared_session.closed
        assert not shared_session.connector.closed
        assert hass.states.get("sensor.local_rscp") is None
        assert hass.states.get("sensor.pv_forecast_today").state == "unavailable"

    before = calls
    later = datetime(2026, 10, 5, 13, 0, 1, tzinfo=UTC)
    freezer.move_to(later)
    async_fire_time_changed(hass, later)
    await hass.async_block_till_done()
    assert calls == before


@pytest.mark.parametrize("missing_hour", [0, 12, 23])
def test_incomplete_day_is_unavailable(missing_hour):
    points = daily_points(date(2026, 10, 5))
    del points[missing_hour]
    data = ForecastData.from_points(points, datetime.now(UTC))
    total, hourly = data.for_day(date(2026, 10, 5), ZoneInfo("Europe/Berlin"))
    assert total is None
    assert len(hourly) == 23


def test_shifted_hourly_series_is_not_a_complete_calendar_day():
    points = daily_points(date(2026, 10, 5))
    points[12]["time"] = "2026-10-05T10:30:00Z"
    data = ForecastData.from_points(points, datetime.now(UTC))
    assert data.for_day(date(2026, 10, 5), ZoneInfo("Europe/Berlin"))[0] is None
