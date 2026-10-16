"""The static deny rules: places no grant may ever reach, whatever any grant or the user says.

ARCHITECTURE section 7: credential and key directories, keyrings, browser profiles,
Tumbleweed's own directories and sockets, `$XDG_RUNTIME_DIR` and the system's secrets are
refused. A path is refused when it **equals, is inside, or is an ancestor of** any entry
(`DenyRelation`), so `~` is never grantable: it is an ancestor of `~/.ssh`. The lists are
`HOME_DENY` (relative to the user's home), `SYSTEM_DENY` (absolute) and what `deny_entries`
derives from a `DenyContext` (the XDG directories, the runtime directory and extra entries such
as the broker's socket directory, which the daemon knows and this module does not).

Matching never trusts a name alone (the S01 review, #16). `DenyRules.build` resolves every entry
once and keeps, besides the folded text, the identity of each directory on the way to it
(`paths.ResolvedPath`), so a path that reaches a protected directory through a bind mount, or
by another spelling on a case-insensitive volume (macOS), is the same object and is refused. Text
is compared too, through `paths.fold_name` (case, Unicode normalisation), because an entry
that does not exist yet has no identity to compare, and because a rule that is checked only
by identity is only as good as the moment it was built. So a rule set is rebuilt when a decision
needs a fresh answer, and it is cheap enough for that (some hundred path walks).

An entry is held in two forms: the literal path built for the user (`~/.aws`) and its canonical
form if the two differ (`~/.aws` is a symlink to `/data/aws`): a grant on `/data/aws` is refused,
and so is one on `~/.aws` after the symlink is replaced by a directory. On macOS each is also
held under `/System/Volumes/Data`, where the data volume is mounted and from where `/Users` and
`/private` are the same directories (firmlinks, which are not symlinks).

Limits.

- A stored grant has no identities to compare, only text (`DenyRules.check_text`), so the
  decision-time `check` on a `ResolvedPath` is the authoritative one.
- Identity is that of the objects the entries name. A hard link to a protected *file entry*
  (`~/.netrc`) is that file; a hard link to a file inside a protected directory is not
  recognised, since only the directory is an entry (a sandbox cannot make one: it sees none of
  these directories).
- Folding is Unicode caseless matching, not the file system's own idea of equal names. Where a
  file system equates names that fold apart (some compatibility characters, Turkish dotless i),
  an entry that does not exist when the rules are built and appears later is matched only by
  exact fold.
"""

import logging
import os
import platform
import posixpath
import pwd
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Final, Self, final

from pydantic import Field

from tumbleweed.broker.errors import BrokerError, PathError
from tumbleweed.broker.identity import FsIdentity
from tumbleweed.broker.model import PolicyModel
from tumbleweed.broker.paths import (
    AbsoluteText,
    ResolvedPath,
    StoredPath,
    components_of,
    fold_name,
)
from tumbleweed.core.errors import InvalidValueError

_LOG = logging.getLogger(__name__)

