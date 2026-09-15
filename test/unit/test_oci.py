"""
Tests for OCI artifact publishing.

The assertions here are mostly about the three registry behaviours worked around in
``oci.py``. Each was found by a failed push against a real Quay, and each is the kind of
detail a future reader would reasonably delete as unnecessary — so each has a test that
says, in its name, what breaks.
"""

import json
import urllib.error

import pytest

from ansible_builder import constants, oci


MANIFEST = {
    "schemaVersion": "1.0.0",
    "generatedAt": "2026-09-14T00:00:00Z",
    "generatedBy": {"tool": "ansible-builder-content-manifest", "ansibleCore": "2.16.14"},
    "collections": [
        {"name": "cisco.ios", "version": "11.5.1"},
        {"name": "ansible.utils", "version": "6.1.0"},
    ],
}
PAYLOAD = json.dumps(MANIFEST).encode()

SUBJECT_DIGEST = "sha256:" + "ab" * 32


class FakeRegistry:
    """Records what would have been pushed, and simulates a registry without referrers."""

    # Signatures must match RegistryClient's, so several parameters go unread here.
    # pylint: disable=unused-argument

    def __init__(self, reject_subject=False, existing_index=None, has_referrers=False):
        self.blobs = {}
        self.manifests = {}
        self.reject_subject = reject_subject
        self.existing_index = existing_index
        self.has_referrers = has_referrers
        self.logged_in = None

    def login(self, repository):
        self.logged_in = repository
        return True

    def resolve_digest(self, repository, reference):
        return SUBJECT_DIGEST, 1234, oci.OCI_MANIFEST

    def push_blob(self, repository, data):
        digest = oci.digest_of(data)
        self.blobs[digest] = data
        return digest

    def put_manifest(self, repository, reference, manifest, media_type=oci.OCI_MANIFEST):
        if self.reject_subject and 'subject' in manifest:
            raise urllib.error.HTTPError(
                'url', 400, 'Bad Request', {}, None)  # type: ignore[arg-type]
        self.manifests[reference] = manifest
        return oci.digest_of(json.dumps(manifest, separators=(',', ':')).encode())

    def get_manifest_json(self, repository, reference):
        return self.existing_index

    def get_referrers(self, repository, digest):
        return {"manifests": []} if self.has_referrers else None


@pytest.fixture(name="published")
def published_fixture():
    registry = FakeRegistry()
    result = oci.publish_content_manifest(registry, 'demo/network-ee', 'poc', PAYLOAD)
    return registry, result


def test_tar_gz_is_byte_for_byte_reproducible():
    """
    A blob's name in the registry IS the hash of these bytes.

    If packing the same manifest twice produced different bytes, the existence check
    before upload would always miss and every publish would orphan another copy of
    identical content in the registry. Both the tar entry mtime and the gzip header
    mtime have to be pinned; the gzip one defaults to "now".
    """
    assert oci.tar_gz({"a.json": b"x"}) == oci.tar_gz({"a.json": b"x"})


def test_layer_uses_a_standard_media_type(published):
    """
    Quay rejects custom layer media types outright, and octet-stream too.

    The Ansible identity has to travel on `artifactType` and in annotations instead.
    Putting it on the layer works on Zot and fails on Quay.
    """
    registry, result = published
    referrer = registry.manifests[result['referrer']]
    layer = referrer['layers'][0]

    assert layer['mediaType'] == oci.TAR_GZIP_MEDIA_TYPE
    assert referrer['artifactType'] == constants.CONTENT_MANIFEST_ARTIFACT_TYPE
    assert layer['annotations']['io.ansible.content.manifest.media_type'] == \
        constants.CONTENT_MANIFEST_ARTIFACT_TYPE


def test_config_avoids_the_oci_empty_descriptor(published):
    """
    application/vnd.oci.empty.v1+json is the OCI 1.1 answer, and Quay's config
    allowlist does not include it. vnd.unknown.config.v1+json is the escape hatch.
    """
    registry, result = published
    config = registry.manifests[result['referrer']]['config']

    assert config['mediaType'] == oci.FALLBACK_CONFIG_MEDIA_TYPE
    assert registry.blobs[config['digest']] == oci.EMPTY_JSON


