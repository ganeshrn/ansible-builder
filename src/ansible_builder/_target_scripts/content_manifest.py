#!/usr/bin/env python3
"""
Generate an Ansible content manifest from inside an execution environment.

This runs during the image build, in the final image, using the image's own
ansible-core. That matters for correctness, not only speed: an EE ships a specific
ansible-core and its collections are authored against that version. Enumerating from
outside with a different core can silently produce wrong results, because plugin
loading, argument-spec handling and documentation-fragment resolution all vary between
versions. Doing it here is correct by construction.

It is also where the cost belongs. Extracting fragment-resolved documentation for a
whole environment takes a few seconds once, at build time. Deferring it to consumers
means paying it repeatedly, per catalog render, forever.

Content covered:
  * collections, with version and metadata
  * modules and every ansible-core plugin type - filter, lookup, test, callback,
    connection, inventory, cliconf, netconf, httpapi, become, cache, shell, strategy,
    vars, terminal - with fragment-resolved documentation
  * roles, including argument_specs entry points
  * playbooks shipped in collections
  * Event-Driven Ansible event_source and event_filter plugins, and rulebooks

Usage:
    content_manifest.py generate [--output PATH] [--collections-path PATH]
                                 [--docs {full,summary,none}] [--quiet]
    content_manifest.py labels [--manifest PATH]
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

try:
    import yaml
except ImportError:  # pragma: no cover - present in every EE base image
    yaml = None

SCHEMA_VERSION = "1.0.0"
TOOL_NAME = "ansible-builder-content-manifest"

DEFAULT_COLLECTIONS_PATH = "/usr/share/ansible/collections"
DEFAULT_OUTPUT = "/usr/share/ansible/content-manifest.json"

# Plugin directories ansible-core recognises, mapped to their plugin type. Used for the
# filesystem fallback when ansible-doc is unavailable.
PLUGIN_DIRS = {
    "modules": "module",
    "action": "action",
    "become": "become",
    "cache": "cache",
    "callback": "callback",
    "cliconf": "cliconf",
    "connection": "connection",
    "filter": "filter",
    "httpapi": "httpapi",
    "inventory": "inventory",
    "lookup": "lookup",
    "netconf": "netconf",
    "shell": "shell",
    "strategy": "strategy",
    "terminal": "terminal",
    "test": "test",
    "vars": "vars",
}

EDA_PLUGIN_DIRS = ("event_source", "event_filter")


class Diagnostics:
    """Collects problems so that partial results stay honest instead of silently lossy."""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def add(self, level: str, message: str, collection: str | None = None) -> None:
        entry = {"level": level, "message": message}
        if collection:
            entry["collection"] = collection
        self.entries.append(entry)

    @property
    def has_errors(self) -> bool:
        return any(e["level"] == "error" for e in self.entries)


# ---------------------------------------------------------------------------
# Small IO helpers
# ---------------------------------------------------------------------------

def _read_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _read_yaml(path: str) -> dict | None:
    if yaml is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, yaml.YAMLError):
        return None


def _module_level_string(path: str, name: str) -> str | None:
    """
    Extract a module-level string assignment without importing the module.

    Importing would require every third-party dependency a plugin happens to use to be
    installed and importable, turning a missing optional package into missing
    documentation. Parsing the AST reads the literal directly and cannot execute code.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except (OSError, SyntaxError, ValueError):
        return None

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if getattr(target, "id", None) == name:
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    return None
                return value if isinstance(value, str) else None
    return None


# ---------------------------------------------------------------------------
# Documentation shaping
# ---------------------------------------------------------------------------

def _shape_option(spec: dict, docs_mode: str) -> dict:
    """Reduce an option spec to the fields consumers actually use."""
    out: dict = {}
    for key in ("type", "required", "default", "choices", "aliases", "elements",
                "version_added"):
        if key in spec and spec[key] is not None:
            out[key] = spec[key]
    if docs_mode == "full" and spec.get("description"):
        out["description"] = spec["description"]
    # Suboptions recurse; network modules nest deeply, which is exactly the detail
    # that makes them worth documenting.
    suboptions = spec.get("suboptions")
    if isinstance(suboptions, dict):
        out["suboptions"] = {
            name: _shape_option(sub, docs_mode)
            for name, sub in suboptions.items()
            if isinstance(sub, dict)
        }
    return out


