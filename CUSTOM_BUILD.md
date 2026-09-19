# Home Assistant OS with AMDGPU CIK

This fork produces an unofficial `generic-x86-64` Home Assistant OS image with:

```text
CONFIG_DRM_AMDGPU=m
CONFIG_DRM_AMDGPU_CIK=y
```

The `Weekly AMDGPU CIK build` workflow checks every day at 03:41 UTC and can
also be started manually. It keeps the fork's maintenance branch synchronized
with upstream and resolves Home Assistant OS's latest non-prerelease release.
If that upstream stable release is already present in `weekly-latest`, the run
stops without compiling. Otherwise it checks out the stable tag directly from
`home-assistant/operating-system` and applies only
`CONFIG_DRM_AMDGPU_CIK=y` in the runner workspace. It then builds only
`generic-x86-64`, verifies the final Linux `.config`, signs the image with a
persistent private RAUC identity, and updates the rolling release. A manual run
can set `force_rebuild` when the same upstream version must be rebuilt.

After a successful release the workflow publishes a Supervisor-compatible
manifest at:

- `https://gpillon.github.io/haos-amdgpu-cik/stable.json`

The manifest is never advanced before the signed RAUC bundle and its checksums
are available. The post-install updater consumes the `custom_haos` metadata and
invokes the host RAUC service; the official Supervisor update endpoint remains
unchanged.

Stable download URLs:

- `https://github.com/gpillon/haos-amdgpu-cik/releases/latest/download/haos_generic-x86-64-amdgpu-cik.raucb`
- `https://github.com/gpillon/haos-amdgpu-cik/releases/latest/download/haos_generic-x86-64-amdgpu-cik.img.xz`
- `https://github.com/gpillon/haos-amdgpu-cik/releases/latest/download/SHA256SUMS`

The `.img.xz` image is required for the first installation because it embeds
the public half of the custom RAUC signing identity. Later `.raucb` updates must
be signed by the same private key.

## Security and compatibility

The private key is stored as the `RAUC_PRIVATE_KEY_PEM` GitHub Actions secret.
Do not commit or publish it. Keep the local backup in `rauc-signing/key.pem`
secure: losing both the backup and the GitHub secret means future compatible
updates can no longer be signed.

This is not an official Home Assistant build. Home Assistant OS previously
disabled AMDGPU SI/CIK support after boot crashes on some AMD systems. Test the
initial image and retain a recoverable backup before relying on it.

## Maintenance

The kernel configuration is not committed to the fork. Each run starts from the
latest stable upstream tag and reapplies only the CIK setting. If upstream
removes or renames the AMDGPU configuration anchor, the workflow stops before
building instead of changing unrelated kernel options.
