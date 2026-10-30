"""The stack: which revision of each measured library the campaigns measure.

`stack/stack-pins.yaml` is the one place a version is decided; this module loads it, checks it
against `schemas/stack-pins.schema.json` and the cross-references a schema cannot express, and
reads back what a benchmark repository actually carries, so `campaign-sync-pins` can write and
check the pin files and `campaign-stack-status` can report them. The package holds no library
names: everything specific is in the stack file.
"""
from __future__ import annotations

import json
import pathlib
import re

import yaml

from . import descriptor, jsonschema_lite


class StackError(Exception):
    """A stack file that is invalid, or a request the stack cannot answer."""


def default_stack_path() -> pathlib.Path:
    """The stack shipped with the installed toolkit (a test keeps it equal to `stack/` at the root)."""
    return pathlib.Path(__file__).resolve().parent / "stack" / "stack-pins.yaml"


def validate(stack) -> list:
    """Problems with a loaded stack: the schema first, then the references between its parts."""
    schema = json.loads((descriptor.schemas_dir() / "stack-pins.schema.json").read_text())
    try:
        jsonschema_lite.validate(stack, schema)
    except jsonschema_lite.ValidationError as exc:
        return [f"{exc.json_path}: {exc.message}"]
    problems = []
    names = [r["name"] for r in stack["repositories"]]
    for name in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"repository {name!r} is declared more than once")
    for repo in stack["repositories"]:
        for use in repo["consumes"]:
            where = f"{repo['name']}: {use['file']}"
            library = stack["libraries"].get(use["library"])
            if library is None:
                problems.append(f"{where}: unknown library {use['library']!r}")
            elif library[use["field"]] is None:
                problems.append(f"{where}: library {use['library']!r} has no {use['field']}")
            if use["kind"] == "rev-file" and not (use["file"].startswith("pins/") and use["file"].endswith(".rev")):
                problems.append(f"{where}: a rev-file lives at pins/<name>.rev")
            if use["kind"] == "workflow-env" and "key" not in use:
                problems.append(f"{where}: a workflow-env pin names its `key`")
    return problems


def load(path=None) -> dict:
    path = pathlib.Path(path) if path else default_stack_path()
    try:
        stack = yaml.safe_load(path.read_text())
    except OSError as exc:
        raise StackError(f"cannot read the stack file {path}: {exc}") from exc
    problems = validate(stack)
    if problems:
        raise StackError(f"{path} is not a valid stack:\n  " + "\n  ".join(problems))
    return stack


def repository(stack, name):
    return next((r for r in stack["repositories"] if r["name"] == name), None)


def expected(stack, use) -> str:
    return stack["libraries"][use["library"]][use["field"]]


def rev_file_value(text: str) -> str:
    """The value of a pin file: `#` comments are ignored and the last other line is the value."""
    value = ""
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            value = line
    return value


def _norm_url(url: str) -> str:
    return re.sub(r"(\.git)?/*$", "", url.strip()).lower()


def observed(stack, use, root) -> list | None:
    """The values the repository carries for this pin, or None when its file is absent.

    A list, because a workflow or a Cargo manifest can name the revision more than once; the
    pin matches only when there is at least one and all of them equal the stack's.
    """
    path = pathlib.Path(root) / use["file"]
    if not path.is_file():
        return None
    text = path.read_text()
    if use["kind"] == "rev-file":
        return [rev_file_value(text)]
    if use["kind"] == "workflow-env":
        return re.findall(rf"^\s*{re.escape(use['key'])}\s*:\s*['\"]?([^\s'\"#]+)", text, re.M)
    url = _norm_url(stack["libraries"][use["library"]]["repo"])
    found = []
    for line in text.splitlines():
        git = re.search(r'git\s*=\s*"([^"]+)"', line)
        rev = re.search(r'rev\s*=\s*"([^"]+)"', line)
        if git and rev and _norm_url(git.group(1)) == url:
            found.append(rev.group(1))
    return found


def compare(stack, repo, root) -> list:
    """One `(use, want, found, ok)` per pin the repository carries; `found` None means no file."""
    results = []
    for use in repo["consumes"]:
        want = expected(stack, use)
        found = observed(stack, use, root)
        results.append((use, want, found, bool(found) and all(v == want for v in found)))
    return results


def short(value) -> str:
    return value[:7] if re.fullmatch(r"[0-9a-f]{40}", value or "") else (value or "")


def tag_commit(repo: str, tag: str):
    """The commit `tag` names on `repo` (an annotated tag is peeled), or None when there is no such tag."""
    import subprocess
    out = subprocess.run(["git", "ls-remote", "--tags", repo, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
                         capture_output=True, text=True, check=True, timeout=120).stdout
    refs = dict(reversed(line.split("\t")) for line in out.splitlines() if "\t" in line)
    return refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")


def crate_commit(crate: str, version: str):
    """`(exists, sha1)` for a crates.io release: sha1 is the archive's `.cargo_vcs_info.json`
    commit, None when the archive records none."""
    import io
    import tarfile
    import urllib.error
    import urllib.request
    url = f"https://static.crates.io/crates/{crate}/{crate}-{version}.crate"
    request = urllib.request.Request(url, headers={"User-Agent": "campaign-toolkit (stack --remote)"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            return False, None
        raise
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        try:
            member = archive.extractfile(f"{crate}-{version}/.cargo_vcs_info.json")
        except KeyError:
            return True, None
        return True, json.load(member)["git"]["sha1"]


def remote_problems(stack, tag_commit=tag_commit, crate_commit=crate_commit) -> list:
    """What the published world disagrees with: a declared tag that is missing or names another
    commit, and a declared crate version that is not on crates.io or was published from another
    commit. A library without a tag or version is not checked for it."""
    problems = []
    for name, lib in stack["libraries"].items():
        if lib["tag"]:
            found = tag_commit(lib["repo"], lib["tag"])
            if found is None:
                problems.append(f"{name}: tag {lib['tag']} not found on {lib['repo']}")
            elif found != lib["commit"]:
                problems.append(f"{name}: tag {lib['tag']} is {found}, stack says {lib['commit']}")
        if lib["crate"] and lib["version"]:
            exists, sha1 = crate_commit(lib["crate"], lib["version"])
            if not exists:
                problems.append(f"{name}: {lib['crate']} {lib['version']} is not on crates.io")
            elif sha1 is not None and sha1 != lib["commit"]:
                problems.append(f"{name}: {lib['crate']} {lib['version']} was published from {sha1}, "
                                f"stack says {lib['commit']}")
    return problems


def main(argv=None) -> int:
    """`python -m campaign_toolkit.stack [--remote] [FILE]`: validate a stack file (default: the
    shipped one); `--remote` also checks every tag and crates.io version against its commit."""
    import sys
    argv = list(argv or [])
    remote = "--remote" in argv
    argv = [a for a in argv if a != "--remote"]
    try:
        stack = load(argv[0] if argv else None)
    except StackError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    print(f"stack ok: {len(stack['libraries'])} librar(ies), {len(stack['repositories'])} repositories")
    if remote:
        problems = remote_problems(stack)
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        if problems:
            return 1
        print("remote ok: every tag and crates.io version matches its commit")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
