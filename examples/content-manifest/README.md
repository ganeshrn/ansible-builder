# Sample: an EE with a published content manifest

Builds an execution environment containing **ansible.utils** and **ansible.netcommon**,
generates the content inventory inside the image using that image's own `ansible-core`,
publishes it to a registry as an OCI referrer, and then checks that a consumer can read
the full inventory back **without pulling the image**.

| File | What |
|---|---|
| `execution-environment.yml` | The EE definition |
| `build-and-publish.sh` | Build → publish → verify, in one command |
| `verify-content-manifest.py` | The consumer's half: reads the manifest out of the registry and validates it |

## Run it

The PoC Quay is the intended target:

```bash
cd ../../../../context/cursor/poc/e2e && make up    # Quay on 127.0.0.1:8080
```

Then, from this directory:

```bash
export ANSIBLE_BUILDER_REGISTRY_USERNAME=demo
export ANSIBLE_BUILDER_REGISTRY_PASSWORD=...        # context/cursor/poc/e2e/.env
./build-and-publish.sh
```

Against a real registry instead:

```bash
REGISTRY=quay.io REPOSITORY=myorg/network-ee TAG=sample INSECURE=0 ./build-and-publish.sh
```

The build takes several minutes; generation of the manifest itself takes a few seconds,
and publishing and reading it back take under a second.

## What the verification proves

`verify-content-manifest.py` deliberately does what the Automation Portal plugins do,
not what the publisher just did — so a pass means the portal can read this image, and a
failure is the same failure the portal would hit.

- **Classification.** The image carries `ansible-execution-environment=true` and
  `io.ansible.content.manifest=true`, so a consumer can tell from the config blob alone
  — without fetching anything else — that an inventory exists.
- **Discovery, by either path.** It tries the referrers API, falls back to the spec's
  `sha256-<hex>` tag, and reports which one answered. Both are correct; a consumer has
  to handle either, and reporting it is how you know the fallback is real rather than
  theoretical.
- **A portable wire format.** The layer is a standard `tar+gzip`, because custom layer
  media types are not portable — Quay rejects them. Identity travels on `artifactType`.
- **The contract.** The payload has the `schemaVersion`, `collections` and `generatedBy`
  that `@ansible/content-model` narrows to.
- **Fidelity.** `generatedBy.ansibleCore` is present, so a consumer can distinguish
  build-time extraction from external introspection. The two are not equivalent.
- **Depth.** Not just plugin *names*: descriptions, argument specs, examples, and
  **nested suboptions**. That last one is what separates real extraction from a
  directory listing, and it is checked explicitly.

## Notes on the definition

Two choices in `execution-environment.yml` exist because the obvious thing fails:

- **`quay.io/fedora/fedora:41` as the base**, not `quay.io/ansible/ansible-runner:latest`
  — which is four years old and whose `ansible-galaxy` fails against the Galaxy v3 API.
  `community-ee-base` is not publicly pullable.
- **`ansible-core` from the Fedora RPM**, not pip. The `cryptography` aarch64 wheel uses
  instructions above the podman machine's CPU baseline, so `pip install ansible-core`
  dies with `SIGILL` (rc=132) on Apple silicon. `ansible-runner` is pure Python and
  installs from pip safely — and it must be present, or the final `check_ansible` step
  fails the build.

The `options.content_manifest` block is stated explicitly, but every value in it is the
default. An EE definition that says nothing gets a full manifest.
