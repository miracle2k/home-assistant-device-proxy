# Device Proxy

Stable Home Assistant devices backed by replaceable sensors and switches. Move a
smart plug between appliances while each appliance keeps its entity IDs, history,
and energy total.

Supports sensors, binary sensors, optional on/off controls, and compatible unit
conversion. Sources stay paired to their existing integration. No templates are
required.

## Install

Requires **Home Assistant 2026.7 or later**.

Add this repository to **HACS → Custom repositories** as an **Integration**, then
install Device Proxy and restart Home Assistant.

For manual installation, download the ZIP from [Releases](https://github.com/miracle2k/home-assistant-device-proxy/releases),
copy `custom_components/appliance_meter` into your configuration directory, and
restart.

## Use

1. Open **Settings → Devices & services → Add integration → Device Proxy**.
2. Name a virtual device, choose its physical source, and select its readings.
   On/off switches are optional.
3. Review the handling modes and mapping, then confirm.

Use **Configure → Create a virtual device** to add more devices, or **Reassign a
virtual device** to change a source. Mappings are remembered, but every assignment
requires review and confirmation. Each physical source can serve one virtual
device at a time.

For energy allocation, wait for the old appliance's final counter report before
reassigning. A report spanning a move cannot be split accurately. Add the virtual
energy sensors to the Energy dashboard instead of the corresponding raw plug
sensors.

See [behavior and limitations](docs/behavior.md) for counter modes, update timing,
controls, and backups.

## Development

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python scripts/build.py
```

The install ZIP is written to `dist/`. Tests use isolated Home Assistant fixtures.

[MIT license](LICENSE).
