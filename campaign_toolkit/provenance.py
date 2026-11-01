"""Check whether a measured commit belongs to main and to a released history."""
from dataclasses import dataclass
import pathlib
import re
import subprocess

from . import stack


class ProvenanceError(Exception):
    """The repository or its history could not be verified."""


@dataclass(frozen=True)
class RevisionProvenance:
    on_main: bool
    in_release: bool


def check_stack_revision(checkout, revision, main_ref, library):
    """Check a commit against main and the release recorded in the installed stack."""
    try:
        entry = stack.load()["libraries"][library]
        release = entry["commit"]
    except (stack.StackError, KeyError, TypeError) as exc:
        raise ProvenanceError(f"cannot load release commit for {library!r}: {exc}") from exc
    if not (entry.get("tag") or entry.get("version")):
        raise ProvenanceError(f"{library!r} has no release recorded in the stack")
    return check_revision(checkout, revision, main_ref, release)


def check_revision(checkout, revision, main_ref, release_revision):
    """Check ancestry in a complete checkout; unknown history is an error, not a refusal.

    The caller supplies an authoritative, freshly fetched main ref and the release commit
    from the stack. A non-ancestor is a verified result; missing objects are not.
    """
    checkout = pathlib.Path(checkout)
    for name, value in (("revision", revision), ("release revision", release_revision)):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", value):
            raise ProvenanceError(f"{name} must be a full 40-character commit hash")
    if not isinstance(main_ref, str) or not main_ref.startswith("refs/"):
        raise ProvenanceError("main ref must be a fully qualified Git ref")

    def git(*args):
        try:
            return subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True)
        except OSError as exc:
            raise ProvenanceError(f"cannot run git in {checkout}: {exc}") from exc

    shallow = git("rev-parse", "--is-shallow-repository")
    if shallow.returncode or shallow.stdout.strip() != "false":
        raise ProvenanceError(f"cannot verify complete Git history in {checkout}; use a full checkout")
    for name, value in (("revision", revision), ("main ref", main_ref), ("release revision", release_revision)):
        result = git("rev-parse", "--verify", f"{value}^{{commit}}")
        if result.returncode:
            raise ProvenanceError(f"cannot resolve {name} {value} in {checkout}: {result.stderr.strip()}")

    def is_ancestor(ancestor, descendant):
        result = git("merge-base", "--is-ancestor", ancestor, descendant)
        if result.returncode not in (0, 1):
            raise ProvenanceError(f"cannot verify ancestry in {checkout}: {result.stderr.strip()}")
        return result.returncode == 0

    return RevisionProvenance(on_main=is_ancestor(revision, main_ref),
                              in_release=is_ancestor(revision, release_revision))
