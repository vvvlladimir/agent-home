#!/usr/bin/env python3
"""Offline checks of the add-on packaging (`make addon-lint`).

CI additionally runs the upstream app linter and hadolint; this script catches the mistakes we
can catch without network, including the ones this project is specifically exposed to.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REQUIRED = ("name", "version", "slug", "description", "arch")
SCHEMA_TYPE = re.compile(
    r"^(str|bool|int|float|email|url|port|password|device)(\([^)]*\))?\??$|^(list|match)\(.+\)\??$"
)


def lint(addon: Path) -> list[str]:
    errors: list[str] = []
    config = yaml.safe_load((addon / "config.yaml").read_text())

    for key in REQUIRED:
        if key not in config:
            errors.append(f"config.yaml: missing required key `{key}`")
    if not isinstance(config.get("version"), str):
        errors.append("config.yaml: version must be a quoted string")
    if not re.fullmatch(r"[a-z0-9_.\-]+", str(config.get("slug", ""))):
        errors.append("config.yaml: slug may only contain a-z, 0-9, _ . -")
    if set(config.get("arch", [])) - {"aarch64", "amd64"}:
        errors.append("config.yaml: only aarch64 and amd64 are supported (others are deprecated)")
    if "aarch64" not in config.get("arch", []):
        errors.append("config.yaml: aarch64 is required (the target is a Raspberry Pi 5)")

    # safety-relevant settings of this project
    if config.get("ingress") is not True:
        errors.append("config.yaml: ingress must be true")
    if config.get("ports"):
        errors.append("config.yaml: do not publish ports; the GUI is reachable through ingress only")
    if config.get("homeassistant_api") is not True:
        errors.append("config.yaml: homeassistant_api must be true (SUPERVISOR_TOKEN for Core API)")
    for forbidden in ("host_network", "privileged", "full_access", "docker_api", "apparmor"):
        if forbidden in config and config[forbidden] not in (
            False,
            None,
            True if forbidden == "apparmor" else False,
        ):
            errors.append(f"config.yaml: `{forbidden}` must not be enabled")
    if config.get("hassio_role") in ("manager", "admin"):
        errors.append("config.yaml: hassio_role manager/admin is not needed")
    if config.get("init") is not False:
        errors.append("config.yaml: init must be false for s6-overlay base images")
    for entry in config.get("map", []):
        if not isinstance(entry, dict) or "type" not in entry:
            errors.append(f"config.yaml: map entries must be `- type: ...` dicts, got {entry!r}")
        elif entry.get("read_only") is False:
            errors.append(f"config.yaml: map `{entry['type']}` must stay read-only")

    options, schema = config.get("options", {}), config.get("schema", {})
    for key in options:
        if key not in schema:
            errors.append(f"config.yaml: option `{key}` has no schema entry")
    for key, type_ in schema.items():
        if isinstance(type_, list) and len(type_) == 1 and isinstance(type_[0], str):
            # a list option: one element type, the default is a list in options
            if not SCHEMA_TYPE.match(type_[0]):
                errors.append(f"config.yaml: schema `{key}` has an unknown element type {type_[0]!r}")
            elif not isinstance(options.get(key, []), list):
                errors.append(f"config.yaml: list option `{key}` needs a list default")
            continue
        if not isinstance(type_, str) or not SCHEMA_TYPE.match(type_):
            errors.append(f"config.yaml: schema `{key}` has an unknown type {type_!r}")
        elif not type_.endswith("?") and key not in options:
            errors.append(f"config.yaml: required option `{key}` has no default in options")
        if re.search(r"key|token|secret|password", key) and not str(type_).startswith("password"):
            errors.append(f"config.yaml: `{key}` looks like a secret and must use the password type")
        if re.search(r"key|token|secret|password", key) and options.get(key):
            errors.append(f"config.yaml: secret `{key}` must not have a default value")

    translations = yaml.safe_load((addon / "translations" / "en.yaml").read_text()).get("configuration", {})
    for key in schema:
        if key not in translations:
            errors.append(f"translations/en.yaml: option `{key}` is not described")
    for key in translations:
        if key not in schema:
            errors.append(f"translations/en.yaml: `{key}` is not an option")

    for name in (
        "Dockerfile",
        "run.sh",
        "DOCS.md",
        "CHANGELOG.md",
        "icon.png",
        "logo.png",
        "requirements.txt",
    ):
        if not (addon / name).is_file():
            errors.append(f"missing file: {name}")
    if (addon / "build.yaml").exists() or (addon / "build.json").exists():
        errors.append(
            "build.yaml is deprecated since Supervisor 2026.04.0; name the base image in the Dockerfile"
        )

    dockerfile = (addon / "Dockerfile").read_text()
    from_lines = re.findall(r"^FROM\s+(\S+)", dockerfile, re.M)
    if not from_lines or any("BUILD_FROM" in line for line in from_lines):
        errors.append("Dockerfile: FROM must name the image (BUILD_FROM is no longer passed)")
    if any(line.endswith(":latest") or ":" not in line for line in from_lines):
        errors.append("Dockerfile: pin the base image tag")
    run_sh = (addon / "run.sh").read_text()
    if not run_sh.startswith("#!/usr/bin/with-contenv"):
        errors.append(
            "run.sh: shebang must be with-contenv, otherwise SUPERVISOR_TOKEN is not in the environment"
        )
    if str(config.get("ingress_port", 8099)) not in run_sh:
        errors.append("run.sh: does not listen on ingress_port")

    version_py = re.search(r'__version__ = "([^"]+)"', (addon / "app" / "__init__.py").read_text())
    if not version_py or version_py.group(1) != config.get("version"):
        errors.append("version mismatch between config.yaml and app/__init__.py")
    if f"## {config.get('version')}" not in (addon / "CHANGELOG.md").read_text():
        errors.append(f"CHANGELOG.md has no entry for {config.get('version')}")

    repo = addon.parent / "repository.yaml"
    if not repo.is_file() or "name" not in (yaml.safe_load(repo.read_text()) or {}):
        errors.append("repository.yaml with a `name` is required at the repository root")
    return errors


def main() -> int:
    addon = Path(sys.argv[1] if len(sys.argv) > 1 else "agent_house")
    errors = lint(addon)
    for error in errors:
        print(f"ERROR {error}")
    print(f"addon-lint: {len(errors)} problem(s) in {addon}/")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
