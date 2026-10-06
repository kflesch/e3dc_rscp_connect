"""Optional PV forecast from the E3/DC customer portal."""

import asyncio
import base64
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import json
import logging
import math
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

import aiohttp
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)
LOGIN_URL = "https://e3dc.e3dc.com/auth-saml/service-providers/customer/login?app=e3dc"
ASSERT_URL = "https://e3dc.e3dc.com/auth-saml/service-providers/customer/assert"
REFRESH_URL = "https://e3dc.e3dc.com/auth-saml/re-auth"
_HOSTS = {
    "e3dc.e3dc.com",
    "auth.hagerenergy.com",
    "my.e3dc.com",
    "weather-api.e3dc.com",
}


class PortalError(Exception):
    """A portal request failed without exposing its URL, body or credentials."""


class PortalAuthError(PortalError):
    """The portal requires new credentials."""


def _check_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise PortalError("Unexpected portal destination")


def _expiry(token: str) -> datetime:
    try:
        payload = token.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
        return datetime.fromtimestamp(claims["exp"], UTC)
    except (ValueError, KeyError, IndexError, TypeError):
        raise PortalAuthError("Invalid portal session") from None


class PortalClient:
    """Keep portal tokens in memory; authenticate with the normal SAML login."""

    def __init__(self, session, username: str, password: str, serial: str) -> None:
        self.session = session
        self.username = username
        self.password = password
        self.serial = serial.rsplit("-", 1)[-1]
        if not self.serial.isdigit():
            raise PortalError("Invalid storage identifier")
        self.token: str | None = None
        self.refresh_token: str | None = None

    async def _request(self, method, url, **kwargs):
        _check_url(url)
        try:
            async with self.session.request(
                method,
                url,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=30),
                **kwargs,
            ) as response:
                return (
                    response.status,
                    await response.text(),
                    response.headers.get("Location"),
                )
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # Authentication URLs and exception strings may contain session tokens.
            raise PortalError("Portal connection failed") from None

    async def _page(self, url):
        for _ in range(6):
            status, body, location = await self._request("GET", url)
            if status in (301, 302, 303, 307, 308) and location:
                url = urljoin(url, location)
                continue
            if status != 200:
                raise PortalError("Portal login page unavailable")
            return body
        raise PortalError("Too many portal redirects")

    async def async_login(self) -> None:
        page = await self._page(LOGIN_URL)
        match = re.search(r'["\']loginAction["\']\s*:\s*("(?:[^"\\]|\\.)*")', page)
        if not match:
            raise PortalAuthError("Portal login form unavailable")
        action = json.loads(match.group(1))
        _check_url(action)
        target = urlsplit(action)
        if (
            target.hostname != "auth.hagerenergy.com"
            or target.path != "/realms/customer/login-actions/authenticate"
        ):
            raise PortalError("Unexpected login form destination")
        status, page, _ = await self._request(
            "POST",
            action,
            data={
                "username": self.username,
                "password": self.password,
                "credentialId": "",
            },
        )
        if status != 200:
            raise PortalAuthError("Portal authentication failed")
        match = re.search(r'"samlPost"\s*:\s*', page)
        if not match:
            raise PortalAuthError("Portal authentication requires attention")
        # Keycloak emits JavaScript with undefined values, not a JSON document.
        block = page[match.end() :].split("}", 1)[0]
        fields = {
            match.group(1): json.loads(match.group(2))
            for match in re.finditer(r'"([A-Za-z_]+)"\s*:\s*("(?:[^"\\]|\\.)*")', block)
        }
        if fields.get("url") != ASSERT_URL or not fields.get("SAMLResponse"):
            raise PortalAuthError("Invalid portal authentication response")
        data = {"SAMLResponse": fields["SAMLResponse"]}
        if fields.get("relayState"):
            data["RelayState"] = fields["relayState"]
        status, _, location = await self._request("POST", ASSERT_URL, data=data)
        if status not in (302, 303) or not location:
            raise PortalAuthError("Portal session unavailable")
        _check_url(location)
        target = urlsplit(location)
        if target.hostname != "my.e3dc.com" or target.path != "/login":
            raise PortalAuthError("Unexpected portal session response")
        params = parse_qs(target.query)
        self._set_tokens(
            {key: params.get(key, [None])[0] for key in ("token", "reAuthToken")}
        )

    def _set_tokens(self, result):
        if (
            not isinstance(result, dict)
            or not result.get("token")
            or not result.get("reAuthToken")
        ):
            raise PortalAuthError("Invalid portal session response")
        _expiry(result["token"])
        _expiry(result["reAuthToken"])
        self.token, self.refresh_token = result["token"], result["reAuthToken"]

    async def async_refresh_token(self) -> None:
        if not self.refresh_token or _expiry(self.refresh_token) <= datetime.now(
            UTC
        ) + timedelta(seconds=60):
            await self.async_login()
            return
        form = aiohttp.FormData()
        form.add_field("reAuthToken", self.refresh_token, content_type="text/plain")
        status, body, _ = await self._request("POST", REFRESH_URL, data=form)
        if status in (401, 403):
            await self.async_login()
            return
        if status != 200:
            raise PortalError("Portal session renewal failed")
        try:
            result = json.loads(body)
        except ValueError:
            raise PortalError("Invalid portal renewal response") from None
        self._set_tokens(result)

    async def async_forecast(self, now: datetime) -> list[dict]:
        if not self.token:
            await self.async_login()
        elif _expiry(self.token) <= datetime.now(UTC) + timedelta(seconds=60):
            await self.async_refresh_token()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=4)
        query = urlencode(
            {
                "limit": 3072,
                "sort": "+time",
                "time_min": int(start.timestamp() * 1000),
                "time_max": int(end.timestamp() * 1000) - 1,
            }
        )
        url = f"https://weather-api.e3dc.com/weather-forecast/pv/{self.serial}/power?{query}"
        for attempt in range(2):
            status, body, _ = await self._request(
                "GET", url, headers={"Authorization": "Bearer " + self.token}
            )
            if status in (401, 403) and attempt == 0:
                await self.async_refresh_token()
                continue
            if status != 200:
                raise PortalError("Portal forecast unavailable")
            try:
                points = json.loads(body)
            except ValueError:
                raise PortalError("Invalid portal forecast response") from None
            if not isinstance(points, list):
                raise PortalError("Invalid portal forecast response")
            return points
        raise PortalAuthError("Portal forecast access denied")


