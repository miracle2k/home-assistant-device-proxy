"""Exercise the same filtered subscription as the Integrations page."""

from homeassistant.components.config import config_entries as config_entries_api


async def test_integration_page_lists_proxy_with_configure(hass, entry, hass_ws_client):
    client = await hass_ws_client(hass)
    config_entries_api.async_setup(hass)
    await client.send_json_auto_id(
        {
            "type": "config_entries/subscribe",
            "type_filter": ["device", "hub", "service", "hardware"],
        }
    )
    result = await client.receive_json()
    assert result["success"]
    event = await client.receive_json()
    visible = [
        item["entry"]
        for item in event["event"]
        if item["entry"]["entry_id"] == entry.entry_id
    ]
    assert len(visible) == 1
    assert visible[0]["title"] == "Device Proxy"
    assert visible[0]["state"] == "loaded"
    assert visible[0]["supports_options"] is True

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert "create" in flow["menu_options"]
    assert "reassign" in flow["menu_options"]
    hass.config_entries.options.async_abort(flow["flow_id"])