def test_fallback_tag_holds_an_index_not_the_referrer(published):
    """
    The single easiest thing to get wrong, and it fails silently.

    Pushing the referrer manifest directly under the fallback tag breaks discovery on
    exactly the registries that need the fallback — the ones without a referrers API.
    """
    registry, result = published
    index = registry.manifests[result['fallback_tag']]

    assert result['fallback_tag'] == SUBJECT_DIGEST.replace(':', '-')
    assert index['mediaType'] == oci.OCI_INDEX
    assert [m['digest'] for m in index['manifests']] == [result['referrer']]


def test_index_descriptor_carries_a_platform_placeholder(published):
    """
    The OCI image index spec makes `platform` optional and a non-image artifact has no
    meaningful one — but Quay validates the index against the Docker manifest-list
    schema, where it is required.
    """
    registry, result = published
    descriptor = registry.manifests[result['fallback_tag']]['manifests'][0]
    assert descriptor['platform'] == {'architecture': 'unknown', 'os': 'unknown'}


def test_subject_links_the_manifest_to_the_image(published):
    registry, result = published
    subject = registry.manifests[result['referrer']]['subject']

    assert subject['digest'] == SUBJECT_DIGEST == result['subject']
    assert registry.logged_in == 'demo/network-ee'


def test_annotations_carry_what_labels_cannot(published):
    """Counts and ansible-core cannot be image labels; they must survive here."""
    registry, result = published
    annotations = registry.manifests[result['referrer']]['annotations']

    assert annotations['io.ansible.content.collections.count'] == '2'
    assert annotations['io.ansible.content.ansible_core'] == '2.16.14'
    assert annotations['io.ansible.content.manifest.schema'] == '1.0.0'


def test_subject_rejection_falls_back_to_the_tag():
    """A registry that refuses `subject` still gets a discoverable manifest."""
    registry = FakeRegistry(reject_subject=True)
    result = oci.publish_content_manifest(registry, 'demo/network-ee', 'poc', PAYLOAD)

    assert 'subject' not in registry.manifests[result['referrer']]
    assert result['fallback_tag'] in registry.manifests
    assert result['referrers_api'] is False


def test_existing_referrers_are_preserved():
    """Publishing must not evict someone else's artifact — a signature, say."""
    signature = {'digest': 'sha256:' + 'cd' * 32,
                 'artifactType': 'application/vnd.dev.cosign.simplesigning.v1+json'}
    stale = {'digest': 'sha256:' + 'ef' * 32,
             'artifactType': constants.CONTENT_MANIFEST_ARTIFACT_TYPE}
    registry = FakeRegistry(existing_index={'manifests': [signature, stale]})

    result = oci.publish_content_manifest(registry, 'demo/network-ee', 'poc', PAYLOAD)
    digests = [m['digest'] for m in registry.manifests[result['fallback_tag']]['manifests']]

    assert signature['digest'] in digests, "another tool's referrer was dropped"
    assert stale['digest'] not in digests, "the previous content manifest was not replaced"
    assert result['referrer'] in digests


def test_referrers_api_is_reported_when_available():
    registry = FakeRegistry(has_referrers=True)
    result = oci.publish_content_manifest(registry, 'demo/network-ee', 'poc', PAYLOAD)
    assert result['referrers_api'] is True


def test_login_primes_anonymously(mocker):
    """
    The priming request to /v2/ must carry no credentials.

    Quay's /v2/ accepts Bearer tokens only. Presenting Basic there earns
    `400 Invalid bearer token format` rather than a challenge — and a 400 carries no
    WWW-Authenticate header to learn from, so sending credentials eagerly is exactly
    what stops the client from ever discovering where to exchange them.
    """
    client = oci.RegistryClient('quay.example', username='robot', password='secret')
    request = mocker.patch.object(client, 'request', return_value=mocker.MagicMock())

    client.login('demo/ee')

    assert request.call_args.args[:2] == ('GET', '/v2/')
    assert request.call_args.kwargs['_auth'] is False


def test_request_omits_authorization_when_auth_is_false(mocker):
    client = oci.RegistryClient('quay.example', username='robot', password='secret')
    urlopen = mocker.patch('urllib.request.urlopen')

    client.request('GET', '/v2/', _auth=False)
    assert not urlopen.call_args.args[0].has_header('Authorization')

    client.request('GET', '/v2/')
    assert urlopen.call_args.args[0].has_header('Authorization')
