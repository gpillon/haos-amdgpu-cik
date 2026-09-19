# HAOS AMDGPU CIK Updater

This app checks `https://gpillon.github.io/haos-amdgpu-cik/stable.json`
and installs the signed RAUC bundle through the host RAUC D-Bus service.

It is only useful after booting a gpillon HAOS AMDGPU CIK image. An official
HAOS installation does not trust the custom signing certificate and cannot use
this app as the initial migration mechanism.

Automatic installation and automatic reboot are disabled by default. Open the
app from its ingress panel, review the installed and available versions, and
select **Install signed update**. Unless `reboot_after_install` is enabled,
reboot Home Assistant from the normal UI after RAUC finishes.
