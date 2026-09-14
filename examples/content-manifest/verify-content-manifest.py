#!/usr/bin/env python3
"""
Read a published content manifest back out of a registry, and check it is usable.

This is the consumer's half of the loop, and it deliberately does what the Automation
Portal plugins do rather than what the publisher did: discovery over the referrers API
with the sha256- tag fallback, gunzip and untar the layer, then validate the payload
against the shape `@ansible/content-model` narrows to. If this passes, the portal can
read the image; if it fails, the failure is the same one the portal would hit.

    python3 verify-content-manifest.py localhost:8080/demo/network-ee:sample --insecure

Credentials come from --username/--password or the ANSIBLE_BUILDER_REGISTRY_*
environment variables, the same as `ansible-builder publish`.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import sys
import tarfile
import time
import urllib.error
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from ansible_builder import constants                      # noqa: E402
from ansible_builder.oci import MANIFEST_ACCEPT, RegistryClient  # noqa: E402
from ansible_builder.publisher import parse_image_reference  # noqa: E402

ARTIFACT_TYPE = constants.CONTENT_MANIFEST_ARTIFACT_TYPE

PASS, FAIL, INFO = '  PASS', '  FAIL', '      '
failures: list[str] = []


def check(condition: bool, description: str, detail: str = '') -> bool:
    print(f'{PASS if condition else FAIL}  {description}{f" — {detail}" if detail else ""}')
    if not condition:
        failures.append(description)
    return condition


def heading(title: str) -> None:
    print(f'\n{title}\n{"-" * len(title)}')


def fetch_blob(client: RegistryClient, repository: str, digest: str) -> bytes:
    return client.request('GET', f'/v2/{repository}/blobs/{digest}').read()


def discover_referrer(client: RegistryClient, repository: str, digest: str):
    """
    Find the content manifest referrer, by whichever path the registry supports.

    Returns (descriptor, via). `via` matters: a registry answering the referrers API
    and one answering only the fallback tag are both correct, and a consumer has to
    handle either. Reporting which one was used is how you know the fallback is real
    rather than theoretical.
    """
    index = client.get_referrers(repository, digest)
    via = 'referrers-api'
    if index is None:
        index = client.get_manifest_json(repository, digest.replace(':', '-'))
        via = 'tag-fallback'
    if not index:
        return None, 'none'

    for descriptor in index.get('manifests', []):
        if descriptor.get('artifactType') == ARTIFACT_TYPE:
            return descriptor, via
    return None, via


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', help='Image reference, e.g. localhost:8080/demo/ee:sample')
    parser.add_argument('--insecure', action='store_true')
    parser.add_argument('--username')
    parser.add_argument('--password')
    args = parser.parse_args()

    registry, repository, reference = parse_image_reference(args.image)
    client = RegistryClient(
        registry,
        insecure=args.insecure,
        username=args.username or os.environ.get('ANSIBLE_BUILDER_REGISTRY_USERNAME'),
        password=args.password or os.environ.get('ANSIBLE_BUILDER_REGISTRY_PASSWORD'),
    )
    authenticated = client.login(repository)
    if not authenticated and client.username:
        print(f'{FAIL}  authentication failed for user {client.username!r} — '
              'the registry would not issue a token')
        print(f'{INFO}  check ANSIBLE_BUILDER_REGISTRY_PASSWORD')
        return 1

    print(f'Registry:   {registry}')
    print(f'Repository: {repository}:{reference}')
    if not client.username:
        print('Auth:       anonymous — set ANSIBLE_BUILDER_REGISTRY_USERNAME and '
              'ANSIBLE_BUILDER_REGISTRY_PASSWORD if the repository is private')

    # -- The image itself -------------------------------------------------
    heading('Image')
    started = time.monotonic()
    try:
        image_manifest = json.load(client.request(
            'GET', f'/v2/{repository}/manifests/{reference}',
            headers={'Accept': MANIFEST_ACCEPT}))
    except urllib.error.HTTPError as error:
        # Distinguishing these matters. "Not found" sends someone off to check whether
        # they pushed the image; 401 means they did, and the credentials are the
        # problem.
        if error.code in (401, 403):
            print(f'{FAIL}  cannot read the image — HTTP {error.code} {error.reason}')
            print(f'{INFO}  the repository is private and these credentials '
                  f'({client.username or "anonymous"}) cannot read it')
        elif error.code == 404:
            print(f'{FAIL}  no such image: {repository}:{reference}')
            print(f'{INFO}  check the tag exists: '
                  f'curl -s http://{registry}/v2/{repository}/tags/list')
        else:
            print(f'{FAIL}  cannot read the image — HTTP {error.code} {error.reason}')
        return 1

    if 'manifests' in image_manifest:  # multi-arch index
        image_manifest = client.get_manifest_json(
            repository, image_manifest['manifests'][0]['digest'])

    subject_digest = client.resolve_digest(repository, reference)[0]
    config = json.loads(fetch_blob(client, repository, image_manifest['config']['digest']))
    labels = config.get('config', {}).get('Labels') or {}

    check(labels.get('ansible-execution-environment') == 'true',
          'classified as an execution environment',
          'label ansible-execution-environment=true')
    check(labels.get('io.ansible.content.manifest') == 'true',
          'advertises a content manifest',
          'label io.ansible.content.manifest=true')
    check(bool(labels.get('io.ansible.content.manifest.path')),
          'records the manifest path in the image',
          labels.get('io.ansible.content.manifest.path', 'missing'))

    # -- Discovery --------------------------------------------------------
    heading('Discovery')
    descriptor, via = discover_referrer(client, repository, subject_digest)
    if not check(descriptor is not None, 'content manifest referrer found', f'via {via}'):
        return 1
    print(f'{INFO}  artifactType {descriptor["artifactType"]}')

    annotations = descriptor.get('annotations', {})
    check('io.ansible.content.collections.count' in annotations,
          'annotations carry the collection count',
          annotations.get('io.ansible.content.collections.count', 'missing'))
    check('io.ansible.content.ansible_core' in annotations,
          'annotations carry the generating ansible-core',
          annotations.get('io.ansible.content.ansible_core', 'missing'))

    # -- The payload ------------------------------------------------------
    heading('Payload')
    referrer = client.get_manifest_json(repository, descriptor['digest'])
    layer = referrer['layers'][0]
    blob = fetch_blob(client, repository, layer['digest'])
    elapsed_ms = (time.monotonic() - started) * 1000

    check(layer['mediaType'].startswith('application/vnd.oci.image.layer'),
          'layer uses a portable media type', layer['mediaType'])

    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(blob))) as archive:
        member = archive.next()
        content = json.load(archive.extractfile(member))

    check(isinstance(content.get('schemaVersion'), str)
          and isinstance(content.get('collections'), list)
          and isinstance(content.get('generatedBy'), dict),
          'payload matches the content-model contract',
          f'schemaVersion {content.get("schemaVersion")}')

    generated_by = content.get('generatedBy', {})
    check(bool(generated_by.get('ansibleCore')),
          'records the ansible-core that performed enumeration',
          generated_by.get('ansibleCore', 'missing'))
    print(f'{INFO}  generated by {generated_by.get("tool")}, '
          f'docs={generated_by.get("docsMode")} via {generated_by.get("docsSource")}')
    print(f'{INFO}  complete={content.get("complete")}, '
          f'{len(blob)} bytes gzipped, read in {elapsed_ms:.0f} ms')

    for diagnostic in content.get('diagnostics') or []:
        print(f'{INFO}  [{diagnostic.get("level")}] {diagnostic.get("message")}')

    # -- Contents ---------------------------------------------------------
    heading('Contents')
    collections = content.get('collections', [])
    check(len(collections) >= 2, 'contains the expected collections',
          f'{len(collections)} found')

    total_plugins = 0
    documented = 0
    with_suboptions = None
    with_examples = 0
    totals: Counter = Counter()

    for collection in collections:
        plugins = collection.get('plugins', [])
        total_plugins += len(plugins)
        kinds = Counter(p.get('type') for p in plugins)
        fqcn = f'{collection.get("namespace")}.{collection.get("name")}'

        # Level B content is not only plugins. EDA plugins and rulebooks in particular
        # are worth naming: ansible-doc cannot describe them at all, so their presence
        # is evidence the generator parsed the DOCUMENTATION literal from the AST
        # rather than merely shelling out.
        other = {kind: len(collection.get(key) or [])
                 for kind, key in (('roles', 'roles'), ('playbooks', 'playbooks'),
                                   ('eda', 'edaPlugins'), ('rulebooks', 'rulebooks'))}
        totals.update({'plugins': len(plugins)})
        totals.update({k: v for k, v in other.items() if v})
        extra = ' '.join(f'{k}={v}' for k, v in other.items() if v)

        print(f'{INFO}  {fqcn}:{collection.get("version")}  plugins={len(plugins)}'
              f'{" " + extra if extra else ""}')
        print(f'{INFO}      {", ".join(f"{k}={v}" for k, v in sorted(kinds.items()))}')

        for plugin in plugins:
            options = plugin.get('options') or {}
            if plugin.get('shortDescription') and options:
                documented += 1
            if plugin.get('examples'):
                with_examples += 1
            if with_suboptions is None:
                for name, option in options.items():
                    if option.get('suboptions'):
                        with_suboptions = (plugin, name, option)
                        break

    print(f'{INFO}  totals: '
          + ', '.join(f'{k}={v}' for k, v in sorted(totals.items())))
    check(total_plugins > 0, 'plugins were enumerated', f'{total_plugins} total')
    check(documented > 0, 'plugins carry documentation and argument specs',
          f'{documented} of {total_plugins} have a description and options')
    check(with_examples > 0, 'plugins carry examples', f'{with_examples} of {total_plugins}')

    # -- Depth ------------------------------------------------------------
    heading('Documentation depth')
    if check(with_suboptions is not None,
             'nested suboptions survived extraction',
             'this is what separates real extraction from a name listing'):
        plugin, option_name, option = with_suboptions
        subs = sorted(option['suboptions'])
        print(f'{INFO}  {plugin["fqcn"]} → {option_name}.suboptions = '
              f'{", ".join(subs[:8])}{" …" if len(subs) > 8 else ""}')
        first = option['suboptions'][subs[0]]
        check(bool(first.get('description') or first.get('type')),
              'suboptions are themselves described',
              f'{subs[0]}: type={first.get("type")}')

    heading('Result')
    if failures:
        print(f'  {len(failures)} check(s) failed:')
        for failure in failures:
            print(f'    - {failure}')
        return 1

    print('  All checks passed. The portal can read this image from the registry '
          'without pulling it.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except urllib.error.HTTPError as error:
        print(f'Registry error: HTTP {error.code} {error.reason}', file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as error:
        print(f'Cannot reach the registry: {error.reason}', file=sys.stderr)
        sys.exit(1)
