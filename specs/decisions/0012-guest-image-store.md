# 0012. Guest image store

Prebuilt MicroVM guests are files in an ordinary store, not an OCI
registry. A worker pulls them before it starts. The same layout works
on `s3://`, `https://`, and `file://`.

## Why not OCI or squashfs

OCI and ORAS add a registry, manifests, and a client we do not need.
Operators already have a bucket or a static HTTPS prefix. squashfs plus
an overlay would change how the guest disk is attached. The guest still
boots one ext4 rootfs, read-only, as today. Signing beyond sha256 can
follow later. Neither change belongs in this format.

## Artifacts

One image version and arch is a zstd-compressed ext4 (`compression` is
`zstd`) plus a `manifest.json`. The uncompressed ext4 is what Firecracker
attaches. One kernel per arch is shared by every image: `vmlinux.zst`,
with its sha256 and version in the index. The kernel version is the
first 12 hex digits of the uncompressed `vmlinux` sha256.

Paths in the index and in manifests are relative to the index file.
They must not be absolute and must not contain `..`. Flat names are the
recommended layout so a GitHub release (no directories) and a nested S3
prefix both work:

- `<id>-<version>-<arch>.ext4.zst`
- `<id>-<version>-<arch>.json`
- `vmlinux-<arch>.zst`
- `index.json`

Schema `2` is a versioned store: one immutable `v<version>/` prefix,
no `latest`, and `manifest_sha256`. Schema `1` is the old flat store.
It is still read, with a deprecation warning. The official store is
the GitHub release at
`https://github.com/GEKI-AI/apipi/releases/download/v<version>/`.
Signing is a Sigstore bundle over `SHA256SUMS`. An OCI registry was
considered and not used, because this layout stays on the existing
HTTPS, S3, and file client.

Schema is `2` for image manifests. Schema `1` manifests are rejected.
Any other schema fails with a clear error. Unknown JSON
keys fail the same way.

## Manifest

`sha256` is the digest of the uncompressed ext4. Workers check that
digest when they pull. Placement matches a worker by image id, not by
digest. `compressed_sha256` is checked while downloading.
`size` and `compressed_size` are byte lengths.

The image version names the inputs, not the bytes. Two builds of the
same inputs may differ (apk mirrors, timestamps). The digest names the
bytes.

```
version = <pi_version>-<8 hex>
```

The 8 hex digits are the leading digits of
`sha256(base + "\n" + node_version + "\n" + guest_sh_sha256 + "\n" + recipe_sha256 + "\n" + agent_browser + "\n" + chrome + "\n")`.
`base` is the digest-pinned Debian trixie slim image. Alpine is no
longer a base. A Node pin change, an agent-browser pin change, or a
Chrome pin change also produces a new version.
`guest_sh_sha256` is the sha256 of `src/apipi/worker/pi/guest.sh`.
`recipe_sha256` hashes `images/build.sh` and the files in
`images/<id>/`, sorted by path relative to `images/`. Each file is
`relative_path`, a NUL, the bytes, and a NUL. A Pi pin change, a
`guest.sh` change, a shared build script change, or a recipe change
produces a new version. No manual bump.

The kernel is not part of that hash. A kernel URL change lives in
`images/build.sh`, so it does bump every image version. A byte change
at the same URL does not. Republishing that case needs `--force`. The
manifest still records the kernel sha256 the build used.

`min_apipi_version` is the ApiPi version that built the image, unless
the recipe sets a higher floor. Pull refuses a manifest whose
`min_apipi_version` is newer than the running ApiPi. Versions are
dotted integers (`0.4.0`). `pi_version` must equal this process's
`PINNED_PI`.

`min_size` comes from the recipe (`S`, `M`, or `L`). It is the smallest
sandbox size the image is meant for. It is not a RAM number.
`min_vcpus` is the recipe floor. Spawn uses the larger of the size
vCPU count and that floor. `node_version` is the pinned Node tarball.
`agent_browser` and `chrome` are set on the browser image and empty
on the others. A recipe may set `ARCHS`. An image that was not built
for the worker arch is `image_unavailable`, not a generic pull error.

## Index

`index.json` lists kernels and images. More than one version of an id
may be listed. Exactly one entry per `(id, arch)` has `latest: true`.
Pull uses that latest entry, then checks the manifest. If it is not
compatible, pull fails. It does not silently pick an older version.

`apipi images push` writes new objects first and rewrites `index.json`
last. `apipi images publish` is the same command. For each image id
and arch it uploads only the newest local build, chosen by manifest
`created_at`. Older manifests left in the build directory are ignored.
If that exact `(id, version, arch)` is already in the store and the
compressed sha256 matches, push skips it and exits 0. `--force`
uploads again. The pushed version becomes `latest` for that id and
arch. `--to` defaults to `APIPI_IMAGE_SOURCE`. A missing bucket fails
with a clear error instead of starting a new index. A missing
`index.json` in an existing bucket still starts a new index.

Pull streams the compressed blob to a temp file, checks
`compressed_sha256`, decompresses as a stream, checks `sha256`, then
renames the ext4 into place. Peak RAM does not grow with image size.
The kernel download uses the same path.

## Local layout

```
<images_dir>/
  kernels/<arch>/vmlinux
  kernels/<arch>/vmlinux.json
  <id>/<version>/rootfs.ext4
  <id>/<version>/manifest.json
  <id>/current
```

`<id>/current` is a one-line file with the version, not a symlink. A
symlink breaks some copy tools. Writers replace it by writing a temp
file and renaming it.

## Image store S3

The image URI names the bucket and prefix. Endpoint, region, and
addressing come from `APIPI_IMAGE_S3_ENDPOINT`,
`APIPI_IMAGE_S3_REGION`, and `APIPI_IMAGE_S3_ADDRESSING` when set.
Otherwise they fall back to `APIPI_S3_ENDPOINT`, `APIPI_S3_REGION`,
and `APIPI_S3_ADDRESSING`. Credentials are
`APIPI_IMAGE_S3_ACCESS_KEY_ID` and
`APIPI_IMAGE_S3_SECRET_ACCESS_KEY`, or `APIPI_IMAGE_S3_PROFILE`.
If none of those is set, the process uses the standard AWS credential
chain. Secrets are not read from TOML. The artifact store is unchanged.
