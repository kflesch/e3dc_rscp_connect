"""Portal and forecast boundaries, using synthetic sessions and generated secrets."""

import base64
from datetime import UTC, date, datetime, timedelta
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import uuid
from zoneinfo import ZoneInfo

import aiohttp
import pytest


def module():
    return importlib.import_module("e3dc_rscp_connect.forecast")


def jwt(exp):
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    )
    return f"eyJ0eXAiOiJKV1QifQ.{payload}.{uuid.uuid4().hex}"


class Response:
    def __init__(self, status=200, body="", headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def text(self):
        return self.body


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def login_responses(token, refresh):
    action = "https://auth.hagerenergy.com/realms/customer/login-actions/authenticate?session_code=synthetic"
    return [
        Response(
            302,
            headers={
                "Location": "https://auth.hagerenergy.com/realms/customer/protocol/saml"
            },
        ),
        Response(body=json.dumps({"url": {"loginAction": action}})),
        Response(
            body='"samlPost": {"SAMLResponse": "synthetic", "url": "https://e3dc.e3dc.com/auth-saml/service-providers/customer/assert", "relayState": undefined}'
        ),
        Response(
            302,
            headers={
                "Location": "https://my.e3dc.com/login?token="
                + token
                + "&reAuthToken="
                + refresh
            },
        ),
    ]


@pytest.mark.asyncio
async def test_login_and_refresh_can_fetch_forecast_without_browser():
    api = module()
    now = datetime.now(UTC).timestamp()
    token, refresh = jwt(now + 600), jwt(now + 2592000)
    new_token = jwt(now + 1200)
    points = [{"time": "2026-10-05T08:00:00.000Z", "power": 2000}]
    session = Session(
        login_responses(token, refresh)
        + [
            Response(body=json.dumps(points)),
            Response(body=json.dumps({"token": new_token, "reAuthToken": refresh})),
            Response(body=json.dumps(points)),
        ]
    )
    username, password = uuid.uuid4().hex, uuid.uuid4().hex
    client = api.PortalClient(session, username, password, "S10-123456789012")
    assert await client.async_forecast(datetime(2026, 10, 5, tzinfo=UTC)) == points
    await client.async_refresh_token()
    assert await client.async_forecast(datetime(2026, 10, 5, tzinfo=UTC)) == points
    login_post = session.calls[2]
    assert login_post[2]["data"]["username"] == username
    assert login_post[2]["data"]["password"] == password
    assert session.calls[-1][2]["headers"]["Authorization"] == "Bearer " + new_token


@pytest.mark.asyncio
async def test_credentials_never_sent_to_unexpected_form_target():
    api = module()
    session = Session(
        [
            Response(
                body=json.dumps(
                    {"url": {"loginAction": "https://example.invalid/login"}}
                )
            )
        ]
    )
    client = api.PortalClient(
        session, uuid.uuid4().hex, uuid.uuid4().hex, "S10-123456789012"
    )
    with pytest.raises(api.PortalError):
        await client.async_login()
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_failed_login_exposes_no_password_or_response_body():
    api = module()
    password = uuid.uuid4().hex
    action = "https://auth.hagerenergy.com/realms/customer/login-actions/authenticate"
    session = Session(
        [
            Response(body=json.dumps({"url": {"loginAction": action}})),
            Response(body=password),
        ]
    )
    client = api.PortalClient(session, uuid.uuid4().hex, password, "S10-123456789012")
    with pytest.raises(api.PortalAuthError) as error:
        await client.async_login()
    assert password not in str(error.value)


@pytest.mark.asyncio
async def test_network_errors_do_not_expose_request_urls_or_tokens():
    api = module()
    secret = uuid.uuid4().hex
    session = Session([aiohttp.ClientError(secret)])
    client = api.PortalClient(session, uuid.uuid4().hex, secret, "S10-123456789012")
    with pytest.raises(api.PortalError) as error:
        await client.async_login()
    assert secret not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.asyncio
async def test_rejected_refresh_falls_back_to_login():
    api = module()
    now = datetime.now(UTC).timestamp()
    token, refresh = jwt(now + 600), jwt(now + 2592000)
    session = Session(
        [Response(401)] + login_responses(token, refresh) + [Response(body="[]")]
    )
    client = api.PortalClient(
        session, uuid.uuid4().hex, uuid.uuid4().hex, "S10-123456789012"
    )
    client.token, client.refresh_token = jwt(now - 1), refresh
    await client.async_forecast(datetime(2026, 10, 5, tzinfo=UTC))
    assert any("/customer/login?app=e3dc" in url for _, url, _ in session.calls)


@pytest.mark.parametrize(
    "day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)]
)
def test_daily_energy_respects_local_day_and_dst(day, hours):
    api = module()
    zone = ZoneInfo("Europe/Berlin")
    start = datetime.combine(day, datetime.min.time(), zone).astimezone(UTC)
    data = api.ForecastData.from_points(
        [
            {"time": (start + timedelta(hours=i)).isoformat(), "power": 1000}
            for i in range(hours)
        ],
        start,
    )
    total, points = data.for_day(day, zone)
    assert total == hours
    assert len(points) == hours
    assert points[0]["time"].endswith("+01:00") or points[0]["time"].endswith("+02:00")
    assert data.for_day(day + timedelta(days=1), zone)[0] is None


@pytest.mark.parametrize(
    "points",
    [
        [{"time": "2026-10-05T08:00:00", "power": 100}],
        [{"time": "2026-10-05T08:00:00Z", "power": None}],
        [{"time": "2026-10-05T08:00:00Z", "power": -1}],
    ],
)
def test_invalid_forecast_never_becomes_zero_energy(points):
    api = module()
    with pytest.raises(api.PortalError):
        api.ForecastData.from_points(points, datetime.now(UTC))


def test_forecast_sensor_changes_day_without_new_request():
    api = module()
    sensors = importlib.import_module("e3dc_rscp_connect.entities.forecast_sensor")
    data = api.ForecastData.from_points(
        [
            {
                "time": (
                    datetime(2026, 10, 4, 22, tzinfo=UTC) + timedelta(hours=i)
                ).isoformat(),
                "power": 2000 if i == 10 else 3000 if i == 34 else 0,
            }
            for i in range(48)
        ],
        datetime.now(UTC),
    )
    coordinator = Mock(data=data, last_update_success=False)
    coordinator.hass.config.time_zone = "Europe/Berlin"
    sensor = sensors.ForecastSensor(
        coordinator, SimpleNamespace(entry_id="entry"), "S10-123456789012", 0
    )
    with patch.object(
        sensors.dt_util,
        "now",
        return_value=datetime(2026, 10, 5, 23, 59, tzinfo=ZoneInfo("Europe/Berlin")),
    ):
        assert sensor.native_value == 2
        assert sensor.available
        assert sensor.extra_state_attributes["stale"] is True
    with patch.object(
        sensors.dt_util,
        "now",
        return_value=datetime(2026, 10, 6, 0, 1, tzinfo=ZoneInfo("Europe/Berlin")),
    ):
        assert sensor.native_value == 3
    assert sensor.state_class is None


def test_optional_portal_fields_are_separate_from_local_password():
    config = importlib.import_module("e3dc_rscp_connect.config_flow")
    schema = config.portal_schema({})
    assert schema({"forecast_enabled": False})["forecast_enabled"] is False
    assert config.portal_errors({"forecast_enabled": True})
    values = {
        "forecast_enabled": True,
        "portal_username": uuid.uuid4().hex,
        "portal_password": uuid.uuid4().hex,
    }
    assert config.portal_errors(schema(values)) == {}


@pytest.mark.asyncio
async def test_api_rejection_renews_token_and_retries_once():
    api = module()
    now = datetime.now(UTC).timestamp()
    old_token, new_token, refresh = jwt(now + 600), jwt(now + 1200), jwt(now + 2592000)
    session = Session(
        [
            Response(401),
            Response(body=json.dumps({"token": new_token, "reAuthToken": refresh})),
            Response(body="[]"),
        ]
    )
    client = api.PortalClient(
        session, uuid.uuid4().hex, uuid.uuid4().hex, "S10-123456789012"
    )
    client.token, client.refresh_token = old_token, refresh
    assert await client.async_forecast(datetime.now(UTC)) == []
    assert len(session.calls) == 3
    assert session.calls[-1][2]["headers"]["Authorization"] == "Bearer " + new_token


@pytest.mark.asyncio
async def test_options_keep_portal_password_when_field_is_empty():
    config = importlib.import_module("e3dc_rscp_connect.config_flow")
    local_password, portal_password = uuid.uuid4().hex, uuid.uuid4().hex
    current = {
        "host": "192.0.2.1",
        "port": 5033,
        "login_type": "local",
        "username": "local.user",
        "password": local_password,
        "key": uuid.uuid4().hex,
        "forecast_enabled": True,
        "portal_username": uuid.uuid4().hex,
        "portal_password": portal_password,
    }

    class Flow(config.OptionsFlowHandler):
        @property
        def config_entry(self):
            return SimpleNamespace(data=current, options={})

    flow = Flow()
    flow.hass = Mock()
    flow.async_create_entry = Mock(side_effect=lambda **kwargs: kwargs)
    session = Mock()
    with (
        patch.object(config, "async_create_clientsession", return_value=session),
        patch.object(config.PortalClient, "async_login", new_callable=AsyncMock),
    ):
        result = await flow.async_step_init({**current, "portal_password": ""})
    assert result["data"]["password"] == local_password
    assert result["data"]["portal_password"] == portal_password
    assert (
        dict((str(k), k.default()) for k in config.portal_schema(current).schema)[
            "portal_password"
        ]
        == ""
    )
    session.detach.assert_called_once()


def test_missing_day_is_unavailable_instead_of_zero():
    api = module()
    sensors = importlib.import_module("e3dc_rscp_connect.entities.forecast_sensor")
    coordinator = Mock(data=api.ForecastData.from_points([], datetime.now(UTC)))
    coordinator.hass.config.time_zone = "Europe/Berlin"
    sensor = sensors.ForecastSensor(
        coordinator, SimpleNamespace(entry_id="entry"), "S10-123456789012", 0
    )
    assert sensor.native_value is None
    assert sensor.available is False
