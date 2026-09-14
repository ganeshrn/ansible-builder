.. _builder_cli:

CLI Usage
=========

Ansible Builder can execute two separate steps. The first step is to create a build instruction file (:file:`Containerfile` for Podman, :file:`Dockerfile` for Docker) and a build context based on your :ref:`definition <builder_ee_definition>` file. The second step is to run a containerization tool (Podman or Docker) to build an image based on the build instruction file and build context. The :command:`ansible-builder build` command executes both steps, giving you a build instruction file, a build context, and a fully built container image. The :command:`ansible-builder create` command only executes the first step, giving you a build instruction file and a build context. If you use :command:`ansible-builder create`, you can use the resulting build instruction file and build context to build your container images on the platform of your choice.

.. note::

   Ansible Builder is colorized by default when outputting to a terminal. Color output can be disabled by setting the :envvar:`NO_COLOR` environment variable to any non-empty value,
   or by setting the :envvar:`CLICOLOR` environment variable to ``0``.


.. contents::
   :local:

The ``build`` command
---------------------

The :command:`ansible-builder build` command:

* takes an :ref:`execution environment definition file<builder_ee_definition>` as an input,
* outputs a build instruction file (:file:`Containerfile` for Podman, :file:`Dockerfile` for Docker),
* creates a build context necessary for building an execution environment image,
* builds the image.

By default, it looks for a file named :file:`execution-environment.yml` (or :file:`execution-environment.yaml`) in the current directory.

To build an execution environment using the default definition file, run:

.. code::

   $ ansible-builder build
   Running command:
     podman build -f context/Containerfile -t ansible-execution-env:latest context
   Complete! The build context can be found at: /path/to/context

Ansible Builder produces a ready-to-use container image and preserves the build context, which you can use to rebuild the image at a different time and/or location with the tooling of your choice.

Flags for the ``build`` command
-------------------------------

``--tag``
*********

Customizes the tagged name applied to the built image. To create an image with a custom name:

.. code::

   $ ansible-builder build --tag=my-custom-ee

More recent versions of :program:`ansible-builder` support multiple tags:

.. code::

   $ ansible-builder build --tag=tag1 --tag=tag2

``--file``
**********

Specifies the execution environment file. To use a file other than the default:

.. code::

   $ ansible-builder build --file=my-ee-def.yml

``--galaxy-keyring``
********************

Specifies a keyring for :program:`ansible-galaxy` to use to verify collection signatures during installation. To verify collection signatures:

.. code::

   $ ansible-builder create --galaxy-keyring=/path/to/pubring.kbx
   $ ansible-builder build --galaxy-keyring=/path/to/pubring.kbx

If you do not pass this option, no signature verification is performed. If you do pass this option, but the version of Ansible is too old to support this feature, you will see an error during the image build process.

``--galaxy-ignore-signature-status-code``
*****************************************

Ignores certain errors that may occur while verifying collections. This option is passed unmodified to :program:`ansible-galaxy` calls. Valid only when ``--galaxy-keyring`` is also set. See the :program:`ansible-galaxy` documentation for more information.

.. code::

   $ ansible-builder create --galaxy-keyring=/path/to/pubring.kbx --galaxy-ignore-signature-status-code 500
   $ ansible-builder build --galaxy-keyring=/path/to/pubring.kbx --galaxy-ignore-signature-status-code 500

``--galaxy-required-valid-signature-count``
*******************************************

Overrides the number of required valid collection signatures. This option is passed unmodified to :program:`ansible-galaxy` calls. Valid only when ``--galaxy-keyring`` is also set. See the :program:`ansible-galaxy` documentation for more information.

.. code::

   $ ansible-builder create --galaxy-keyring=/path/to/pubring.kbx --galaxy-required-valid-signature-count 3
   $ ansible-builder build --galaxy-keyring=/path/to/pubring.kbx --galaxy-required-valid-signature-count 3


.. _context:

``--context``
*************

Specifies the directory name for the build context Ansible Builder creates. Default directory name is :file:`context` in the current working directory. To specify another location:

.. code::

   $ ansible-builder build --context=/path/to/dir


.. _build-arg:

``--build-arg``
***************

Passes build-time arguments to Podman or Docker. Specify these flags or variables the same way you would with :command:`podman build` or :command:`docker build`.

By default, the :file:`Containerfile` / :file:`Dockerfile` created by Ansible Builder contains a build argument ``EE_BASE_IMAGE``, which can be useful for rebuilding execution environments without modifying any files.

.. code::

   $ ansible-builder build --build-arg FOO=bar

To use different build arguments, you can specify ``--build-arg`` multiple times:

.. code::

   $ ansible-builder build --build-arg FOO=bar --build-arg SIMPLE=sample

To use a custom base image:

