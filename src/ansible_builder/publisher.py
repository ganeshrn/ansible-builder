"""
Push an execution environment and its content manifest to an OCI registry.

``ansible-builder build`` stops at a local image. This is the step after: it hands the
image to the container runtime to push, then publishes the content manifest the build
generated as an OCI referrer of that image, so a catalog can read what is inside the
image without pulling it.

The two halves are deliberately separable. ``--skip-image-push`` publishes only the
manifest, for an image that is already in the registry.
"""

from __future__ import annotations

import logging
import os
import tempfile
import urllib.error

from pathlib import Path

from . import constants
from .exceptions import DefinitionError
from .oci import RegistryClient, publish_content_manifest
from .utils import run_command

logger = logging.getLogger(__name__)


def parse_image_reference(reference: str) -> tuple[str, str, str]:
    """
    Split ``registry/repository:tag`` into its parts.

    Follows the usual registry convention: the first path segment is the registry only
    when it looks like a host — it contains a dot or a port, or it is ``localhost``.
    Otherwise the whole thing is a repository on Docker Hub, which this cannot publish
    to anonymously, so that case is rejected with an explanation rather than a
    confusing 401 later.
    """
    remainder, _, digest = reference.partition('@')
    head, _, tail = remainder.partition('/')

    if not tail or not ('.' in head or ':' in head or head == 'localhost'):
        raise DefinitionError(
            f"Cannot determine the registry from '{reference}'. "
            "Use a fully qualified reference, for example "
            "'quay.io/myorg/my-ee:latest'."
        )

    registry = head
    repository, _, tag = tail.rpartition(':')
    if not repository:
        # No colon in the remainder, so rpartition put everything in `tag`.
        repository, tag = tail, 'latest'

    return registry, repository, digest or tag


class Publisher:
    """Publishes an execution environment image and its content manifest."""

    def __init__(self,  # pylint: disable=unused-argument
                 *,
                 image: str,
                 container_runtime: str = constants.default_container_runtime,
                 manifest: str | None = None,
                 manifest_path: str = constants.CONTENT_MANIFEST_PATH,
                 skip_image_push: bool = False,
                 insecure: bool = False,
                 username: str | None = None,
                 password: str | None = None,
                 # Absorbs the rest of the argparse namespace (verbosity, action) so
                 # the CLI can hand this the whole thing.
                 **kwargs) -> None:
        self.image = image
        self.container_runtime = container_runtime
        self.manifest = manifest
        self.manifest_path = manifest_path
        self.skip_image_push = skip_image_push
        self.insecure = insecure
        self.username = username or os.environ.get('ANSIBLE_BUILDER_REGISTRY_USERNAME')
        self.password = password or os.environ.get('ANSIBLE_BUILDER_REGISTRY_PASSWORD')

        self.registry, self.repository, self.reference = parse_image_reference(image)

    @property
    def push_command(self) -> list[str]:
        command = [self.container_runtime, 'push']
        if self.insecure:
            command.append('--tls-verify=false')
        command.append(self.image)
        return command

    def extract_manifest(self) -> bytes:
        """
        Read the content manifest out of the image.

        Uses ``create``/``cp``/``rm`` rather than running the image, because the
        manifest is a file in the filesystem and starting a container to read it would
        execute the image's entrypoint for no reason.
        """
        if self.manifest:
            # Raised as a DefinitionError rather than left to surface as a
            # FileNotFoundError, which publish() would otherwise report as "no manifest
            # in the image" — blaming the image for a path that was simply wrong.
            path = Path(self.manifest)
            if not path.is_file():
                raise DefinitionError(f"No such manifest file: {self.manifest}")
            return path.read_bytes()

        container_id = run_command(
            [self.container_runtime, 'create', self.image],
            capture_output=True)[1][-1].strip()

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                destination = os.path.join(tmpdir, 'content-manifest.json')
                run_command([
                    self.container_runtime, 'cp',
                    f'{container_id}:{self.manifest_path}', destination,
                ])
                return Path(destination).read_bytes()
        finally:
            run_command([self.container_runtime, 'rm', '-f', container_id],
                        capture_output=True, allow_error=True)

    def publish(self) -> bool:
        if not self.skip_image_push:
            logger.debug('Pushing image %s', self.image)
            run_command(self.push_command)

        try:
            payload = self.extract_manifest()
        except (FileNotFoundError, IndexError) as error:
            raise DefinitionError(
                f"No content manifest at {self.manifest_path} in {self.image}. "
                "Build with options.content_manifest.enabled (the default), or pass "
                "--manifest to publish one from a file."
            ) from error

        logger.debug('Publishing content manifest to %s/%s', self.registry, self.repository)
        client = RegistryClient(self.registry, insecure=self.insecure,
                                username=self.username, password=self.password)

        try:
            result = publish_content_manifest(
                client, self.repository, self.reference, payload)
        except urllib.error.HTTPError as error:
            raise DefinitionError(
                f'Registry rejected the content manifest: '
                f'HTTP {error.code} {error.reason}'
            ) from error

        discovery = ('referrers API' if result['referrers_api']
                     else f"tag fallback {result['fallback_tag']}")
        logger.log(
            constants.SUCCESS_LOGLEVEL,
            'Published content manifest for %s: %d collection(s), %d bytes gzipped, '
            'discoverable via %s.',
            result['subject'], result['collections'], result['gzipped_size'], discovery,
        )
        return True
