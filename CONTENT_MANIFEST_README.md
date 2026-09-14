# Content Manifest Script

## Overview

The `content_manifest.py` script is a critical component of Ansible Builder that generates comprehensive manifests of Ansible content from within execution environments.

## Purpose

This script generates an Ansible content manifest that runs during the image build, inside the final image using the image's own ansible-core. This approach ensures correctness because:

- An Execution Environment (EE) ships a specific ansible-core version
- Collections are authored against that specific version
- Plugin loading, argument-spec handling, and documentation-fragment resolution vary between versions
- Enumerating content from the final image with the correct ansible-core is correct by construction

## Key Benefits

- **Correctness**: Uses the actual ansible-core version that runs in the EE
- **Performance**: Cost is paid once at build time, not repeatedly by consumers
- **Completeness**: Comprehensive documentation extraction with fragment resolution

## Content Coverage

The manifest includes:
- **Collections**: Version and metadata
- **Plugins**: All ansible-core plugin types with fragment-resolved documentation:
  - Filters, lookups, tests
  - Callbacks, connections, inventory
  - Networking plugins (cliconf, netconf, httpapi)
  - Privilege escalation (become), caching, shell
  - Strategy, variables, and terminal plugins
- **Roles**: Including argument_spec entry points
- **Playbooks**: Shipped in collections
- **Event-Driven Ansible (EDA)**: event_source and event_filter plugins, and rulebooks

## Usage

```bash
# Generate full manifest with documentation
python content_manifest.py generate --output /path/to/output.json --docs full

# Generate manifest with summary documentation only
python content_manifest.py generate --docs summary

# Generate manifest without documentation
python content_manifest.py generate --docs none

# Get labels from an existing manifest
python content_manifest.py labels --manifest /path/to/manifest.json
```

## Schema

The generated manifest follows schema version `1.0.0` and provides structured, machine-readable information about all content available in the execution environment.

## Integration

This script is designed to be executed as part of the Ansible Builder image build process, ensuring that every execution environment has accurate, complete metadata about its available Ansible content.