# Relative to the user's home. Credentials and keys, cloud and container tooling, package
# registries, keyrings and password managers, browsers and mail, and shell histories (which hold
# pasted secrets). A directory that does not exist is no problem: it is matched by text.
HOME_DENY: Final = (
    # keys and general credentials
    ".ssh",
    ".gnupg",
    ".pki",
    ".password-store",
    ".netrc",
    "_netrc",
    ".git-credentials",
    ".authinfo",
    ".authinfo.gpg",
    ".vault-token",
    # cloud and infrastructure
    ".aws",
    ".azure",
    ".kube",
    ".oci",
    ".terraform.d",
    ".pulumi",
    ".cloudflared",
    ".fly",
    ".s3cfg",
    ".boto",
    ".databrickscfg",
    ".config/gcloud",
    ".config/doctl",
    ".config/hcloud",
    ".config/helm",
    ".config/gh",
    ".config/glab-cli",
    ".config/op",
    ".config/rclone",
    ".config/sops",
    ".config/pulumi",
    ".config/flyctl",
    ".config/heroku",
    # containers
    ".docker",
    ".config/containers",
    # package managers and registries
    ".npmrc",
    ".yarnrc",
    ".yarnrc.yml",
    ".pypirc",
    ".pip",
    ".config/pip",
    ".cargo/credentials",
    ".cargo/credentials.toml",
    ".gem/credentials",
    ".m2/settings.xml",
    ".m2/settings-security.xml",
    ".gradle/gradle.properties",
    ".composer/auth.json",
    ".config/composer/auth.json",
    ".nuget/NuGet",
    ".config/NuGet",
    # keyrings and password managers
    ".local/share/keyrings",
    ".local/share/gnome-keyring",
    ".gnome2/keyrings",
    ".local/share/kwalletd",
    ".kde/share/apps/kwallet",
    ".kde4/share/apps/kwallet",
    ".config/kwalletrc",
    ".config/1Password",
    ".config/Bitwarden",
    ".config/keepassxc",
    "Library/Keychains",
    # browsers and mail
    ".mozilla",
    ".librewolf",
    ".waterfox",
    ".thunderbird",
    ".config/google-chrome",
    ".config/chromium",
    ".config/BraveSoftware",
    ".config/microsoft-edge",
    ".config/vivaldi",
    ".config/opera",
    ".cache/mozilla",
    ".cache/google-chrome",
    ".cache/chromium",
    ".var/app",
    "snap/firefox",
    "snap/chromium",
    "Library/Application Support/Firefox",
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Chromium",
    "Library/Application Support/BraveSoftware",
    "Library/Application Support/Microsoft Edge",
    "Library/Safari",
    "Library/Cookies",
    "Library/Mail",
    "Library/Messages",
    # shell and tool histories
    ".bash_history",
    ".zsh_history",
    ".zhistory",
    ".python_history",
    ".psql_history",
    ".mysql_history",
    ".sqlite_history",
    ".node_repl_history",
    # Tumbleweed's own directories at their default places (the XDG variables may differ
    # between processes, so the defaults are listed as well as what the environment says)
    ".config/tumbleweed",
    ".local/share/tumbleweed",
    ".local/state/tumbleweed",
    ".cache/tumbleweed",
)

SYSTEM_DENY: Final = (
    "/proc",
    "/sys",
    "/dev",
    "/run/user",  # every user's $XDG_RUNTIME_DIR: sockets, keyring and bus
    "/run/dbus",
    "/run/docker.sock",
    "/var/run/docker.sock",
    "/run/containerd",
    "/run/podman",
    "/run/secrets",
    "/etc/shadow",
    "/etc/shadow-",
    "/etc/gshadow",
    "/etc/gshadow-",
    "/etc/sudoers",
    "/etc/sudoers.d",
    "/etc/ssh",
    "/etc/ssl/private",
    "/etc/security",
    "/etc/krb5.keytab",
    "/etc/master.passwd",
    "/root",
    "/private/etc/master.passwd",
    "/private/var/db",
    "/Library/Keychains",
    "/System/Library/Keychains",
)

_XDG: Final = (
    ("XDG_CONFIG_HOME", ".config"),
    ("XDG_DATA_HOME", ".local/share"),
    ("XDG_STATE_HOME", ".local/state"),
    ("XDG_CACHE_HOME", ".cache"),
)
_DATA_VOLUME: Final = ("system", "volumes", "data")  # folded, as the forms hold them
_MAX_HOMES: Final = 4
_MAX_EXTRA: Final = 64
_LABEL_PATTERN: Final = r"[ -~]{1,64}"  # printable ASCII: a label goes into logs and prompts

Label = Annotated[str, Field(pattern=f"^{_LABEL_PATTERN}$", strict=True)]


class DenyRelation(StrEnum):
    """How a path relates to a deny entry."""

    EQUAL = "equal"
    INSIDE = "inside"  # the path is below the entry
    ANCESTOR = "ancestor"  # the entry is below the path


@final
class DenyMatch(PolicyModel):
    """A deny entry that a path equals, is inside or is an ancestor of. `entry` is the entry's
    label, text of the rule list and not of the path that was refused."""

    entry: Label
    relation: DenyRelation


@final
class ExtraDeny(PolicyModel):
    """An entry the daemon adds to the lists: the broker and shell socket directories, the btrfs
    snapshot subvolume, anything else only it knows the path of."""

    label: Label
    path: AbsoluteText


