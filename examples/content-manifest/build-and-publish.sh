#!/bin/bash
# Build the sample EE, publish it with its content manifest, and verify a consumer
# can read the contents back out of the registry without pulling the image.
#
#   export ANSIBLE_BUILDER_REGISTRY_USERNAME=demo
#   export ANSIBLE_BUILDER_REGISTRY_PASSWORD=...   # context/cursor/poc/e2e/.env
#   ./build-and-publish.sh
#
# Env:
#   REGISTRY    default 127.0.0.1:8080   (the PoC Quay: cd context/cursor/poc/e2e && make up)
#   REPOSITORY  default demo/network-ee
#   TAG         default sample
#   INSECURE    default 1                (HTTP, no TLS verification — local registries only)
set -euo pipefail

REGISTRY="${REGISTRY:-127.0.0.1:8080}"
REPOSITORY="${REPOSITORY:-demo/network-ee}"
TAG="${TAG:-sample}"
INSECURE="${INSECURE:-1}"
RUNTIME="${CONTAINER_RUNTIME:-podman}"
IMAGE="${REGISTRY}/${REPOSITORY}:${TAG}"
HERE="$(cd "$(dirname "$0")" && pwd)"

insecure_flag=()
[ "$INSECURE" = "1" ] && insecure_flag=(--insecure)

for tool in "$RUNTIME" ansible-builder python3; do
  command -v "$tool" >/dev/null || { echo "ERROR: $tool is required but not installed"; exit 1; }
done

if ! curl -sf "http://${REGISTRY}/v2/" -o /dev/null && \
   ! curl -sf "https://${REGISTRY}/v2/" -o /dev/null -k; then
  echo "WARNING: no registry answering at ${REGISTRY}."
  echo "         Start the PoC Quay with: (cd context/cursor/poc/e2e && make up)"
fi

echo "=== 1/3  Build ==============================================="
# Generation is on by default, so this needs no extra flags: the manifest is written
# to /usr/share/ansible/content-manifest.json inside the image, by the image's own
# ansible-core.
ansible-builder build \
  -f "${HERE}/execution-environment.yml" \
  -c "${HERE}/context" \
  -t "$IMAGE" \
  --container-runtime "$RUNTIME" \
  -v 3

echo
echo "--- manifest generated inside the image:"
cid="$("$RUNTIME" create "$IMAGE")"
trap '"$RUNTIME" rm -f "$cid" >/dev/null 2>&1 || true' EXIT
"$RUNTIME" cp "${cid}:/usr/share/ansible/content-manifest.json" "${HERE}/content-manifest.json"
python3 - "${HERE}/content-manifest.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
print(f"    ansible-core {m['generatedBy'].get('ansibleCore')}, "
      f"docs={m['generatedBy'].get('docsMode')}, complete={m.get('complete')}")
for c in m['collections']:
    print(f"    {c['namespace']}.{c['name']}:{c['version']}  "
          f"plugins={len(c['plugins'])} roles={len(c['roles'])} "
          f"eda={len(c['edaPlugins'])} rulebooks={len(c['rulebooks'])}")
PY

echo
echo "=== 2/3  Publish ============================================="
ansible-builder publish "$IMAGE" "${insecure_flag[@]}" -v 3

echo
echo "=== 3/3  Verify from the registry ============================"
python3 "${HERE}/verify-content-manifest.py" "$IMAGE" "${insecure_flag[@]}"