def _shape_plugin(fqcn: str, plugin_type: str, payload: dict, docs_mode: str) -> dict:
    """Turn one ansible-doc entry into a manifest plugin entry."""
    doc = payload.get("doc") or {}
    short_name = fqcn.rsplit(".", 1)[-1]

    entry: dict = {
        "name": short_name,
        "type": plugin_type,
        "fqcn": fqcn,
    }

    if doc.get("short_description"):
        entry["shortDescription"] = doc["short_description"]
    if doc.get("version_added"):
        entry["versionAdded"] = str(doc["version_added"])
    if doc.get("deprecated"):
        entry["deprecated"] = True
        entry["deprecation"] = doc["deprecated"]

    if docs_mode == "none":
        return entry

    if doc.get("author"):
        entry["author"] = doc["author"]
    if doc.get("notes"):
        entry["notes"] = doc["notes"]
    if doc.get("requirements"):
        entry["requirements"] = doc["requirements"]

    options = doc.get("options")
    if isinstance(options, dict):
        entry["options"] = {
            name: _shape_option(spec, docs_mode)
            for name, spec in options.items()
            if isinstance(spec, dict)
        }

    if docs_mode == "full":
        if doc.get("description"):
            entry["description"] = doc["description"]
        if payload.get("examples"):
            entry["examples"] = payload["examples"]
        if payload.get("return"):
            entry["returns"] = payload["return"]

    return entry


# ---------------------------------------------------------------------------
# ansible-doc based extraction
# ---------------------------------------------------------------------------

