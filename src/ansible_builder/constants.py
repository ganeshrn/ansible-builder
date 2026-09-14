import shutil

default_tag = 'ansible-execution-env:latest'
default_build_context = 'context'
default_verbosity = 2
max_verbosity = 3
runtime_files = {
    'podman': 'Containerfile',
    'docker': 'Dockerfile'
}
default_container_runtime = 'podman'
base_roles_path = '/usr/share/ansible/roles'
base_collections_path = '/usr/share/ansible/collections'

build_arg_defaults = {
    # empty string values here still allow the build arg to be emitted into the generated Containerfile
    'ANSIBLE_GALAXY_CLI_COLLECTION_OPTS': '',
    'ANSIBLE_GALAXY_CLI_ROLE_OPTS': '',
    'EE_BASE_IMAGE': 'quay.io/ansible/ansible-runner:latest',
    # this value is removed elsewhere for v3+ schemas
    'EE_BUILDER_IMAGE': 'quay.io/ansible/ansible-builder:latest',
    'PKGMGR_PRESERVE_CACHE': '',
}

user_content_subfolder = '_build'

if shutil.which('podman'):
    default_container_runtime = 'podman'
else:
    default_container_runtime = 'docker'

default_keyring_name = 'keyring.gpg'
default_policy_file_name = 'policy.json'

EXCL_COLLECTIONS_FILENAME = 'exclude-collections.txt'
STD_BINDEP_FILENAME = 'bindep.txt'
STD_GALAXY_FILENAME = 'requirements.yml'
STD_PIP_FILENAME = 'requirements.txt'

# Files that need to be moved into the build context, and their naming inside the context
CONTEXT_FILES = {
    'galaxy': STD_GALAXY_FILENAME,
    'python': STD_PIP_FILENAME,
    'system': STD_BINDEP_FILENAME,
}

FINAL_IMAGE_BIN_PATH = "/opt/builder/bin"

# Build-time content manifest.
#
# The manifest is generated inside the final image by that image's own ansible-core.
# An EE ships a specific core, and collections are authored against it: plugin loading,
# argument-spec handling and documentation-fragment resolution all vary between
# versions, so enumerating content from outside with a different core can be quietly
# wrong. Generating in place is correct by construction, and the cost is paid once at
# build time rather than by every consumer.
CONTENT_MANIFEST_SCRIPT = 'content_manifest.py'
CONTENT_MANIFEST_PATH = '/usr/share/ansible/content-manifest.json'
CONTENT_MANIFEST_DOCS_LEVELS = ('full', 'summary', 'none')
DEFAULT_CONTENT_MANIFEST_DOCS = 'full'

# Media type identifying the manifest as an OCI artifact. Carried on `artifactType`
# and in annotations rather than on the layer: registries vary in how strictly they
# police media types, and a custom layer type is not portable.
CONTENT_MANIFEST_ARTIFACT_TYPE = 'application/vnd.ansible.content-manifest.v1+json'

DEFAULT_EE_BASENAME = "execution-environment"
YAML_FILENAME_EXTENSIONS = ('yml', 'yaml')

SUCCESS_LOGLEVEL = 100