.. code::

   $ ansible-builder build --build-arg EE_BASE_IMAGE=registry.example.com/another-ee


.. _container-runtime:

``--container-runtime``
***********************

Specifies the containerization tool used to build images. Default is Podman. To use Docker:

.. code::

   $ ansible-builder build --container-runtime=docker


.. _container-policy:

``--container-policy``
**********************

.. note:: Added in version 1.2

Specifies the container image validation policy to use. Valid only when :ref:`container-runtime` is :program:`podman`. Valid values are one of:

* ``ignore_all``: Run :program:`podman` with generated policy that ignores all signatures.
* ``system``: Relies on :program:`podman`'s consumption of system policy/signature with
  inline keyring paths. No builder-specific overrides are possible.
* ``signature_required``: Run :program:`podman` with ``--pull-always`` and a generated
   policy that rejects all by default, with generated identity requirements for
   referenced container images, using an explicitly-provided keyring (specified
   with the :ref:`container-keyring` CLI option).

.. _container-keyring:

``--container-keyring``
***********************

.. note:: Added in version 1.2

Specifies the path to a GPG keyring file to use for validating container image signatures.

.. _extra-build-cli-args:

``--extra-build-cli-args``
**************************

.. note:: Added in version 3.1

This option allows the user to pass any additional command line arguments to the container engine
build command (:command:`docker build` or :command:`podman build`). Take care when using this option as there is
no attempt to identify or resolve conflicting argument values from this option and arguments
normally added by :program:`ansible-builder`.

.. code::

   $ ansible-builder build --extra-build-cli-args='--pull --env=MY_ENV_VAR'

``--verbosity``
***************

Customizes the level of verbosity:

.. code::

   $ ansible-builder build --verbosity 2

You may also use ``-v`` for the shorthand version. You may either specify an integer for
the verbosity level, or supply multiples of the option. Individual instances of ``-v``
will stack. For example, the following are equivalent to setting the verbosity level to ``3``:

.. code::

   $ ansible-builder build -v 3
   $ ansible-builder build -vvv
   $ ansible-builder build -v -v -v


``--prune-images``
******************

Removes unused images created after the build process:

.. code::

   $ ansible-builder build --prune-images

.. note::

   This flag removes all the dangling images on the given machine whether they already existed or were created by :program:`ansible-builder` build process.


``--squash``
************

Controls the final image layer squashing. Valid values are:

* ``new``: Squash all of the final image's new layers into a single new layer
  (preexisting layers are not squashed).
* ``all``: Squash all of the final image's layers, including those inherited
  from the base image, into a single new layer.
* ``off``: Turn off layer squashing. This is the default.

.. note::

   This flag is compatible only with the :program:`podman` runtime and will be ignored for any other runtime. Docker does not support layer squashing; it is considered an experimental feature.


The ``create`` command
----------------------

The :command:`ansible-builder create` command accepts an execution environment definition as an input and outputs the build context necessary for building an execution environment image. However, the ``create`` command *will not* build the execution environment image; this is useful for creating just the build context and a :file:`Containerfile` that can then be shared.


.. _publish_command:

The ``publish`` command
-----------------------

The :command:`ansible-builder publish` command pushes an execution environment image to
an OCI registry, then publishes the :ref:`content manifest <builder_ee_definition>`
generated during the build as an OCI *referrer* of that image.

The manifest travels beside the image rather than inside it, which is what makes it
cheap to read. A catalog that wants to know which collections and modules an execution
environment contains fetches a referrer and one small blob — a few HTTP calls — instead
of pulling an image measured in hundreds of megabytes.

.. code::

   $ ansible-builder publish quay.io/myorg/my-ee:latest

The image reference must be fully qualified, because the registry to publish to is read
from it.

Publishing is deliberately separable from pushing. Use ``--skip-image-push`` to publish
a manifest for an image that is already in the registry:

.. code::

   $ ansible-builder publish quay.io/myorg/my-ee:latest --skip-image-push

.. note::

   Registries vary in what they accept, and this command accommodates that rather than
   assuming conformance. The manifest is packaged as a standard gzipped tar layer with
   the Ansible artifact type carried on ``artifactType``, because some registries reject
   custom layer media types; and a fallback tag holding an image index is always
   published alongside the referrer, because some registries do not implement the
   referrers API. Consumers discover the manifest by whichever path the registry
   supports.

Flags for the ``publish`` command
----------------------------------

``image``
*********

The fully qualified image reference to publish, for example
``quay.io/myorg/my-ee:latest``. A positional argument, and the only required one.

``--container-runtime``
***********************

The container runtime used to push the image and to read the manifest out of it.
Defaults to :program:`podman` when available, otherwise :program:`docker`.

``--manifest``
**************