def ansible_doc_dump(collections_path: str, diagnostics: Diagnostics) -> dict | None:
    """
    Run `ansible-doc --metadata-dump`, which resolves documentation fragments.

    This is the whole reason to generate the manifest inside the image. A single call
    returns fragment-resolved documentation for every plugin of every type; querying
    ansible-doc per plugin instead costs seconds each because of its process-per-query
    model.
    """
    env = dict(os.environ)
    env["ANSIBLE_COLLECTIONS_PATH"] = collections_path
    env.setdefault("ANSIBLE_LOCAL_TEMP", "/tmp/.ansible-tmp")

    try:
        completed = subprocess.run(
            ["ansible-doc", "--metadata-dump", "--no-fail-on-errors"],
            capture_output=True,
            text=True,
            env=env,
            timeout=900,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        diagnostics.add("warning",
                        f"ansible-doc unavailable ({exc}); falling back to filesystem "
                        "enumeration without documentation.")
        return None

    if completed.returncode != 0:
        diagnostics.add("warning",
                        f"ansible-doc exited {completed.returncode}; falling back to "
                        "filesystem enumeration without documentation.")
        return None

    try:
        return json.loads(completed.stdout).get("all", {})
    except ValueError as exc:
        diagnostics.add("warning", f"Could not parse ansible-doc output: {exc}")
        return None


def group_plugins_by_collection(dump: dict, docs_mode: str) -> dict[str, list[dict]]:
    """
    Index plugins by `namespace.name`.

    ansible-doc reports a flat namespace across all collections, so grouping is by the
    FQCN prefix. Entries reporting an extraction error are kept with the error attached
    rather than dropped — a plugin that failed to document still exists in the image.
    """
    grouped: dict[str, list[dict]] = {}

    for plugin_type, plugins in dump.items():
        if plugin_type == "keyword" or not isinstance(plugins, dict):
            continue
        for fqcn, payload in plugins.items():
            parts = fqcn.split(".")
            if len(parts) < 3:
                continue  # not collection-qualified, i.e. shipped by ansible-core
            collection_key = f"{parts[0]}.{parts[1]}"

            if not isinstance(payload, dict):
                continue
            if payload.get("error"):
                grouped.setdefault(collection_key, []).append({
                    "name": parts[-1],
                    "type": plugin_type,
                    "fqcn": fqcn,
                    "extractionError": str(payload["error"])[:500],
                })
                continue

            grouped.setdefault(collection_key, []).append(
                _shape_plugin(fqcn, plugin_type, payload, docs_mode))

    for entries in grouped.values():
        entries.sort(key=lambda e: (e["type"], e["name"]))
    return grouped


def filesystem_plugins(collection_dir: str, fqcn: str) -> list[dict]:
    """
    Name-only plugin enumeration, used when ansible-doc could not run.

    Produces a truthful but undocumented listing rather than nothing.
    """
    plugins: list[dict] = []
    plugins_root = os.path.join(collection_dir, "plugins")
    if not os.path.isdir(plugins_root):
        return plugins

    for dirname, plugin_type in sorted(PLUGIN_DIRS.items()):
        plugin_dir = os.path.join(plugins_root, dirname)
        if not os.path.isdir(plugin_dir):
            continue
        for filename in sorted(os.listdir(plugin_dir)):
            if filename == "__init__.py" or not filename.endswith((".py", ".yml", ".yaml")):
                continue
            name = os.path.splitext(filename)[0]
            deprecated = name.startswith("_")
            name = name.lstrip("_")
            if not name:
                continue
            plugins.append({
                "name": name,
                "type": plugin_type,
                "fqcn": f"{fqcn}.{name}",
                "deprecated": deprecated,
            })
    return plugins


# ---------------------------------------------------------------------------
# Roles, playbooks, EDA
# ---------------------------------------------------------------------------

def enumerate_roles(collection_dir: str, fqcn: str, docs_mode: str) -> list[dict]:
    """
    Enumerate roles, reading argument_specs.yml entry points where present.

    Roles without argument_specs are still listed, flagged `inferred`, because omitting
    them would misrepresent the collection. Consumers can tell declared structure from
    inferred.
    """
    roles: list[dict] = []
    roles_root = os.path.join(collection_dir, "roles")
    if not os.path.isdir(roles_root):
        return roles

    for role_name in sorted(os.listdir(roles_root)):
        role_dir = os.path.join(roles_root, role_name)
        if not os.path.isdir(role_dir) or role_name.startswith("."):
            continue

        entry: dict = {
            "name": role_name,
            "fqcn": f"{fqcn}.{role_name}",
            "entryPoints": ["main"],
            "inferred": True,
        }

        spec = None
        for spec_name in ("argument_specs.yml", "argument_specs.yaml"):
            spec = _read_yaml(os.path.join(role_dir, "meta", spec_name))
            if spec and isinstance(spec.get("argument_specs"), dict):
                break
            spec = None

        if spec:
            specs = spec["argument_specs"]
            entry["inferred"] = False
            entry["entryPoints"] = sorted(specs.keys())
            if docs_mode != "none":
                entry["entryPointSpecs"] = {
                    name: {
                        "shortDescription": body.get("short_description"),
                        "description": body.get("description") if docs_mode == "full" else None,
                        "options": {
                            opt: _shape_option(val, docs_mode)
                            for opt, val in (body.get("options") or {}).items()
                            if isinstance(val, dict)
                        },
                    }
                    for name, body in specs.items()
                    if isinstance(body, dict)
                }

        meta = _read_yaml(os.path.join(role_dir, "meta", "main.yml"))
        if meta and isinstance(meta.get("galaxy_info"), dict) and docs_mode != "none":
            galaxy_info = meta["galaxy_info"]
            if galaxy_info.get("description"):
                entry["description"] = galaxy_info["description"]

        roles.append(entry)
    return roles


def enumerate_playbooks(collection_dir: str, fqcn: str) -> list[dict]:
    """
    List playbooks shipped in a collection.

    Only names are reported. No documentation convention for collection playbooks is
    standardised, and header comments are too inconsistent to parse reliably - guessing
    would produce confidently wrong descriptions, which is worse than none.
    """
    playbooks: list[dict] = []
    playbooks_root = os.path.join(collection_dir, "playbooks")
    if not os.path.isdir(playbooks_root):
        return playbooks

    for filename in sorted(os.listdir(playbooks_root)):
        if not filename.endswith((".yml", ".yaml")):
            continue
        name = os.path.splitext(filename)[0]
        playbooks.append({"name": name, "fqcn": f"{fqcn}.{name}"})
    return playbooks


def enumerate_eda_plugins(collection_dir: str, fqcn: str, docs_mode: str,
                          diagnostics: Diagnostics) -> list[dict]:
    """
    Enumerate Event-Driven Ansible plugins from extensions/eda/plugins/.

    ansible-core knows nothing about these types, so ansible-doc cannot describe them.
    They do carry a module-level DOCUMENTATION string in the same YAML dialect, which
    is read here directly from the AST.
    """
    found: list[dict] = []
    eda_root = os.path.join(collection_dir, "extensions", "eda", "plugins")
    if not os.path.isdir(eda_root):
        return found

    for plugin_type in EDA_PLUGIN_DIRS:
        type_dir = os.path.join(eda_root, plugin_type)
        if not os.path.isdir(type_dir):
            continue

        for filename in sorted(os.listdir(type_dir)):
            if filename == "__init__.py" or not filename.endswith(".py"):
                continue
            name = os.path.splitext(filename)[0]
            entry: dict = {
                "name": name,
                "type": plugin_type,
                "fqcn": f"{fqcn}.{name}",
            }

            raw = _module_level_string(os.path.join(type_dir, filename), "DOCUMENTATION")
            if raw and yaml is not None:
                try:
                    doc = yaml.safe_load(raw)
                except yaml.YAMLError as exc:
                    diagnostics.add("warning",
                                    f"EDA plugin {name}: unparsable DOCUMENTATION ({exc})",
                                    fqcn)
                    doc = None
                if isinstance(doc, dict):
                    if doc.get("short_description"):
                        entry["shortDescription"] = doc["short_description"]
                    if docs_mode != "none":
                        if docs_mode == "full" and doc.get("description"):
                            entry["description"] = doc["description"]
                        options = doc.get("options")
                        if isinstance(options, dict):
                            entry["options"] = {
                                opt: _shape_option(val, docs_mode)
                                for opt, val in options.items()
                                if isinstance(val, dict)
                            }
                    if docs_mode == "full":
                        examples = _module_level_string(
                            os.path.join(type_dir, filename), "EXAMPLES")
                        if examples:
                            entry["examples"] = examples

            found.append(entry)
    return found


def enumerate_rulebooks(collection_dir: str) -> list[dict]:
    """List EDA rulebooks at the conventional extensions/eda/rulebooks/ location."""
    rulebooks: list[dict] = []
    rulebooks_root = os.path.join(collection_dir, "extensions", "eda", "rulebooks")
    if not os.path.isdir(rulebooks_root):
        return rulebooks

    for filename in sorted(os.listdir(rulebooks_root)):
        if not filename.endswith((".yml", ".yaml")):
            continue
        rulebooks.append({
            "name": os.path.splitext(filename)[0],
            "path": f"extensions/eda/rulebooks/{filename}",
        })
    return rulebooks


# ---------------------------------------------------------------------------
# Collection discovery
# ---------------------------------------------------------------------------

def collection_metadata(collection_dir: str, fqcn: str,
                        diagnostics: Diagnostics) -> dict:
    """
    Read collection metadata, preferring MANIFEST.json over galaxy.yml.

    Installed collections carry MANIFEST.json; collections mounted from source carry
    only galaxy.yml. Supporting both means this works for a development EE as well as
    a released one.
    """
    manifest = _read_json(os.path.join(collection_dir, "MANIFEST.json"))
    if manifest and isinstance(manifest.get("collection_info"), dict):
        return manifest["collection_info"]

    galaxy = _read_yaml(os.path.join(collection_dir, "galaxy.yml"))
    if galaxy:
        return galaxy

    diagnostics.add("warning",
                    "No MANIFEST.json or galaxy.yml; version and metadata unavailable.",
                    fqcn)
    return {}


def find_collections(collections_path: str, diagnostics: Diagnostics) -> list[tuple]:
    """Yield (namespace, name, directory) for every installed collection."""
    root = os.path.join(collections_path, "ansible_collections")
    if not os.path.isdir(root):
        diagnostics.add("warning",
                        f"No ansible_collections directory under {collections_path}.")
        return []

    found = []
    for namespace in sorted(os.listdir(root)):
        namespace_dir = os.path.join(root, namespace)
        if not os.path.isdir(namespace_dir) or namespace.startswith("."):
            continue
        for name in sorted(os.listdir(namespace_dir)):
            collection_dir = os.path.join(namespace_dir, name)
            if not os.path.isdir(collection_dir) or name.startswith("."):
                continue
            found.append((namespace, name, collection_dir))
    return found


def ansible_core_version() -> str | None:
    try:
        from ansible.release import __version__ as core_version
        return core_version
    except Exception:  # noqa: BLE001 - any failure means "unknown"
        return None


def python_packages() -> dict:
    packages: dict[str, str] = {}
    try:
        from importlib.metadata import distributions
        for dist in distributions():
            name = dist.metadata["Name"] if dist.metadata else None
            if name:
                packages[name] = dist.version
    except Exception:  # noqa: BLE001
        return {}
    return dict(sorted(packages.items()))


# ---------------------------------------------------------------------------
# Manifest assembly
# ---------------------------------------------------------------------------

def generate_manifest(collections_path: str, docs_mode: str = "full") -> dict:
    diagnostics = Diagnostics()

    dump = ansible_doc_dump(collections_path, diagnostics)
    grouped = group_plugins_by_collection(dump, docs_mode) if dump else {}

    collections: list[dict] = []
    for namespace, name, collection_dir in find_collections(collections_path, diagnostics):
        fqcn = f"{namespace}.{name}"
        info = collection_metadata(collection_dir, fqcn, diagnostics)

        plugins = grouped.get(fqcn)
        if plugins is None:
            plugins = filesystem_plugins(collection_dir, fqcn)
            if plugins:
                diagnostics.add(
                    "info",
                    "Plugins listed without documentation; ansible-doc reported nothing "
                    "for this collection.",
                    fqcn)

        entry = {
            "namespace": namespace,
            "name": name,
            "version": info.get("version") or "unknown",
            "plugins": plugins,
            "roles": enumerate_roles(collection_dir, fqcn, docs_mode),
            "playbooks": enumerate_playbooks(collection_dir, fqcn),
            "edaPlugins": enumerate_eda_plugins(collection_dir, fqcn, docs_mode,
                                                diagnostics),
            "rulebooks": enumerate_rulebooks(collection_dir),
        }

        for key in ("repository", "documentation", "license", "dependencies",
                    "description", "authors", "tags"):
            value = info.get(key)
            if value:
                entry[key] = value

        if entry["version"] == "unknown":
            diagnostics.add("warning", "Collection version could not be determined.", fqcn)

        collections.append(entry)

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "generatedBy": {
            "tool": TOOL_NAME,
            "version": SCHEMA_VERSION,
            "ansibleCore": ansible_core_version(),
            "pythonVersion": (f"{sys.version_info.major}.{sys.version_info.minor}."
                              f"{sys.version_info.micro}"),
            "docsMode": docs_mode,
            "docsSource": "ansible-doc" if dump else "filesystem",
        },
        "collections": collections,
        "environment": {
            "pythonPackages": python_packages(),
            "baseImage": os.environ.get("EE_BASE_IMAGE"),
        },
        "diagnostics": diagnostics.entries,
        "complete": bool(dump) and not diagnostics.has_errors,
    }


