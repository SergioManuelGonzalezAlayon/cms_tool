Place the Linux Silicon Labs toolchain payload here before building the image.

Expected layout:

- `container/vendor-tools/bin/commander`
- `container/vendor-tools/bin/slc`
- `container/vendor-tools/sdk/...`
- any additional Linux-only tool payload needed by the repo scripts

Everything under `container/vendor-tools/` is copied directly into `/opt/silabs` by the
`Dockerfile` during `docker build`. The image `PATH` includes `/opt/silabs/bin`.

The container image installs the generic Linux build dependencies itself:

- `uv`
- `cmake`
- `ninja-build`
- `build-essential`

Put the vendor-specific pieces here so the image owns the full repo toolchain.