Publish this manifest file instead of reading one out of the image. Useful when the
manifest was produced separately, or when publishing for an image that cannot be run
locally.

``--manifest-path``
*******************

Where the manifest lives inside the image. Defaults to
:file:`/usr/share/ansible/content-manifest.json`, matching the ``content_manifest.path``
option in the definition file.

``--skip-image-push``
*********************

Publish only the content manifest, leaving the image alone. Use this when the image is
already in the registry.

``--insecure``
**************

Use HTTP and skip TLS verification. Intended for local registries; do not use it against
a registry you do not control.

``--username`` and ``--password``
*********************************

Registry credentials. Both fall back to the :envvar:`ANSIBLE_BUILDER_REGISTRY_USERNAME`
and :envvar:`ANSIBLE_BUILDER_REGISTRY_PASSWORD` environment variables, which is the
better way to supply a token — a password passed on the command line lands in shell
history and in the process list.


The ``introspect`` command
---------------------------

The :command:`ansible-builder introspect` command loops over collections in a specified folder and returns data about their
dependencies. This command is used internally by :program:`ansible-builder` and is exposed here for verification purposes. It is
primarily targeted toward collection authors and maintainers who need to understand or validate collection dependencies.

To introspect collections in the default location:

.. code::

   $ ansible-builder introspect


Flags for the ``introspect`` command
-------------------------------------

``folder``
**********

The Ansible collections path to introspect. This is a positional argument that specifies the location of the
collections to analyze. The folder must contain an :file:`ansible_collections` subdirectory. If not supplied, it
will default to collections installed in :file:`/usr/share/ansible/collections`.

.. code::

   $ ansible-builder introspect /path/to/collections

``--write-pip``
***************

Write the combined pip requirements to a file. This option outputs all Python dependencies found across the
introspected collections to the specified file.

.. code::

   $ ansible-builder introspect --write-pip=requirements.txt /path/to/collections

``--write-bindep``
******************

Write the combined bindep requirements to a file. This option outputs all system-level dependencies found across the
introspected collections to the specified file.

.. code::

   $ ansible-builder introspect --write-bindep=bindep.txt /path/to/collections

``--user-pip``
**************

Specify an additional pip requirements file to combine with collection requirements. This is useful when you have
custom Python dependencies that should be included alongside the collection dependencies.

.. code::

   $ ansible-builder introspect --user-pip=user-requirements.txt --write-pip=combined.txt /path/to/collections

``--user-bindep``
*****************

Specify an additional bindep requirements file to combine with collection requirements. This is useful when you have
custom system-level dependencies that should be included alongside the collection dependencies.

.. code::

   $ ansible-builder introspect --user-bindep=user-bindep.txt --write-bindep=combined.txt /path/to/collections

``--exclude-pip-reqs``
**********************

Exclude specific pip requirements listed in a file. Each line in the file should contain one Python package name to
exclude from the final output.

.. code::

   $ ansible-builder introspect --exclude-pip-reqs=exclude.txt --write-pip=filtered.txt /path/to/collections

``--exclude-bindep-reqs``
*************************

Exclude specific bindep requirements listed in a file. Each line in the file should contain one system package name to
exclude from the final output.

.. code::

   $ ansible-builder introspect --exclude-bindep-reqs=exclude.txt --write-bindep=filtered.txt /path/to/collections

``--exclude-collection-reqs``
******************************

Exclude all requirements from specific collections listed in a file. Each line in the file should contain one
collection name (in the format ``namespace.name``) whose requirements should be excluded from the final output.

.. code::

   $ ansible-builder introspect --exclude-collection-reqs=collections-to-skip.txt /path/to/collections

.. note::

   The ``introspect`` command also supports the ``--verbosity`` flag, which works the same way as described in
   the ``build`` command documentation above.


Examples
--------

The example in :file:`test/data/pytz` requires the ``awx.awx`` collection in the execution environment definition. The lookup plugin
``awx.awx.schedule_rrule`` requires the PyPI ``pytz`` and another
library to work. If :file:`test/data/pytz/execution-environment.yml` file is
given to the :command:`ansible-builder build` command, then it will install the
collection inside the image, read :file:`requirements.txt` inside of the
collection, and then install ``pytz`` into the image.

The image produced can be used inside of an :program:`ansible-runner` project
by placing these variables inside the :file:`env/settings` file, inside of
the private data directory.


.. code:: yaml

    ---
    container_image: image-name
    process_isolation_executable: podman # or docker
    process_isolation: true

The ``awx.awx`` collection is a subset of content included in the default
AWX execution environment. More details can be found at the
`awx-ee <https://github.com/ansible/awx-ee>`_ repository.


Deprecated Features
-------------------

The ``--base-image`` CLI option has been removed.
See the ``--build-arg`` option for a replacement.