def manifest_labels(manifest: dict) -> dict:
    """
    Summarise a manifest as OCI image labels.

    Labels are the fallback for registries that cannot serve OCI referrers. They carry
    a summary only: label space is limited and some registries truncate, so the full
    document stays in the referrer and in the image.
    """
    collections = manifest.get("collections", [])
    summary = ",".join(
        f"{c['namespace']}.{c['name']}:{c.get('version', 'unknown')}"
        for c in collections
    )
    generated_by = manifest.get("generatedBy", {})
    labels = {
        "io.ansible.content.manifest": "true",
        "io.ansible.content.manifest.schema": manifest.get("schemaVersion", SCHEMA_VERSION),
        "io.ansible.content.manifest.path": DEFAULT_OUTPUT,
        "io.ansible.content.collections.count": str(len(collections)),
        "io.ansible.content.generated_by": generated_by.get("tool", TOOL_NAME),
        "io.ansible.content.generated_at": manifest.get("generatedAt", ""),
    }
    if summary:
        labels["io.ansible.content.collections"] = summary
    if generated_by.get("ansibleCore"):
        labels["io.ansible.content.ansible_core"] = generated_by["ansibleCore"]
    return labels


def summarise(manifest: dict) -> str:
    collections = manifest["collections"]
    by_type: dict[str, int] = {}
    roles = playbooks = eda = rulebooks = 0
    for collection in collections:
        for plugin in collection["plugins"]:
            by_type[plugin["type"]] = by_type.get(plugin["type"], 0) + 1
        roles += len(collection["roles"])
        playbooks += len(collection["playbooks"])
        eda += len(collection["edaPlugins"])
        rulebooks += len(collection["rulebooks"])

    parts = [f"{len(collections)} collections"]
    for plugin_type in sorted(by_type):
        parts.append(f"{by_type[plugin_type]} {plugin_type}")
    if roles:
        parts.append(f"{roles} roles")
    if playbooks:
        parts.append(f"{playbooks} playbooks")
    if eda:
        parts.append(f"{eda} eda plugins")
    if rulebooks:
        parts.append(f"{rulebooks} rulebooks")
    return ", ".join(parts)


