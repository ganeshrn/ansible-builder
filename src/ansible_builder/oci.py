"""
Publish an Ansible content manifest to an OCI registry as a referrer of an image.

This is the publish half of build-time content discovery. ``content_manifest.py``
generates the inventory inside the execution environment during the build; this pushes
it alongside the image as a separate OCI artifact whose ``subject`` is the image
manifest.

The point is what it costs a consumer. A catalog that wants to know what is inside an
execution environment can read this artifact in a few HTTP calls — get referrers, fetch
one small blob — instead of pulling gigabytes of image. Measured against a live Quay,
the complete inventory of a 563 MB image reads back in around 80 ms.

Nothing here is Ansible-specific beyond the artifact type: it is the OCI Distribution v2
API and the referrers convention, implemented with the standard library only so that
publishing adds no dependency to ansible-builder.

Three registry behaviours are worked around, each found by a failed push against a stock
Quay rather than by reading the specification. They are documented at the point they
are handled, because a future reader will otherwise remove them as unnecessary.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import logging
import ssl
import tarfile
import urllib.error
import urllib.parse
import urllib.request

from . import constants
from .exceptions import DefinitionError

logger = logging.getLogger(__name__)

OCI_MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
OCI_INDEX = 'application/vnd.oci.image.index.v1+json'
DOCKER_MANIFEST = 'application/vnd.docker.distribution.manifest.v2+json'
DOCKER_MANIFEST_LIST = 'application/vnd.docker.distribution.manifest.list.v2+json'

MANIFEST_ACCEPT = ', '.join(
    [OCI_MANIFEST, OCI_INDEX, DOCKER_MANIFEST, DOCKER_MANIFEST_LIST])

# An artifact with no meaningful config. OCI 1.1 defines
# application/vnd.oci.empty.v1+json for exactly this, but registries predating 1.1
# reject it: Quay validates config mediaType against a fixed allowlist which does not
# include it. vnd.unknown.config.v1+json is the widely accepted escape hatch.
EMPTY_JSON = b'{}'
FALLBACK_CONFIG_MEDIA_TYPE = 'application/vnd.unknown.config.v1+json'

# Standard OCI layer type, accepted everywhere. The Ansible-specific type travels on
# `artifactType` and in annotations instead of on the layer, because custom layer media
# types are not portable — a design that assumes otherwise works on Zot and fails on
# Quay.
TAR_GZIP_MEDIA_TYPE = 'application/vnd.oci.image.layer.v1.tar+gzip'


def digest_of(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def tar_gz(files: dict[str, bytes]) -> bytes:
    """
    Package files into a gzipped tar, as ORAS does when pushing plain files.

    Byte-for-byte reproducible, which matters more than it looks: a blob's name in the
    registry *is* the hash of these bytes. If packing the same manifest twice produced
    different bytes it would produce a different digest, the existence check before
    upload would always miss, and every publish would leave another orphaned copy of
    the same content in the registry.

    Two clocks have to be pinned to get that. ``TarInfo.mtime`` covers the tar entry and
    ``GzipFile(mtime=...)`` covers the gzip header — the latter defaults to "now", so
    omitting it silently defeats the whole thing.
    """
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode='w') as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))

    gz_buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=gz_buffer, mode='wb', mtime=0) as compressor:
        compressor.write(tar_buffer.getvalue())
    return gz_buffer.getvalue()


class RegistryClient:
    """A minimal OCI Distribution v2 client: enough to push an artifact, no more."""

    def __init__(self, registry: str, insecure: bool = False,
                 username: str | None = None, password: str | None = None) -> None:
        self.registry = registry
        self.scheme = 'http' if insecure else 'https'
        self.username = username
        self.password = password
        self._token: str | None = None
        self._ctx = ssl.create_default_context()
        if insecure:
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    def _basic(self) -> str | None:
        if not self.username:
            return None
        raw = f'{self.username}:{self.password or ""}'.encode()
        return 'Basic ' + base64.b64encode(raw).decode()

    def _auth_header(self) -> str | None:
        if self._token:
            return f'Bearer {self._token}'
        return self._basic()

    def login(self, repository: str) -> bool:
        """
        Acquire a repository-scoped token before doing any real work.

        Necessary because registries are inconsistent about where they advertise the
        auth challenge. Quay returns 401 with a ``WWW-Authenticate`` header on ``/v2/``
        but a bare 401 with no challenge on repository paths, so a client that waits to
        be challenged on its first real request never learns where the token endpoint
        is. Priming from ``/v2/`` is what podman does, and it works everywhere.

        The priming request is sent **anonymously**, on purpose. Quay's ``/v2/`` accepts
        Bearer tokens only: presenting Basic credentials there earns
        ``400 Invalid bearer token format`` rather than a challenge, and a 400 carries
        no ``WWW-Authenticate`` header to learn from — so sending credentials eagerly is
        what prevents the client from ever discovering where to exchange them.
        """
        try:
            self.request('GET', '/v2/', _retry=False, _auth=False)
            return True  # anonymous access is permitted
        except urllib.error.HTTPError as error:
            challenge = error.headers.get('www-authenticate', '')
            if error.code != 401 and not challenge:
                raise

        if not challenge:
            return False
        return self._handle_challenge(
            challenge, scope=f'repository:{repository}:pull,push')

    def _handle_challenge(self, challenge: str, scope: str | None = None) -> bool:
        """Satisfy a Bearer challenge via the token-exchange flow."""
        if not challenge.lower().startswith('bearer '):
            return False

        params: dict[str, str] = {}
        for part in challenge[7:].split(','):
            if '=' in part:
                key, _, value = part.partition('=')
                params[key.strip()] = value.strip().strip('"')

        realm = params.get('realm')
        if not realm:
            return False
        if scope:
            params['scope'] = scope

        query = {k: v for k, v in params.items() if k in ('service', 'scope')}
        url = realm + ('?' + urllib.parse.urlencode(query) if query else '')
        request = urllib.request.Request(url, headers={'Accept': 'application/json'})
        if basic := self._basic():
            request.add_header('Authorization', basic)

        try:
            with urllib.request.urlopen(request, context=self._ctx, timeout=60) as res:
                body = json.load(res)
        except urllib.error.URLError:
            return False

        self._token = body.get('token') or body.get('access_token')
        return bool(self._token)

    def request(self, method: str, path: str, body: bytes | None = None,
                headers: dict[str, str] | None = None, _retry: bool = True,
                _auth: bool = True):
        url = path if path.startswith('http') else f'{self.scheme}://{self.registry}{path}'
        request = urllib.request.Request(url, data=body, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        if _auth and (auth := self._auth_header()):
            request.add_header('Authorization', auth)

        try:
            return urllib.request.urlopen(request, context=self._ctx, timeout=300)
        except urllib.error.HTTPError as error:
            if error.code == 401 and _retry:
                challenge = error.headers.get('www-authenticate', '')
                if challenge and self._handle_challenge(challenge):
                    return self.request(method, path, body, headers, _retry=False)
            raise

    # -- Distribution v2 operations ---------------------------------------

    def blob_exists(self, repository: str, digest: str) -> bool:
        try:
            self.request('HEAD', f'/v2/{repository}/blobs/{digest}')
            return True
        except urllib.error.HTTPError:
            return False

    def push_blob(self, repository: str, data: bytes) -> str:
        """Upload a blob using the two-step POST-then-PUT flow."""
        digest = digest_of(data)
        if self.blob_exists(repository, digest):
            return digest

        response = self.request('POST', f'/v2/{repository}/blobs/uploads/',
                                headers={'Content-Length': '0'})
        location = response.headers.get('location')
        if not location:
            raise DefinitionError('Registry did not return a blob upload location.')
        if location.startswith('/'):
            location = f'{self.scheme}://{self.registry}{location}'

        separator = '&' if '?' in location else '?'
        self.request('PUT', f'{location}{separator}digest={digest}', body=data,
                     headers={'Content-Type': 'application/octet-stream',
                              'Content-Length': str(len(data))})
        return digest

    def resolve_digest(self, repository: str, reference: str) -> tuple[str, int, str]:
        """Return (digest, size, mediaType) for a tag or digest reference."""
        response = self.request('GET', f'/v2/{repository}/manifests/{reference}',
                                headers={'Accept': MANIFEST_ACCEPT})
        raw = response.read()
        digest = response.headers.get('docker-content-digest') or digest_of(raw)
        media_type = response.headers.get('content-type') or OCI_MANIFEST
        return digest, len(raw), media_type

    def put_manifest(self, repository: str, reference: str, manifest: dict,
                     media_type: str = OCI_MANIFEST) -> str:
        data = json.dumps(manifest, separators=(',', ':')).encode()
        digest = digest_of(data)
        self.request('PUT', f'/v2/{repository}/manifests/{reference}', body=data,
                     headers={'Content-Type': media_type,
                              'Content-Length': str(len(data))})
        return digest

    def get_manifest_json(self, repository: str, reference: str) -> dict | None:
        """Fetch a manifest, or None when absent. Used to merge into an existing index."""
        try:
            response = self.request('GET', f'/v2/{repository}/manifests/{reference}',
                                    headers={'Accept': MANIFEST_ACCEPT})
            return json.load(response)
        except (urllib.error.HTTPError, ValueError):
            return None

    def get_referrers(self, repository: str, digest: str) -> dict | None:
        try:
            response = self.request('GET', f'/v2/{repository}/referrers/{digest}',
                                    headers={'Accept': OCI_INDEX})
            return json.load(response)
        except urllib.error.HTTPError:
            return None


def _annotations(content: dict) -> dict[str, str]:
    """
    Facts a consumer can read without fetching the manifest blob.

    This is where the counts and the ansible-core version live. They cannot be image
    labels, because a LABEL is fixed when the Containerfile is written and the manifest
    does not exist until the build runs.
    """
    generated_by = content.get('generatedBy', {})
    annotations = {
        'org.opencontainers.image.created': content.get('generatedAt', ''),
        'org.opencontainers.artifact.description':
            'Ansible content manifest: collections, plugins, roles, EDA plugins',
        'io.ansible.content.collections.count': str(len(content.get('collections', []))),
        'io.ansible.content.manifest.schema': content.get('schemaVersion', ''),
        'io.ansible.content.manifest.artifact_type': constants.CONTENT_MANIFEST_ARTIFACT_TYPE,
    }
    if ansible_core := generated_by.get('ansibleCore'):
        annotations['io.ansible.content.ansible_core'] = ansible_core
    return annotations


def publish_content_manifest(client: RegistryClient, repository: str, reference: str,
                             payload: bytes) -> dict:
    """
    Publish a content manifest as a referrer of the image at ``reference``.

    Returns a summary dict describing what was published and how a consumer will
    discover it.
    """
    content = json.loads(payload)

    client.login(repository)
    subject_digest, subject_size, subject_media = client.resolve_digest(
        repository, reference)
    logger.debug('Subject image digest: %s', subject_digest)

    # The payload travels as a standard gzipped tar layer, the convention ORAS uses.
    #
    # Not cosmetic: registries vary in how strictly they police media types. Quay
    # validates both the config and the layer mediaType against a fixed allowlist and
    # rejects custom artifact types outright — including this artifact type as a layer,
    # and application/vnd.oci.empty.v1+json as a config. Standard OCI layer types are
    # accepted everywhere, so the Ansible identity is carried on `artifactType` and in
    # annotations instead.
    blob = tar_gz({'content-manifest.json': payload})
    layer_digest = client.push_blob(repository, blob)
    config_digest = client.push_blob(repository, EMPTY_JSON)
    logger.debug('Manifest layer %s (%d bytes gzipped, %d raw)',
                 layer_digest, len(blob), len(payload))

    referrer = {
        'schemaVersion': 2,
        'mediaType': OCI_MANIFEST,
        'artifactType': constants.CONTENT_MANIFEST_ARTIFACT_TYPE,
        'config': {
            'mediaType': FALLBACK_CONFIG_MEDIA_TYPE,
            'digest': config_digest,
            'size': len(EMPTY_JSON),
        },
        'layers': [{
            'mediaType': TAR_GZIP_MEDIA_TYPE,
            'digest': layer_digest,
            'size': len(blob),
            'annotations': {
                'org.opencontainers.image.title': 'content-manifest.json',
                'io.ansible.content.manifest.media_type':
                    constants.CONTENT_MANIFEST_ARTIFACT_TYPE,
            },
        }],
        # The link that makes this discoverable from the image.
        'subject': {
            'mediaType': subject_media,
            'digest': subject_digest,
            'size': subject_size,
        },
        'annotations': _annotations(content),
    }

    referrer_bytes = json.dumps(referrer, separators=(',', ':')).encode()
    referrer_digest = digest_of(referrer_bytes)
    try:
        client.put_manifest(repository, referrer_digest, referrer)
    except urllib.error.HTTPError:
        # Some registries reject `subject` outright. Dropping it loses the referrers
        # API as a discovery path, but the fallback tag published below still works.
        logger.warning('Registry rejected the `subject` field; '
                       'discovery will rely on the tag fallback.')
        referrer.pop('subject', None)
        referrer_bytes = json.dumps(referrer, separators=(',', ':')).encode()
        referrer_digest = digest_of(referrer_bytes)
        client.put_manifest(repository, referrer_digest, referrer)

    # Publish the fallback tag the spec defines for registries without a referrers
    # endpoint. The tag must hold an image *index listing referrer descriptors*, not
    # the referrer manifest itself. Getting this wrong is easy, and it silently breaks
    # discovery on exactly the registries that need the fallback.
    fallback_tag = subject_digest.replace(':', '-')
    descriptor = {
        'mediaType': OCI_MANIFEST,
        'digest': referrer_digest,
        'size': len(referrer_bytes),
        'artifactType': constants.CONTENT_MANIFEST_ARTIFACT_TYPE,
        'annotations': referrer['annotations'],
        # The OCI image index spec makes `platform` optional, but Quay validates the
        # index against the Docker manifest-list schema where it is required — and a
        # non-image artifact has no meaningful platform. This placeholder is the
        # conventional answer.
        'platform': {'architecture': 'unknown', 'os': 'unknown'},
    }

    manifests = [descriptor]
    existing = client.get_manifest_json(repository, fallback_tag)
    if existing and existing.get('manifests'):
        # Preserve other referrers; replace any previous content manifest.
        manifests = [m for m in existing['manifests']
                     if m.get('artifactType') != constants.CONTENT_MANIFEST_ARTIFACT_TYPE]
        manifests.append(descriptor)

    client.put_manifest(repository, fallback_tag, {
        'schemaVersion': 2,
        'mediaType': OCI_INDEX,
        'manifests': manifests,
    }, media_type=OCI_INDEX)

    index = client.get_referrers(repository, subject_digest)
    if index is None:
        logger.debug('Referrers API unavailable; consumers will use the %s tag fallback.',
                     fallback_tag)
    else:
        logger.debug('Referrers API available: %d referrer(s).',
                     len(index.get('manifests', [])))

    return {
        'subject': subject_digest,
        'referrer': referrer_digest,
        'fallback_tag': fallback_tag,
        'referrers_api': index is not None,
        'gzipped_size': len(blob),
        'raw_size': len(payload),
        'collections': len(content.get('collections', [])),
    }
