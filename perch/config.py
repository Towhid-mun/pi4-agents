"""C1 - config resolution.

Resolves project settings from a `.perch.toml` discovered by walking up from
the working directory. Connection detail is deferred entirely to ~/.ssh/config:
this module knows an alias and nothing else (I10).

This module is the only place that derives the (local_root, remote_root) pair.
C5 consumes it for path rewriting; nothing else may compute it.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from perch.errors import ConfigError

CONFIG_FILENAME = ".perch.toml"

# Verbs in C7 that map to a configured command string.
COMMAND_VERBS = ("build", "test", "run")

# Always excluded from the mirror, regardless of config (C3). The tool's own
# config file is here because the target has no use for it and a stray copy
# would be discovered by a walk-up on the target side.
BUILTIN_EXCLUDES = (
    ".git/",
    ".venv/",
    "__pycache__/",
    "node_modules/",
    "*.o",
    "*.pyc",
    ".DS_Store",
    CONFIG_FILENAME,
)

_TOP_LEVEL_KEYS = frozenset({"host", "remote_root", "exclude", "artifacts", "commands"})


@dataclass(frozen=True)
class Config:
    """Resolved project settings.

    MUST NOT carry hostnames, ports, users, keys or passwords - only the ssh
    alias, which ssh itself resolves (I10).
    """

    host: str
    remote_root: str  # path on target, relative to $HOME unless absolute
    local_root: Path  # absolute path on host
    commands: dict[str, str]  # "build" | "test" | "run" -> shell command
    exclude: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()  # globs auto-pulled after a successful run
    source: Path | None = field(default=None, compare=False)  # the .perch.toml

    @property
    def roots(self) -> tuple[Path, str]:
        """The (local_root, remote_root) pair. The single source of this pair.

        C5 rewrites target paths to host paths using exactly this. No other
        component derives it (ARCHITECTURE.md §5/C1).
        """
        return (self.local_root, self.remote_root)

    @property
    def all_excludes(self) -> tuple[str, ...]:
        """Built-in excludes followed by the project's own, in that order."""
        return BUILTIN_EXCLUDES + self.exclude

    def command_for(self, verb: str) -> str:
        """The shell command for a verb, or a config error naming what is missing."""
        try:
            return self.commands[verb]
        except KeyError:
            raise ConfigError(
                f"{self._where()}: no command configured for '{verb}'. "
                f"Add it under [commands]:\n\n    [commands]\n    {verb} = \"make\"\n"
            ) from None

    def _where(self) -> str:
        return str(self.source) if self.source else "<config>"


def find_config_file(start: Path) -> Path:
    """Walk up from `start` looking for .perch.toml.

    Raises ConfigError naming the expected path when there is none.
    """
    start = Path(start).resolve()
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    raise ConfigError(
        f"no {CONFIG_FILENAME} found in {start} or any parent directory. "
        f"Expected one at {start / CONFIG_FILENAME}."
    )


def load(start: Path | None = None) -> Config:
    """Discover, parse and validate the project config."""
    path = find_config_file(Path.cwd() if start is None else Path(start))
    return load_file(path)


def load_file(path: Path) -> Config:
    """Parse and validate a specific config file."""
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read config file: {exc.strerror}") from exc

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: config file is not valid UTF-8: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: malformed TOML: {exc}") from exc

    return _validate(data, path)


def _validate(data: dict, path: Path) -> Config:
    _reject_unknown(data, _TOP_LEVEL_KEYS, path, context="")

    host = _require_str(data, "host", path)
    if not host.strip():
        raise ConfigError(f"{path}: key 'host' must be a non-empty ssh alias")
    if any(c in host for c in " \t@:/"):
        # An alias, not a connection string - ssh_config owns user, port and
        # address (I10). Catching this here keeps credentials out of config.
        raise ConfigError(
            f"{path}: key 'host' must be a bare ssh alias from ~/.ssh/config, "
            f"not a connection string (got {host!r}). "
            f"Put the user, port and address in ~/.ssh/config."
        )

    local_root = path.parent

    remote_root = data.get("remote_root")
    if remote_root is None:
        remote_root = f"perch/{local_root.name}"
    elif not isinstance(remote_root, str):
        raise ConfigError(
            f"{path}: key 'remote_root' must be a string, got {_typename(remote_root)}"
        )
    remote_root = remote_root.rstrip("/")
    if not remote_root:
        raise ConfigError(f"{path}: key 'remote_root' must not be empty or '/'")
    if ".." in remote_root.split("/"):
        raise ConfigError(
            f"{path}: key 'remote_root' must not contain '..' (got {remote_root!r})"
        )

    commands = _commands(data, path)
    exclude = _str_list(data, "exclude", path)
    artifacts = _str_list(data, "artifacts", path)

    return Config(
        host=host,
        remote_root=remote_root,
        local_root=local_root,
        commands=commands,
        exclude=exclude,
        artifacts=artifacts,
        source=path,
    )


def _commands(data: dict, path: Path) -> dict[str, str]:
    raw = data.get("commands", {})
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path}: [commands] must be a table, got {_typename(raw)}"
        )
    _reject_unknown(raw, frozenset(COMMAND_VERBS), path, context="commands")
    out = {}
    for verb, command in raw.items():
        if not isinstance(command, str):
            raise ConfigError(
                f"{path}: commands.{verb} must be a string, got {_typename(command)}"
            )
        if not command.strip():
            raise ConfigError(f"{path}: commands.{verb} must not be empty")
        out[verb] = command
    return out


def _str_list(data: dict, key: str, path: Path) -> tuple[str, ...]:
    raw = data.get(key, [])
    if not isinstance(raw, list):
        raise ConfigError(f"{path}: key '{key}' must be an array, got {_typename(raw)}")
    for index, item in enumerate(raw):
        if not isinstance(item, str):
            raise ConfigError(
                f"{path}: key '{key}[{index}]' must be a string, "
                f"got {_typename(item)}"
            )
    return tuple(raw)


def _require_str(data: dict, key: str, path: Path) -> str:
    if key not in data:
        raise ConfigError(f"{path}: missing required key '{key}'")
    value = data[key]
    if not isinstance(value, str):
        raise ConfigError(
            f"{path}: key '{key}' must be a string, got {_typename(value)}"
        )
    return value


def _reject_unknown(data: dict, known: frozenset, path: Path, *, context: str) -> None:
    """A typo in a config key must not fail silently."""
    unknown = sorted(set(data) - known)
    if not unknown:
        return
    where = f"[{context}]" if context else "top level"
    offending = unknown[0]
    suggestion = _nearest(offending, known)
    hint = f" (did you mean '{suggestion}'?)" if suggestion else ""
    plural = "keys" if len(unknown) > 1 else "key"
    names = ", ".join(f"'{k}'" for k in unknown)
    raise ConfigError(
        f"{path}: unknown {plural} at {where}: {names}{hint}. "
        f"Known keys: {', '.join(sorted(known))}."
    )


def _nearest(word: str, candidates) -> str | None:
    """Cheap typo hint: the candidate sharing the most leading characters."""
    best, best_score = None, 0
    for candidate in candidates:
        score = len(_common_prefix(word, candidate))
        if score > best_score:
            best, best_score = candidate, score
    return best if best_score >= 2 else None


def _common_prefix(a: str, b: str) -> str:
    out = []
    for x, y in zip(a, b):
        if x != y:
            break
        out.append(x)
    return "".join(out)


def _typename(value) -> str:
    return type(value).__name__