@dataclass(frozen=True)
class ForecastData:
    points: tuple[tuple[datetime, float], ...]
    fetched_at: datetime

    @classmethod
    def from_points(cls, points, fetched_at):
        parsed = []
        try:
            for point in points:
                when = datetime.fromisoformat(point["time"].replace("Z", "+00:00"))
                power = point["power"]
                if (
                    when.tzinfo is None
                    or not isinstance(power, (int, float))
                    or isinstance(power, bool)
                    or not math.isfinite(power)
                    or power < 0
                ):
                    raise ValueError
                parsed.append((when.astimezone(UTC), float(power)))
            parsed.sort()
            if len({when for when, _ in parsed}) != len(parsed):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise PortalError("Invalid portal forecast values") from None
        return cls(tuple(parsed), fetched_at)

    def for_day(self, day: date, zone: ZoneInfo):
        start = datetime.combine(day, datetime.min.time(), zone).astimezone(UTC)
        end = datetime.combine(
            day + timedelta(days=1), datetime.min.time(), zone
        ).astimezone(UTC)
        points = [
            {
                "time": when.astimezone(zone).isoformat(),
                "power_w": power,
                "energy_kwh": power / 1000,
            }
            for when, power in self.points
            if start <= when < end
        ]
        expected_hours = int((end - start).total_seconds() / 3600)
        complete = {when for when, _ in self.points if start <= when < end} == {
            start + timedelta(hours=i) for i in range(expected_hours)
        }
        return (
            round(sum(point["energy_kwh"] for point in points), 3) if complete else None,
            points,
        )


class ForecastCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, client, entry=None):
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="E3/DC PV forecast",
            update_interval=timedelta(hours=1),
        )
        self.client = client

    async def _async_update_data(self):
        try:
            now = dt_util.now()
            points = await self.client.async_forecast(now)
            return ForecastData.from_points(points, datetime.now(UTC))
        except PortalError as err:
            raise UpdateFailed(str(err)) from None
        except (ValueError, KeyError, TypeError):
            raise UpdateFailed("Invalid portal response") from None
