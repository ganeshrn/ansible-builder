"""Tests for `ansible-builder publish`."""

import json

import pytest

from ansible_builder import constants
from ansible_builder.exceptions import DefinitionError
from ansible_builder.publisher import Publisher, parse_image_reference


PAYLOAD = json.dumps({
    "schemaVersion": "1.0.0",
    "generatedBy": {"ansibleCore": "2.16.14"},
    "collections": [{"name": "cisco.ios", "version": "11.5.1"}],
}).encode()


@pytest.mark.parametrize('reference,expected', [
    ('quay.io/myorg/my-ee:latest', ('quay.io', 'myorg/my-ee', 'latest')),
    ('localhost:8080/demo/network-ee:poc', ('localhost:8080', 'demo/network-ee', 'poc')),
    ('127.0.0.1:8080/demo/ee', ('127.0.0.1:8080', 'demo/ee', 'latest')),
    ('quay.io/org/nested/path:v1', ('quay.io', 'org/nested/path', 'v1')),
])
def test_parse_image_reference(reference, expected):
    assert parse_image_reference(reference) == expected


def test_parse_image_reference_keeps_a_digest():
    digest = 'sha256:' + 'ab' * 32
    assert parse_image_reference(f'quay.io/demo/ee@{digest}') == \
        ('quay.io', 'demo/ee', digest)


@pytest.mark.parametrize('reference', ['my-ee:latest', 'myorg/my-ee:latest'])
def test_parse_image_reference_rejects_an_unqualified_name(reference):
    """
    A bare name means Docker Hub, which this cannot publish to.

    Rejecting it here produces a sentence that says what to do. Letting it through
    produces a 401 from a registry the user never mentioned.
    """
    with pytest.raises(DefinitionError, match='Cannot determine the registry'):
        parse_image_reference(reference)


def test_push_command_uses_the_chosen_runtime():
    publisher = Publisher(image='quay.io/demo/ee:poc', container_runtime='docker')
    assert publisher.push_command == ['docker', 'push', 'quay.io/demo/ee:poc']


def test_push_command_disables_tls_when_insecure():
    publisher = Publisher(image='localhost:8080/demo/ee:poc', insecure=True)
    assert '--tls-verify=false' in publisher.push_command


def test_credentials_fall_back_to_the_environment(monkeypatch):
    """So a token need not appear in shell history or a process list."""
    monkeypatch.setenv('ANSIBLE_BUILDER_REGISTRY_USERNAME', 'robot')
    monkeypatch.setenv('ANSIBLE_BUILDER_REGISTRY_PASSWORD', 'secret')
    publisher = Publisher(image='quay.io/demo/ee:poc')
    assert (publisher.username, publisher.password) == ('robot', 'secret')


def test_explicit_credentials_win_over_the_environment(monkeypatch):
    monkeypatch.setenv('ANSIBLE_BUILDER_REGISTRY_USERNAME', 'robot')
    publisher = Publisher(image='quay.io/demo/ee:poc', username='explicit')
    assert publisher.username == 'explicit'


def test_manifest_file_bypasses_the_image(tmp_path):
    path = tmp_path / 'content-manifest.json'
    path.write_bytes(PAYLOAD)
    publisher = Publisher(image='quay.io/demo/ee:poc', manifest=str(path))
    assert publisher.extract_manifest() == PAYLOAD


def test_extract_manifest_does_not_run_the_image(mocker):
    """
    create/cp/rm, never run.

    The manifest is a file in the filesystem; starting a container to read it would
    execute the image's entrypoint for no reason, and on some EEs that is not a no-op.
    """
    def fake_run(command, **_kwargs):
        if command[1] == 'create':
            return 0, ['deadbeef']
        if command[1] == 'cp':
            # Third argument is the destination path chosen by extract_manifest.
            with open(command[3], 'wb') as handle:
                handle.write(PAYLOAD)
        return 0, []

    run = mocker.patch('ansible_builder.publisher.run_command', side_effect=fake_run)
    publisher = Publisher(image='quay.io/demo/ee:poc')

    assert publisher.extract_manifest() == PAYLOAD

    verbs = [call.args[0][1] for call in run.call_args_list]
    assert verbs == ['create', 'cp', 'rm']
    assert 'run' not in verbs
    assert constants.CONTENT_MANIFEST_PATH in run.call_args_list[1].args[0][2]


def test_missing_manifest_says_what_to_do(mocker):
    mocker.patch('ansible_builder.publisher.run_command', return_value=(0, []))
    publisher = Publisher(image='quay.io/demo/ee:poc', skip_image_push=True)

    with pytest.raises(DefinitionError, match='No content manifest at'):
        publisher.publish()


def test_publish_pushes_the_image_then_the_manifest(mocker, tmp_path):
    path = tmp_path / 'content-manifest.json'
    path.write_bytes(PAYLOAD)

    run = mocker.patch('ansible_builder.publisher.run_command', return_value=(0, []))
    mocker.patch('ansible_builder.publisher.RegistryClient')
    publish = mocker.patch('ansible_builder.publisher.publish_content_manifest',
                           return_value={'subject': 'sha256:abc', 'referrer': 'sha256:def',
                                         'fallback_tag': 'sha256-abc', 'referrers_api': False,
                                         'gzipped_size': 100, 'raw_size': 200,
                                         'collections': 1})

    publisher = Publisher(image='quay.io/demo/ee:poc', manifest=str(path))
    assert publisher.publish() is True

    run.assert_called_once_with(
        [constants.default_container_runtime, 'push', 'quay.io/demo/ee:poc'])
    assert publish.call_args.args[1:] == ('demo/ee', 'poc', PAYLOAD)


def test_skip_image_push_publishes_only_the_manifest(mocker, tmp_path):
    path = tmp_path / 'content-manifest.json'
    path.write_bytes(PAYLOAD)

    run = mocker.patch('ansible_builder.publisher.run_command')
    mocker.patch('ansible_builder.publisher.RegistryClient')
    mocker.patch('ansible_builder.publisher.publish_content_manifest',
                 return_value={'subject': 'sha256:abc', 'referrer': 'sha256:def',
                               'fallback_tag': 'sha256-abc', 'referrers_api': True,
                               'gzipped_size': 100, 'raw_size': 200, 'collections': 1})

    Publisher(image='quay.io/demo/ee:poc', manifest=str(path),
              skip_image_push=True).publish()
    run.assert_not_called()


def test_missing_manifest_file_blames_the_file_not_the_image(mocker):
    """
    A wrong --manifest path must not be reported as "no manifest in the image".

    That message sends someone off to rebuild an image that was never the problem.
    """
    mocker.patch('ansible_builder.publisher.run_command', return_value=(0, []))
    publisher = Publisher(image='quay.io/demo/ee:poc', manifest='does-not-exist.json',
                          skip_image_push=True)

    with pytest.raises(DefinitionError, match='No such manifest file: does-not-exist.json'):
        publisher.publish()
