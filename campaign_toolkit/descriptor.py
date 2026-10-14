"""The per-campaign descriptor: every campaign-specific constant in one file.

The toolkit's seven entry points are campaign-agnostic. What a campaign *is* --
its measured project, its vendor arms, its engine names, where its results live,
how a published page is named and what its report header says -- comes from
`benchmarks/campaign.yaml` at the campaign root, validated against
`schemas/campaign.schema.json` before any of it is used.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import yaml

from . import jsonschema_lite

DEFAULT_RELPATH = "benchmarks/campaign.yaml"
SCHEMA_RELPATH = "schemas/campaign.schema.json"

_current: "Descriptor | None" = None


def descriptor_path(root) -> pathlib.Path:
    """The descriptor file a campaign names, defaulting to `<root>/benchmarks/campaign.yaml`.

    `CAMPAIGN_DESCRIPTOR` overrides it, as an absolute path or one relative to the
    campaign root, for a layout that keeps the descriptor elsewhere.
    """
    root = pathlib.Path(root).resolve()
    named = os.environ.get("CAMPAIGN_DESCRIPTOR")
    if named:
        named = pathlib.Path(named)
        return named if named.is_absolute() else root / named
    return root / DEFAULT_RELPATH


class DescriptorError(SystemExit):
    """A missing or invalid descriptor; a campaign cannot run without one."""


def profiles_dir() -> pathlib.Path:
    """Where the toolkit's shared hardware profile library lives.

    A profile is a fact about a machine, not about a campaign, so the library is
    shared here and a campaign's `benchmarks/profiles.yaml` *names* the profiles
    it measures.
    """
    return pathlib.Path(__file__).resolve().parent / "profiles"


def shared_profile(name: str) -> dict:
    """One profile from the toolkit's library, by name."""
    path = profiles_dir() / f"{name}.yaml"
    if not path.exists():
        raise DescriptorError(f"ERROR: no shared profile {name!r} in {profiles_dir()}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise DescriptorError(f"ERROR: {path} does not hold a mapping")
    return data


def schemas_dir() -> pathlib.Path:
    """Where the toolkit's JSON Schemas live.

    An editable install keeps the source tree, so the repository's top-level
    `schemas/` is found next to the package. `CAMPAIGN_TOOLKIT_SCHEMAS` overrides
    it for a layout where that is not true.
    """
    env = os.environ.get("CAMPAIGN_TOOLKIT_SCHEMAS")
    if env:
        return pathlib.Path(env).resolve()
    package = pathlib.Path(__file__).resolve().parent
    for candidate in (package / "schemas", package.parent / "schemas"):
        if candidate.is_dir():
            return candidate
    return package / "schemas"


class Descriptor:
    def __init__(self, root: pathlib.Path, data: dict, path: pathlib.Path):
        self.root = pathlib.Path(root).resolve()
        self.data = data
        self.path = pathlib.Path(path)

    # ---- generic access -------------------------------------------------
    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    def resolve(self, relative: str) -> pathlib.Path:
        return (self.root / relative).resolve()

    # ---- identity -------------------------------------------------------
    @property
    def identities(self) -> list:
        return list(self.data.get("identities", ["library"]))

    @property
    def has_upstream(self) -> bool:
        return "upstream" in self.identities

    @property
    def measured(self) -> dict:
        return self.data["measured"]

    @property
    def upstream(self) -> dict:
        return self.data.get("upstream") or {}

    def identity(self, name: str) -> dict:
        return self.measured if name == "library" else self.upstream

    def pin_file(self, identity: str = "library") -> pathlib.Path:
        return self.resolve(self.identity(identity)["pin_file"])

    def checkout_dir(self, identity: str = "library") -> pathlib.Path:
        return self.resolve(self.identity(identity)["extern_dir"])

    # ---- declarations ---------------------------------------------------
    @property
    def profiles_file(self) -> pathlib.Path:
        return self.resolve(self.data["profiles_file"])

    def profiles(self) -> list:
        """The hardware profiles this campaign measures.

        An entry of `profiles.yaml` is either a mapping -- a machine only this
        campaign uses -- or a name, resolved against the toolkit's shared library.
        """
        declared = yaml.safe_load(self.profiles_file.read_text())
        return [shared_profile(entry) if isinstance(entry, str) else entry
                for entry in declared["profiles"]]

    def profile_names(self) -> list:
        return [p["name"] for p in self.profiles()]

    @property
    def suites_dir(self) -> pathlib.Path:
        return self.resolve(self.data["suites_dir"])

    def suite_path(self, suite_id: str) -> pathlib.Path:
        return self.suites_dir / f"{suite_id}.yaml"

    @property
    def data_dir(self) -> pathlib.Path:
        return self.resolve(self.data["data_dir"])

    @property
    def result_dir(self) -> pathlib.Path:
        return self.resolve(self.data["result_dir"])

    @property
    def index_path(self) -> pathlib.Path:
        return self.result_dir / "INDEX.md"

    @property
    def default_invalidated_by(self) -> list:
        return list(self.data.get("default_invalidated_by", []))

    @property
    def page_name(self) -> str:
        return self.data["page_name"]

    # ---- measurement ----------------------------------------------------
    @property
    def engines(self) -> dict:
        return dict(self.data["engines"])

    @property
    def vendor_providers(self) -> tuple:
        return tuple(self.data.get("vendor_providers", []))

    @property
    def measured_providers(self) -> tuple:
        seen = []
        for provider in self.engines.values():
            if provider not in self.vendor_providers and provider not in seen:
                seen.append(provider)
        return tuple(seen)

    @property
    def provider_packages(self) -> tuple:
        return tuple(self.measured.get("provider_packages", []))

    @property
    def vendor_env(self) -> tuple:
        return tuple(self.data.get("vendor_env", []))

    @property
    def csv_columns(self) -> list:
        return list(self.data.get("csv_columns", []))

    # ---- report / presentation -----------------------------------------
    @property
    def report(self) -> dict:
        return self.data.get("report", {})

    def report_field(self, name: str) -> str:
        return self.report[name]


def load_descriptor(root) -> Descriptor:
    root = pathlib.Path(root).resolve()
    path = descriptor_path(root)
    if not path.exists():
        raise DescriptorError(f"ERROR: {path} not found; a campaign needs its descriptor")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise DescriptorError(f"ERROR: {path} is not valid YAML: {exc}")
    if not isinstance(data, dict):
        raise DescriptorError(f"ERROR: {path} does not hold a mapping")
    schema_path = schemas_dir() / "campaign.schema.json"
    try:
        schema = yaml.safe_load(schema_path.read_text())
    except OSError:
        raise DescriptorError(f"ERROR: toolkit schema {schema_path} is missing")
    try:
        jsonschema_lite.validate(data, schema)
    except jsonschema_lite.ValidationError as exc:
        raise DescriptorError(f"ERROR: {path}: {exc.json_path}: {exc.message}")
    return Descriptor(root, data, path)


def set_root(root) -> Descriptor:
    global _current
    _current = load_descriptor(root)
    return _current


def current() -> Descriptor:
    if _current is None:
        env = os.environ.get("CAMPAIGN_ROOT")
        set_root(env or pathlib.Path.cwd())
    return _current


def root() -> pathlib.Path:
    return current().root


def run_module(module: str, args, root=None, capture=False):
    """Run another toolkit entry point against the same campaign root.

    The seven entry points call one another (record -> validate -> report ->
    publish -> index). Going back through `python -m` rather than importing keeps
    each one's argument parsing and exit status the single place that decides what
    it did, and carries the campaign root across the process boundary in the
    environment.
    """
    root = pathlib.Path(root or current().root)
    env = dict(os.environ, CAMPAIGN_ROOT=str(root))
    # Run from the toolkit's source root so `python -m campaign_toolkit.<name>`
    # resolves whether or not the package has been installed.
    toolkit_root = pathlib.Path(__file__).resolve().parent.parent
    return subprocess.run([sys.executable, "-m", f"campaign_toolkit.{module}", *args],
                          env=env, cwd=str(toolkit_root), text=True, capture_output=capture)