@final
class DenyContext(PolicyModel):
    """What the deny list is computed from: where this user's home and XDG directories are."""

    homes: Annotated[tuple[AbsoluteText, ...], Field(min_length=1, max_length=_MAX_HOMES)]
    config_home: AbsoluteText
    data_home: AbsoluteText
    state_home: AbsoluteText
    cache_home: AbsoluteText
    runtime_dir: AbsoluteText | None = None
    extra: Annotated[tuple[ExtraDeny, ...], Field(max_length=_MAX_EXTRA)] = ()

    @classmethod
    def for_current_user(
        cls, environ: Mapping[str, str] | None = None, *, extra: Sequence[ExtraDeny] = ()
    ) -> Self:
        """The context of the user this process runs as.

        The home directory is the passwd entry's, and `$HOME` as well if it names another one;
        both are protected. The XDG directories are the environment's if set to an absolute path
        (a relative one is ignored, as the XDG specification says) and the usual places under the
        passwd home otherwise; `$XDG_RUNTIME_DIR` only if set. `BrokerError` if no home can be
        found or a variable holds an absolute path that is not usable.
        """
        env = os.environ if environ is None else environ
        homes: list[str] = []
        try:
            candidates = [pwd.getpwuid(os.getuid()).pw_dir, env.get("HOME", "")]
        except KeyError:  # a uid with no passwd entry, as in some containers
            candidates = [env.get("HOME", "")]
        for candidate in candidates:
            home = _absolute_or_none(candidate, "the home directory")
            if home is not None and home not in homes:
                homes.append(home)
        if not homes:
            raise BrokerError("cannot determine the user's home directory")
        xdg = {
            name: _absolute_or_none(env.get(name, ""), name) or posixpath.join(homes[0], default)
            for name, default in _XDG
        }
        return cls(
            homes=tuple(homes),
            config_home=xdg["XDG_CONFIG_HOME"],
            data_home=xdg["XDG_DATA_HOME"],
            state_home=xdg["XDG_STATE_HOME"],
            cache_home=xdg["XDG_CACHE_HOME"],
            runtime_dir=_absolute_or_none(env.get("XDG_RUNTIME_DIR", ""), "XDG_RUNTIME_DIR"),
            extra=tuple(extra),
        )


def _absolute_or_none(value: str, what: str) -> str | None:
    """`value` normalised if it is an absolute path, None if empty or relative; `BrokerError`
    if it is absolute and still not a usable path."""
    if not value.startswith("/"):
        return None
    normal = "/" + posixpath.normpath(value).lstrip("/")
    try:
        components_of(normal)
    except InvalidValueError:
        raise BrokerError(f"{what} is not a usable absolute path") from None
    return normal


@final
@dataclass(frozen=True, slots=True)
class DenyDefinition:
    """One entry before it is resolved: its label and the literal path built for this user."""

    label: str
    path: str


def _under(root: str, relative: str) -> str:
    return posixpath.join(root, relative)


def deny_entries(context: DenyContext) -> Iterator[DenyDefinition]:
    """Every entry of the deny list for `context`, in the order they are checked.

    Pure: nothing is looked up. An entry may appear more than once (the XDG data directory is
    usually `~/.local/share`); `DenyRules.build` keeps the first.
    """
    for home in context.homes:
        for relative in HOME_DENY:
            yield DenyDefinition(f"~/{relative}", _under(home, relative))
    for variable, root in (
        ("XDG_CONFIG_HOME", context.config_home),
        ("XDG_DATA_HOME", context.data_home),
        ("XDG_STATE_HOME", context.state_home),
        ("XDG_CACHE_HOME", context.cache_home),
    ):
        yield DenyDefinition(f"${variable}/tumbleweed", _under(root, "tumbleweed"))
    if context.runtime_dir is not None:
        yield DenyDefinition("$XDG_RUNTIME_DIR", context.runtime_dir)
    for path in SYSTEM_DENY:
        yield DenyDefinition(path, path)
    for entry in context.extra:
        yield DenyDefinition(entry.label, entry.path)


