![Tests](https://github.com/tobias-terhaar/e3dc_rscp_connect/actions/workflows/tests.yml/badge.svg)

# E3DC RSCP Connect

A [Home Assistant](https://www.home-assistant.io/) custom integration for **E3/DC** energy storage systems (S10 battery storage). It communicates directly with the device on your local network using the proprietary **RSCP** (Remote Storage Control  Protocol), giving you access to your battery storage, connected wallboxes and power meters — without going through the E3/DC cloud.

## Purpose of this fork and branch selection

This fork develops additions to supply E3/DC measurements and PV forecasts to
[Ortsnetzauslastung](https://www.ortsnetz-auslastung.de/) through the separate
[Home Assistant Ortsnetzauslastung integration](https://github.com/thomaslehmann1234/ha_ortsnetz_auslastung).
E3DC RSCP Connect exposes Home Assistant entities; configure their use and
transmission in the Ortsnetzauslastung integration separately.

**`main` is the clean fork without feature additions:** it follows upstream
functionality and contains neither the additional smartmeter sensors nor the
portal forecast. Its adjustments are the fork documentation and a dependency
compatibility fix (`defusedxml>=0.7.1`) required by Home Assistant's validator.
It runs with local RSCP measurements and does not need a PV forecast.

Choose the branch according to the features you need:

| Branch to install | Local RSCP measurements | Additional grid voltage L1/L2/L3 sensors | Optional portal PV forecast |
| --- | --- | --- | --- |
| [`main`](https://github.com/kflesch/e3dc_rscp_connect/tree/main) | Yes | No | No |
| [`smartmeter-phasenspannungen`](https://github.com/kflesch/e3dc_rscp_connect/tree/smartmeter-phasenspannungen) | Yes | Yes | No |
| [`pv-prognose`](https://github.com/kflesch/e3dc_rscp_connect/tree/pv-prognose) | Yes | Yes | Yes |

The smartmeter branch adds voltages from the main grid meter for use by
Ortsnetzauslastung. The forecast branch builds on that branch and includes all
three voltage sensors plus production estimates for today and tomorrow, with
hourly values. For Ortsnetzauslastung with E3/DC measurements and the E3/DC portal
forecast, install `pv-prognose`.

The portal forecast is optional and uses separately configured credentials.
Select today's forecast sensor as the PV forecast source in the
Ortsnetzauslastung integration when enabling it. The voltage sensors remain
available when the forecast is disabled.

Each branch is a complete installation of the same Home Assistant integration.
Install only one branch; do not mix files from different branches.

The detailed feature list below describes the branch whose README you are reading.
This fork is installed manually and will not be submitted for inclusion in HACS.
The test badge refers to the upstream repository.

## Features

- Autodetection of connected storage systems and auto commissioning of all wallboxes connected to the storage system.
- Local polling over TCP (port `5033`) using Rijndael-256 encrypted RSCP frames — no cloud dependency.
- Live readings for the main storage system:
  - State of charge, battery power, battery state
  - PV production, grid import/export, house consumption
  - Grid voltage L1, L2 and L3 from the root power meter (index 0, `PM_TYPE_ROOT`)
  - Energy counters (daily / total)
  - Emergency power status
  - Device state and firmware update state
- Wallbox support:
  - Charge power and charging state
  - Adjustable charging current
  - for every connected wallbox
- SG-Ready heat pump signal
- Sun mode / battery remote control
- UI-based configuration (no YAML required) with an options flow to update credentials and polling interval after setup.
- Optional portal PV forecasts for today and tomorrow, including hourly estimates.

### Optional PV forecast

After setting up the local connection, open the integration's options and enable
**Enable portal PV forecast**. Enter your separate **my.e3dc.com** portal username
and password. These credentials are independent of the local RSCP login; leave
the portal password field empty when editing options to keep its stored value.

Two sensors provide estimated daily production in kWh. Their `hourly` attribute
contains timestamps with the configured Home Assistant time zone, `power_w` and
`energy_kwh`. `last_update` records the successful fetch; `stale` indicates that
the most recent fetch failed. Missing or incomplete days are unavailable, not
zero. Forecasts are estimates and do not have a total-increasing state class.

The forecast is fetched every hour through the portal's customer SAML login.
Access and renewal tokens stay in memory and are renewed automatically; no
browser session or copied token is required. Portal outages do not interrupt
local RSCP measurements. The last successful forecast remains available while
its day is still present in the cached series. The sensors switch days at local
midnight, including daylight saving time transitions.

This optional feature requires Internet access. It uses the portal interface,
which may change independently of this integration. Interactive login steps such
as MFA require attention and are not bypassed. Disable the forecast in options
to return to local-only operation.

## Requirements

Grid voltage sensors read the smart meter at the grid connection, not the PV
inverter. They remain unknown if the meter does not support voltage readings,
returns an error, or reports zero/missing values. No grid-frequency reading is
exposed for this meter by the supported RSCP tags.

- Home Assistant **2025.10.0** or newer
- An E3/DC S10 system reachable on your local network
- **RSCP password** (set on the device under `Personalize → User profile → RSCP password`)
- A E3/DC portal user account (username/email + password) or a configured password for local.user on the storage (`Personalize  → User profile → Password for offline RSCP User` )

## Installation

### Install a branch from this fork manually

1. Choose a branch from the table above and open its GitHub link.
2. Use **Code → Download ZIP** on that branch, then extract the archive.
3. Back up any existing `config/custom_components/e3dc_rscp_connect/` directory.
4. Replace that directory with the complete `custom_components/e3dc_rscp_connect/`
   folder from the chosen archive. Do not mix files from different branches.
5. Restart Home Assistant.

The branch selected when downloading determines which additions are installed.

## Configuration

Add the integration via **Settings → Devices & services → Add integration → E3DC RSCP connect**
and provide:

| Field       | Description                                          | Default      |
|-------------|------------------------------------------------------|--------------|
| login_type  | *Local user* or *Portal user*                        | Local user   |
| host        | IP address or hostname of your E3/DC system          | —            |
| port        | RSCP TCP port                                        | `5033`       |
| username    | Your E3/DC portal email address (portal login only)  | —            |
| password    | Password of the portal or the local user             | —            |
| key         | RSCP password configured on the device               | —            |

The login method is a dropdown on the form itself, so it can be changed at any point before
submitting — also when you come back to a setup you left half finished:

- **Local user** — authenticates as the fixed user `local.user`; leave the username empty.
- **Portal user** — authenticates with your E3/DC portal credentials; the username is required.

Devices found via SSDP discovery are offered the same choice; host and port are taken from the
device's UPnP description.

The options flow lets you change these values and the polling interval (default: 10 seconds) without removing the integration.

## Architecture

All device communication lives in `e3dc_rscp_api`, a self-contained package with no Home Assistant
imports that is meant to become a standalone library. The integration above it never sees an RSCP
tag, frame or connection — it only reads the plain dataclasses the api returns.

This architecture is inherited from upstream.

```
Home Assistant Config Entry
    ↓
E3dcRscpCoordinator (DataUpdateCoordinator)          ── integration
    ├─ polls every 10s (configurable)
    └─ device info refresh every 60 min
    ↓                                        ↑ plain dataclasses
─────────────────────────────────────────────────────────────────
RscpClient                                           ── e3dc_rscp_api
    ├─ RscpConnection  →  RscpEncryption  →  RscpFrame / RscpValue
    └─ RscpHandlerPipeline
         ├─ StorageRscpModel   →  StorageDataModel
         ├─ WallboxRscpModel   →  WallboxDataModel
         └─ SgReadyRscpModel   →  SgReadyDataModel
              ↓
         Sensor / Select / Number Entities
```

- **Coordinator** (`coordinator.py`) drives all periodic fetches; entities subscribe through `CoordinatorEntity`.
- **Api boundary**: everything the integration needs is re-exported from `e3dc_rscp_api/__init__.py` — the client, the data models, and the `E3dcRscpError` hierarchy. Errors of the underlying protocol never leave the package. `tests/test_architecture.py` fails if the integration imports `rscp_lib` or mentions an RSCP tag, or if the api imports Home Assistant.
- **Handler pipeline** (`e3dc_rscp_api/model/RscpHandlerPipeline.py`) routes raw RSCP frames to registered device models. Adding a new device type is a matter of implementing `RscpModelInterface` and registering it with the pipeline.
- **RSCP protocol** is provided by the [`rscp_lib`](https://pypi.org/project/rscp_lib/) PyPI package — magic `0xDCE3`, timestamp header, variable-length binary frames, Rijndael-256 CBC encryption with IV chaining.

### Repository layout

| Path | Purpose |
|------|---------|
| `custom_components/e3dc_rscp_connect/` | Integration root |
| `├─ e3dc_rscp_api/` | Communication layer (future standalone library) |
| `│  ├─ client.py` | High-level RSCP client: connect, request, dispatch |
| `│  ├─ exceptions.py` | Error hierarchy exposed to the integration |
| `│  └─ model/` | Tag handling per device type and the resulting data models |
| `├─ entities/` | Entity base class and sensor / select / number types |
| `├─ sensor.py`, `select.py`, `number.py`, `switch.py` | HA platform entry points |
| `├─ coordinator.py` | Polling coordinator |
| `└─ config_flow.py` | UI config & options flow |
| `tests/` | Unit tests (mocked, no device required) |

## Development

Home Assistant's `hassfest` validator runs automatically on pull requests via `.github/workflows/hassfest.yml` and checks `manifest.json` and the integration structure.

### Dependencies

- [`rscp_lib`](https://pypi.org/project/rscp_lib/) — RSCP protocol implementation (connection, encryption, framing, tags); pinned in `manifest.json`, installed by Home Assistant at runtime.
- `homeassistant` — provided by the Home Assistant runtime

## Contributing

Bug reports and pull requests are welcome on [GitHub](https://github.com/tobias-terhaar/e3dc_rscp_connect/issues). When adding support for a new device type, implement `RscpModelInterface` and register the handler with `RscpHandlerPipeline` — existing models in `model/` are good templates.

## Disclaimer

This integration is not affiliated with or endorsed by HagerEnergy GmbH. "E3/DC" and "S10" are trademarks of their respective owners. Use at your own risk.

## License

Released under the [MIT License](LICENSE).
