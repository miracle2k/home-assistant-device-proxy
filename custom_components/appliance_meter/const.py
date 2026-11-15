"""Constants for Device Proxy (the domain is retained for upgrades)."""

DOMAIN = "appliance_meter"
NAME = "Device Proxy"
STORAGE_VERSION = 2
SAVE_INTERVAL = 5
MIRROR = "mirror"
INCREASE = "increase"
NET = "net"
MODES = {
    MIRROR: "Mirror the current reading",
    INCREASE: "Allocate counter increases (resets allowed)",
    NET: "Allocate signed counter changes",
}
UNMAPPED = "__unmapped__"