@final
@dataclass(frozen=True, slots=True)
class DenyForm:
    """One way an entry can be named: its folded components, and for a form that was resolved
    against the file system the identity of each directory on the way (`chain`) and of the
    entry itself (`target`, None if it does not exist). Empty for a form held by text only."""

    folded: tuple[str, ...]
    chain: frozenset[FsIdentity] = frozenset()
    target: FsIdentity | None = None


@final
@dataclass(frozen=True, slots=True)
class DenyEntry:
    """A protected place: its label and every form it can be named by."""

    label: str
    forms: tuple[DenyForm, ...]


def _relation(
    form: DenyForm,
    folded: tuple[str, ...],
    chain: frozenset[FsIdentity],
    identity: FsIdentity | None,
) -> DenyRelation | None:
    """How the path (as folded text, identities along it, identity of the target) relates to one
    form of an entry: by identity first, then by text."""
    if form.target is not None and form.target in chain:
        return DenyRelation.EQUAL if form.target == identity else DenyRelation.INSIDE
    if identity is not None and identity in form.chain:
        return DenyRelation.EQUAL if identity == form.target else DenyRelation.ANCESTOR
    shared = min(len(folded), len(form.folded))
    if folded[:shared] != form.folded[:shared]:
        return None
    if len(folded) == len(form.folded):
        return DenyRelation.EQUAL
    return DenyRelation.INSIDE if len(folded) > len(form.folded) else DenyRelation.ANCESTOR


def _forms_of(label: str, literal: str) -> tuple[DenyForm, ...]:
    """The forms of one entry: its literal path (text), its canonical path (text and
    identities) if it can be resolved, and on macOS the same under the data volume."""
    forms = [DenyForm(tuple(fold_name(name) for name in components_of(literal)))]
    try:
        resolved = ResolvedPath.resolve(literal)
    except PathError as error:
        # An unresolvable entry is still matched by its literal text, which refuses more, not less.
        _LOG.warning("deny entry %s was not resolved (%s); matching it by text", label, error.fault)
    else:
        forms.append(DenyForm(resolved.folded, resolved.chain_set, resolved.identity))
    if platform.system() == "Darwin":
        forms.extend(DenyForm(_DATA_VOLUME + form.folded) for form in list(forms))
    return tuple(dict.fromkeys(forms))


@final
class DenyRules:
    """The deny list for one user at one moment. Build with `DenyRules.build`; immutable.

    The constructor takes entries as they are (a test of the matching can give it forms with
    identities of its own); `build` is how the real ones are made."""

    __slots__ = ("_entries",)

    def __init__(self, entries: Sequence[DenyEntry]) -> None:
        self._entries = tuple(entries)

    @classmethod
    def build(cls, context: DenyContext) -> Self:
        """Resolve every entry of `deny_entries(context)` against the file system now."""
        seen: set[str] = set()
        entries: list[DenyEntry] = []
        for definition in deny_entries(context):
            if definition.path in seen:
                continue
            seen.add(definition.path)
            entries.append(
                DenyEntry(definition.label, _forms_of(definition.label, definition.path))
            )
        return cls(entries)

    @property
    def labels(self) -> tuple[str, ...]:
        """The label of every entry, in the order they are checked."""
        return tuple(entry.label for entry in self._entries)

    def check(self, path: ResolvedPath) -> DenyMatch | None:
        """The first entry that `path` equals, is inside or is an ancestor of, or None.

        By identity wherever both sides have one, and by folded text as well, so the path need
        not exist and a different spelling does not escape.
        """
        for entry in self._entries:
            for form in entry.forms:
                relation = _relation(form, path.folded, path.chain_set, path.identity)
                if relation is not None:
                    return DenyMatch(entry=entry.label, relation=relation)
        return None

    def check_text(self, path: StoredPath) -> DenyMatch | None:
        """Like `check`, for a stored grant path: by folded text only, because a stored record
        has no identities along it. Used to re-check grants made before the list changed."""
        folded = tuple(fold_name(name) for name in path.components)
        for entry in self._entries:
            for form in entry.forms:
                relation = _relation(form, folded, frozenset(), None)
                if relation is not None:
                    return DenyMatch(entry=entry.label, relation=relation)
        return None