def cmd_generate(args: argparse.Namespace) -> int:
    manifest = generate_manifest(args.collections_path, args.docs)

    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")

    if not args.quiet:
        size_kb = os.path.getsize(args.output) / 1024
        core = manifest["generatedBy"].get("ansibleCore") or "unknown"
        print(f"Wrote {args.output} ({size_kb:.0f} KiB)")
        print(f"  {summarise(manifest)}")
        print(f"  ansible-core {core}, docs from "
              f"{manifest['generatedBy']['docsSource']} ({args.docs})")
        for entry in manifest["diagnostics"]:
            print(f"  [{entry['level']}] {entry.get('collection', '-')}: "
                  f"{entry['message']}", file=sys.stderr)
    return 0


def cmd_labels(args: argparse.Namespace) -> int:
    manifest = _read_json(args.manifest)
    if manifest is None:
        print(f"Cannot read manifest at {args.manifest}", file=sys.stderr)
        return 1
    for key, value in manifest_labels(manifest).items():
        print(f"{key}={value}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="content_manifest.py",
        description="Generate an Ansible content manifest from inside an execution "
                    "environment.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate", help="Enumerate installed content and write the manifest.")
    generate.add_argument("--output", default=DEFAULT_OUTPUT,
                          help=f"Manifest path (default: {DEFAULT_OUTPUT})")
    generate.add_argument("--collections-path", default=DEFAULT_COLLECTIONS_PATH,
                          help=f"Collections root (default: {DEFAULT_COLLECTIONS_PATH})")
    generate.add_argument("--docs", choices=("full", "summary", "none"), default="full",
                          help="How much documentation to embed (default: full)")
    generate.add_argument("--quiet", action="store_true", help="Suppress the summary.")
    generate.set_defaults(func=cmd_generate)

    labels = subparsers.add_parser(
        "labels", help="Print OCI labels summarising an existing manifest.")
    labels.add_argument("--manifest", default=DEFAULT_OUTPUT,
                        help=f"Manifest path (default: {DEFAULT_OUTPUT})")
    labels.set_defaults(func=cmd_labels)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
