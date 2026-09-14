# Content manifest

An execution environment is a container image of several hundred megabytes to a few
gigabytes. Answering "what is inside it" — which collections, which modules and plugins,
with documentation good enough to search — should not require pulling it.

Ansible Builder solves that by generating an inventory **at build time, inside the
image, using that image's own `ansible-core`**, and publishing it to the registry as a
separate OCI artifact that refers to the image. A consumer then reads the inventory in a
few HTTP calls.

Measured against a live Quay: the complete inventory of a 563 MB image — 5 collections,
200 plugins, 19 EDA plugins, 15 rulebooks, with nested suboptions and examples — reads
back in about **80 ms**. Generation costs about **3.6 s**, once, at build time.

## Why inside the image

This is a correctness property, not a performance one.

An execution environment ships a specific `ansible-core`, and collections are authored
against it. Plugin loading, argument-spec handling and documentation-fragment resolution
all vary between versions. Enumerating the same image from outside with a *different*
`ansible-core` can produce results that are subtly wrong rather than obviously missing —
and a wrong answer presented confidently is worse than no answer.

The manifest records `generatedBy.ansibleCore`, so a consumer can always tell build-time
extraction from external introspection.

## Generating

On by default. Every image built with schema version 3 gets a manifest at
`/usr/share/ansible/content-manifest.json` unless the definition opts out:

```yaml
version: 3

options:
  content_manifest:
    enabled: true        # default
    docs: full           # full | summary | none
    path: /usr/share/ansible/content-manifest.json
```

The generation step runs in the final stage, after `append_final` so that content added
by custom build steps is enumerated too.

Two image labels are added so a consumer can tell from the config blob alone — without
fetching anything else — that a manifest is present:

```
io.ansible.content.manifest=true
io.ansible.content.manifest.path=/usr/share/ansible/content-manifest.json
```

Collection counts and the `ansible-core` version are deliberately *not* labels. A `LABEL`
directive is fixed when the Containerfile is written, and the manifest does not exist
until the build runs. Those facts travel in the referrer's annotations instead, which is
where a consumer reads them anyway.

## Publishing

```bash
ansible-builder publish quay.io/myorg/my-ee:latest
```

Pushes the image, then publishes the manifest as an OCI referrer whose `subject` is the
image manifest. `--skip-image-push` publishes only the manifest, for an image already in
the registry. See `docs/usage.rst` for the full flag list.

### What it works around

Three behaviours, each found by a failed push against a stock Quay rather than by
reading the specification. They are not hypothetical, and they are why the wire format
looks the way it does:

| Registry behaviour | Consequence |
|---|---|
| Custom artifact and layer media types are rejected — including `application/vnd.oci.empty.v1+json` as a config | The payload travels as a standard gzipped tar layer, with the Ansible type on `artifactType` and in annotations |
| The referrers API returns 404 | A fallback tag (`sha256-<hex>`) is always published alongside, holding an image **index listing referrer descriptors** — not the referrer manifest itself |
| Index descriptors require `platform`, though the OCI spec makes it optional | Referrer descriptors carry `{"architecture": "unknown", "os": "unknown"}` |

Any one of these breaks a publisher that assumes OCI 1.1 conformance.

## Content coverage

- **Collections** — version and metadata
- **Plugins** — every `ansible-core` plugin type, with fragment-resolved documentation:
  filters, lookups, tests, callbacks, connections, inventory, the networking plugins
  (`cliconf`, `netconf`, `httpapi`), `become`, cache, shell, strategy, vars and terminal
- **Roles** — including `argument_spec` entry points
- **Playbooks** shipped in collections
- **Event-Driven Ansible** — `event_source` and `event_filter` plugins, and rulebooks.
  `ansible-doc` cannot describe these at all; they are extracted by parsing the
  module-level `DOCUMENTATION` literal from the AST.

## Running the generator directly

The generator is a standalone script and can be run inside any image that has one:

```bash
python3 content_manifest.py generate --output /path/to/manifest.json --docs full --quiet
python3 content_manifest.py labels --manifest /path/to/manifest.json
```

| Option | Default |
|---|---|
| `--output` | `/usr/share/ansible/content-manifest.json` |
| `--collections-path` | `/usr/share/ansible/collections` |
| `--docs` | `full` (`summary`, `none`) |
| `--quiet` | off |
