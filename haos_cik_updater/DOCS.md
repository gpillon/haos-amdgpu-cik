# HAOS AMDGPU CIK Updater

This app checks `https://gpillon.github.io/haos-amdgpu-cik/stable.json`
and installs the signed RAUC bundle through the host RAUC D-Bus service. The
app first downloads the bundle to the shared data directory and verifies its
SHA-256 checksum, then gives RAUC the corresponding host-local path. This
avoids RAUC's small built-in limit for direct HTTP downloads.

It is only useful after booting a gpillon HAOS AMDGPU CIK image. An official
HAOS installation does not trust the custom signing certificate and cannot use
this app as the initial migration mechanism.

Automatic installation and automatic reboot are disabled by default. Open the
app from its ingress panel, review the installed and available versions, and
select **Installa aggiornamento**. Unless `reboot_after_install` is enabled,
reboot Home Assistant from the normal UI after RAUC finishes.

The installed version is read from the booted RAUC slot. The Supervisor API is
used when its token is available, but it is not required for update checks.
