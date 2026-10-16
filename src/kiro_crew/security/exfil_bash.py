"""The data-egress and reverse-shell command gate.

The exfiltration-specific subset of ``SUSPICIOUS_BASH_PATTERNS``, enforced at the
tool-invocation gate: command shapes that push local data out or open a shell to
a remote host. Every branch carries the id of the catalog rule it enforces, so an
operator opt-out of that rule skips it, and a ``-d @`` hit in a plain ``date`` or
inert-search command is let through by the deny tier's single-command eligibility
test.

:mod:`kiro_crew.security.exfil` is the import path of every name defined here.
"""

from __future__ import annotations

import fnmatch
import re

from .denied_rules import _INERT_SEARCH_VERBS, _exception_eligible
from .shell_normalizer import _heredoc_marker, _iter_shell_chars

# Data-egress / reverse-shell command shapes — the exfiltration-specific subset
# of SUSPICIOUS_BASH_PATTERNS. These are enforced at the
# tool-invocation gate (denied), unlike the full SUSPICIOUS_BASH_PATTERNS list
# which stays advisory: that list also carries destructive-but-local shapes
# (rm -rf, dd if=, chmod on system dirs, DROP TABLE) that a user may legitimately
# run in their own workspace, so hard-denying all of them at the gate would break
# ordinary use. This subset is narrowly the "push local data OUT / open a shell
# to a remote" shapes, where a hijacked-agent block is worth the rare false
# positive.
#
# Entries containing `*` are fnmatch globs (`*<pat>*`); the rest are
# case-insensitive substrings, so they fire regardless of intervening flags /
# token layout — `curl -d @f`, `curl -s -d @f`, `curl --data-binary @f` all
# match. The `@` sigil on curl body/upload flags means "read from a local file"
# (the tell-tale of egress); a bare `-d 'x=1'` inline body has no `@` and is not
# matched. curl long options accept BOTH ` @` and `=@` separators, so both are
# listed. `--data-raw` is deliberately EXCLUDED: it is the one --data variant
# that does NOT interpret a leading `@` as a file reference, so `--data-raw @x`
# posts the literal string `@x` (never reads a file) — including it would only
# add false positives. Multipart uploads use a glob (`-F *=@`) so ANY field name
# matches, not just a field literally named `file` (`curl -F x=@secret` exfils
# just as well).
_BASH_EXFIL_PATTERNS: list[str] = [
    "-d @",  # curl POST body read from a local file (space + `=` separators)
    "-d@",
    "-d=@",
    "--data @",
    "--data=@",
    "--data-binary @",
    "--data-binary=@",
    "--data-ascii @",
    "--data-ascii=@",
    "--data-urlencode @",  # also reads a local file when the value starts with @
    "--data-urlencode=@",
    "-F *=@",  # curl multipart file upload, any field name (glob)
    "--form *=@",
    "--upload-file",  # curl upload, long form
    "wget --post-file",  # wget file upload
    "/dev/tcp/",  # bash builtin reverse shell (>/dev/tcp/host/port)
    "/dev/udp/",
]

# Exfil shapes where whitespace or flag CASE around an operator matters, so a
# plain lowercased substring/glob would either miss a no-space variant or
# false-positive. Matched via regex against the ORIGINAL (non-lowercased)
# command. Each entry is (compiled pattern, human label).
_BASH_EXFIL_RES: list[tuple[re.Pattern[str], str]] = [
    # netcat reading a local file via input redirect — `nc host port < file` AND
    # `nc host port <file` (no space after `<`, a valid shell redirect that the
    # old `nc * < ` glob missed). `nc`/`ncat` is anchored at a word boundary so
    # `sync`/`func` etc. do not match. Case-insensitive (command name).
    (re.compile(r"(?:^|\s)nc(?:at)?\s+\S.*<", re.IGNORECASE), "nc/ncat file redirect"),
    # netcat reverse shell `nc -e <prog>` / `ncat -e <prog>`. `nc`/`ncat` is
    # anchored at a word boundary so `rsync -e ssh` (contains `nc -e`) and
    # `vnc -e` do NOT match; a plain substring `"nc -e"` false-positived on them.
    (re.compile(r"(?:^|\s)nc(?:at)?\s+-e\b", re.IGNORECASE), "nc/ncat reverse shell"),
    # curl upload short form `-T <file>` / `-Tfile` (no space). CASE-SENSITIVE
    # `-T`: curl's upload flag is uppercase, so this does NOT match lowercase long
    # options such as `--trace-time`. `-T` must begin at a word boundary.
    (re.compile(r"\bcurl\b.*(?:^|\s)-T\s*\S"), "curl -T upload"),
]


# Which catalog rule each always-on exfil branch enforces, so a denial maps back
# to a rule id and an operator opt-out is honoured. Patterns/labels absent from
# these maps stay unconditional.
_BASH_EXFIL_RULE_BY_PATTERN: dict[str, str] = {
    "-d @": "data-exfil-curl-file-body",
    "-d@": "data-exfil-curl-file-body",
    "-d=@": "data-exfil-curl-file-body",
    "--data @": "data-exfil-curl-file-body",
    "--data=@": "data-exfil-curl-file-body",
    "--data-binary @": "data-exfil-curl-file-body",
    "--data-binary=@": "data-exfil-curl-file-body",
    "--data-ascii @": "data-exfil-curl-file-body",
    "--data-ascii=@": "data-exfil-curl-file-body",
    "--data-urlencode @": "data-exfil-curl-file-body",
    "--data-urlencode=@": "data-exfil-curl-file-body",
    "-F *=@": "data-exfil-curl-multipart-upload",
    "--form *=@": "data-exfil-curl-multipart-upload",
    "--upload-file": "data-exfil-curl-upload",
    "wget --post-file": "data-exfil-wget-post-file",
    "/dev/tcp/": "reverse-shell-devtcp",
    "/dev/udp/": "reverse-shell-devtcp",
}

# A single regex can span more than one catalog row, so this maps to a TUPLE. The
# gate attributes each MATCH to one of those rows and honours that row's own
# toggle — see _exfil_rule_id_for_match.
_BASH_EXFIL_RULE_BY_LABEL: dict[str, tuple[str, ...]] = {
    "nc/ncat file redirect": ("data-exfil-nc-file-redirect",),
    "nc/ncat reverse shell": ("reverse-shell-nc", "reverse-shell-ncat"),
    "curl -T upload": ("data-exfil-curl-upload",),
}

#: For a label whose regex spans several catalog rows, the token that identifies
#: WHICH row a given match belongs to. Ordered longest-first so ``ncat`` is tested
#: before ``nc`` — the reverse would classify every ``ncat`` hit as ``nc``.
_BASH_EXFIL_ROW_DISCRIMINATORS: dict[str, tuple[tuple[str, str], ...]] = {
    "nc/ncat reverse shell": (("ncat", "reverse-shell-ncat"), ("nc", "reverse-shell-nc")),
}


def _exfil_rule_id_for_match(label: str, matched: str, rule_ids: tuple[str, ...]) -> str:
    """The catalog row a single exfil match belongs to.

    One regex can cover more than one row, and the operator toggles rows, not
    regexes — so a match has to be attributed before its toggle can be honoured.
    Falls back to the label's first row when nothing discriminates, which keeps the
    single-row labels (the common case) on their existing behaviour and never
    returns an id outside ``rule_ids``.
    """
    low = matched.lower()
    for token, rid in _BASH_EXFIL_ROW_DISCRIMINATORS.get(label, ()):
        if token in low and rid in rule_ids:
            return rid
    return rule_ids[0]


# `-d @` is a bare substring, so it also fires on benign commands that egress
# nothing (GNU `date` converting an epoch, `grep` for the literal `-d @`). The
# denial stays on the text — fail-closed, never anchored to curl, since no text
# matcher models bash. The carve-out relaxes it only for a command the
# deny-exception gate judges a single plain command whose first word runs no
# subcommand, so a `-d @` in it cannot reach an HTTP client.
_DATA_EXFIL_FILE_BODY_RULE = "data-exfil-curl-file-body"

#: `date` plus the inert-search verbs. `date` is local, not added to
#: `_INERT_SEARCH_VERBS`, which must not grow the `rm -rf /` deny-exception.
_CARVEOUT_VERBS = frozenset((*_INERT_SEARCH_VERBS, "date"))

#: Bash splits words on space and tab only; `str.split()` also splits on
#: Unicode spaces such as NBSP, which would misread `date<NBSP>x` as `date`.
_BASH_BLANKS_RE = re.compile(r"[ \t]+")


def _data_at_carveout_allows(command: str) -> bool:
    """A `-d @` hit in a benign `date`/`grep` command, not an upload: reuses
    `denied_rules._exception_eligible` (the blessed single-plain-command test)
    and requires a first word in `_CARVEOUT_VERBS`, which runs no subcommand."""
    if not _exception_eligible(command):
        return False
    words = _BASH_BLANKS_RE.split(command.strip(" \t"))
    return words[0] in _CARVEOUT_VERBS


# gh reads a request body from a LOCAL FILE through flags the substring table
# above does not name: `gh api --input <file>`, `--field k=@<file>`, and
# `gh release create|edit --notes-file <file>` (`-F <file>`). They are matched on
# shell words, and only where `gh` is a command's first word, so text that only
# mentions a flag (`echo …`, `grep …`, a later command's own `--input`) is left
# alone. Stdin (`-`, `k=@-`) names no file and stays allowed. `-f`/`--raw-field`
# never reads a file.
_GH_FILE_BODY_RULE = "data-exfil-gh-file-body"
_GH_FILE_BODY_SUBCOMMANDS = frozenset({"api", "repo", "release"})
#: gh global flags that take the next word as their value.
_GH_VALUE_FLAGS = frozenset({"-R", "--repo", "--hostname"})
#: Per subcommand, the gh options whose next word is a plain value (gh's own
#: flag tables; a short letter means different things per subcommand: `-d` is
#: `repo --description` but `release --draft`).
_GH_VALUE_OPTIONS: "dict[str, frozenset[str]]" = {
    "api": _GH_VALUE_FLAGS
    | frozenset(
        {"-X", "--method", "-H", "--header", "-f", "--raw-field", "-q", "--jq"}
        | {"-t", "--template", "-p", "--preview", "--cache"}
    ),
    "repo": _GH_VALUE_FLAGS
    | frozenset(
        {"-d", "--description", "-h", "--homepage", "--add-topic", "--remove-topic"}
        | {"--default-branch", "--visibility", "-g", "--gitignore", "-l", "--license"}
        | {"-t", "--team", "-p", "--template", "-s", "--source", "-r", "--remote"}
        | {"-u", "--upstream-remote-name", "--fork-name", "-b", "--branch"}
    ),
    "release": _GH_VALUE_FLAGS
    | frozenset(
        {"-t", "--title", "-n", "--notes", "--target", "--tag", "--notes-start-tag"}
        | {"--discussion-category", "-L", "--limit", "-O", "--output", "--pattern"}
        | {"-D", "--dir", "-A", "--archive", "-q", "--jq", "-T", "--template"}
    ),
}
#: Bare characters that end a simple command.
_COMMAND_ENDS = ";&|()\n"
#: Bash reserved words that may stand before a command's program word
#: (`{ gh …; }`, `then gh …`, `do gh …`, `! gh …`, `time gh …`).
_LEADING_WORDS = frozenset({"{", "!", "if", "then", "else", "elif", "while", "until", "do", "time"})


def _skip_heredoc_bodies(command: str, pos: int, delimiters: "list[str]") -> int:
    """Index just past the here-document bodies that start at *pos*."""
    for delimiter in delimiters:
        while pos < len(command):
            end = command.find("\n", pos)
            end = len(command) if end == -1 else end
            line, pos = command[pos:end], end + 1
            if line.lstrip("\t") == delimiter:
                break
    return pos


def _shell_commands(command: str) -> "list[list[str]]":
    """*command*'s simple commands, each as the words its program receives.

    Quote state comes from :func:`shell_normalizer._iter_shell_chars`, so a
    quoted `|`, `;` or `--input` stays inside its word, and here-document tags
    from :func:`shell_normalizer._heredoc_marker`. A bare `;`, `&`, `|`, `(`,
    `)` or newline ends a command (`>&`/`&>` are redirections). Comments and
    here-document bodies are not commands. No expansion is evaluated: `$(gh …)`
    is read as the words it contains.
    """
    commands: "list[list[str]]" = [[]]
    word: "list[str]" = []
    #: Per character of *word*: True where it was shell syntax (unquoted,
    #: unescaped), so a quoted `'<<EOF'` is text, never a here-document.
    syntax: "list[bool]" = []
    in_word = False
    heredocs: "list[str]" = []
    want_tag = False

    def add(ch: str, is_syntax: bool) -> None:
        nonlocal in_word
        word.append(ch)
        syntax.append(is_syntax)
        in_word = True

    def end_word() -> None:
        nonlocal in_word, want_tag
        if in_word:
            text = "".join(word)
            fd = len(text) - len(text.lstrip("0123456789"))
            operator = len(text) >= fd + 2 and syntax[fd] and syntax[fd + 1]
            tag = _heredoc_marker(text[fd:]) if operator else None
            if want_tag:
                heredocs.append(text)
                want_tag = False
            elif tag is not None:
                if tag:
                    heredocs.append(tag)
                else:
                    want_tag = True
            else:
                commands[-1].append(text)
        word.clear()
        syntax.clear()
        in_word = False

    pos = 0
    while pos < len(command):
        # Restarted after a comment or a here-document body: quote characters
        # there are text, so the state machine must not read them.
        restart = len(command)
        prev_state = 0
        for step in _iter_shell_chars(command[pos:]):
            at, ch = pos + step.offset, step.char
            if step.state != prev_state:  # an opening or closing quote
                if step.ansi and word and word[-1] == "$":
                    word.pop()  # the `$` of `$'…'`
                    syntax.pop()
                prev_state = step.state
                in_word = True
                continue
            if not step.active:
                if step.text != "\\\n":  # `\<newline>` joins lines
                    add(ch, False)
                continue
            if ch == "#" and not in_word:
                newline = command.find("\n", at)
                restart = len(command) if newline == -1 else newline
                break
            if ch in "<>" and word and not (all(syntax) and _is_fd_or_operator(word)):
                # Bash ends a word at a bare `<`/`>`: `-<<<'{}'` is the word `-`
                # then a here-string, `cat<<'EOF'` is `cat` then a here-document.
                end_word()
            if ch == "&" and ((word and word[-1] in "<>") or command[at + 1 : at + 2] == ">"):
                add(ch, True)
            elif ch in _COMMAND_ENDS:
                end_word()
                commands.append([])
                if ch == "\n" and heredocs:
                    restart = _skip_heredoc_bodies(command, at + 1, heredocs)
                    heredocs.clear()
                    break
            elif ch in " \t\r":
                end_word()
            else:
                add(ch, True)
        pos = restart
    end_word()
    return commands


def _is_fd_or_operator(chars: "list[str]") -> bool:
    """*chars* so far are an fd number or a redirection operator (`2`, `>`, `&`),
    which a following `<`/`>` continues rather than ends."""
    return all(c in "0123456789<>&" for c in chars)


def _is_assignment(word: str) -> bool:
    name, eq, _value = word.partition("=")
    return bool(eq) and name.isidentifier()


def _is_redirection(word: str) -> bool:
    return word.lstrip("0123456789")[:1] in ("<", ">")


def _names_stdin(value: str) -> bool:
    """*value* reads no file on disk: empty, `-`, `/dev/stdin`, `/dev/fd/N`, or a
    process substitution `<( … )` (its first word is the bare `<`)."""
    return value in ("", "-", "<", "/dev/stdin") or value.startswith("/dev/fd/")


def _gh_field_reads_file(spec: str) -> bool:
    """A gh field spec `k=@<file>`; `k=@-` and the other stdin names are not files."""
    _key, sep, value = spec.partition("=")
    return bool(sep) and value.startswith("@") and not _names_stdin(value[1:])


def _gh_short_cluster(word: str, value_options: "frozenset[str]") -> "tuple[str, bool]":
    """Read a short-flag cluster the way gh's flag parser does: booleans, then at
    most one flag that takes the rest of the word or, when none is left, the next
    word (`-dt TITLE`, `-tTITLE`). Returns the `-F…` part still to check (or
    ``""``) and whether the next word is a value. Any letter that is not a known
    value option counts as a boolean, so `-dF notes.md` still reaches `-F`."""
    for ci in range(1, len(word)):
        flag, rest = "-" + word[ci], word[ci + 1 :]
        if flag == "-F":
            return "-F" + rest, False
        if flag in value_options:
            return "", not rest
    return "", False


def _gh_release_action(args: "list[str]", value_options: "frozenset[str]") -> str:
    """The word after `release` that names its action (`create`, `download`, …)."""
    skip = False
    for word in args:
        if skip:
            skip = False
        elif word.startswith("-"):
            skip = word in value_options
        else:
            return word
    return ""


def _gh_file_body_flag(command: str) -> str:
    """The gh flag in *command* that sends a local file, or ``""``."""
    for words in _shell_commands(command):
        i = 0
        while i < len(words) and (_is_assignment(words[i]) or words[i] in _LEADING_WORDS):
            i += 1
        if i >= len(words) or words[i].rsplit("/", 1)[-1] != "gh":
            continue
        args = words[i + 1 :]
        # Global flags and redirections may sit before the subcommand.
        j = 0
        while j < len(args):
            w = args[j]
            if w.startswith("-") or _is_redirection(w):
                # A bare operator (`2>`) or a value flag (`-R o/r`) takes the next word.
                bare = _is_redirection(w) and w.lstrip("0123456789").strip("<>&") == ""
                j += 2 if w in _GH_VALUE_FLAGS or bare else 1
            elif j and args[j - 1].startswith("-") and "=" not in args[j - 1]:
                if w in _GH_FILE_BODY_SUBCOMMANDS:
                    break
                j += 1  # the value of a flag this walk does not know
            else:
                break
        if j >= len(args) or args[j] not in _GH_FILE_BODY_SUBCOMMANDS:
            continue
        release = args[j] == "release"
        value_options = _GH_VALUE_OPTIONS[args[j]]
        if release and _gh_release_action(args[j + 1 :], value_options) == "download":
            value_options = value_options | {"-p"}  # `--pattern` here, `--prerelease` on create
        # Global flags count (`gh --input f api …` gives api the flag); `-F` on
        # release means `--notes-file` only after the subcommand.
        # A known option's value (`--title '--input=x'`) is data, not a flag.
        skip = False
        for k, word in enumerate(args):
            if skip:
                skip = False
                continue
            skip = word in value_options
            if len(word) > 2 and word[0] == "-" and word[1] not in "-F":
                word, skip = _gh_short_cluster(word, value_options)
                if not word:
                    continue
            if release and k <= j and word.startswith("-F"):
                continue
            nxt = args[k + 1] if k + 1 < len(args) else ""
            name, eq, value = word.partition("=")
            if word.startswith("-F") and not word.startswith("--"):
                name, value = "-F", word[2:].removeprefix("=") or nxt
                skip = word == "-F"
            elif not eq:
                value = nxt
            if name in ("--input", "--notes-file", "--field"):
                skip = not eq
            if name == "--input" and not _names_stdin(value):
                return "--input"
            if release and name in ("--notes-file", "-F") and not _names_stdin(value):
                return "--notes-file"
            if not release and name in ("--field", "-F") and _gh_field_reads_file(value):
                return "--field"
    return ""


def audit_bash_exfiltration(
    command: str, *, enabled_ids: "frozenset[str] | None" = None
) -> str | None:
    """Return a denial reason if *command* matches a data-egress / reverse-shell
    shape that must be blocked at the tool-invocation gate, else None.

    Scoped to _BASH_EXFIL_PATTERNS / _BASH_EXFIL_RES (exfil/reverse-shell only) so
    it can be wired into the deny path in ``hooks.on_tool_call`` without blocking
    benign local commands. The broader :func:`audit_bash_command` stays advisory.

    Every branch carries the id of the catalog rule it enforces, so *enabled_ids*
    lets the caller honour an operator opt-out: a branch whose rule the operator
    disabled is skipped. ``None`` (the default) means ALL enabled — fail-closed,
    which is what keeps the callers that hold no effective set (cron command
    vetting, computer-use input vetting) at full strength without a change.
    """
    lower = command.lower()

    def _on(rule_id: str) -> bool:
        return enabled_ids is None or rule_id in enabled_ids

    carveout_allows: bool | None = None
    for pattern in _BASH_EXFIL_PATTERNS:
        rule_id = _BASH_EXFIL_RULE_BY_PATTERN.get(pattern, "")
        if rule_id and not _on(rule_id):
            continue
        pat = pattern.lower()
        if "*" in pat:
            if fnmatch.fnmatch(lower, f"*{pat}*"):
                return f"Blocked: command matches data-exfiltration pattern '{pattern}'"
        elif pat in lower:
            # Fail-closed denial, except an inert `date`/`grep` command (computed
            # once per command and reused).
            if rule_id == _DATA_EXFIL_FILE_BODY_RULE:
                if carveout_allows is None:
                    carveout_allows = _data_at_carveout_allows(command)
                if carveout_allows:
                    continue
            return f"Blocked: command matches data-exfiltration pattern '{pattern}'"
    for rx, label in _BASH_EXFIL_RES:
        rule_ids = _BASH_EXFIL_RULE_BY_LABEL.get(label, ())
        if not rule_ids:
            if rx.search(command):
                return f"Blocked: command matches data-exfiltration pattern ({label})"
            continue
        # A label can span more than one catalog row (one regex covers both the nc
        # and ncat rules). Denying while EITHER is enabled defeats the operator:
        # switching `reverse-shell-nc` off left `nc` blocked by its sibling. So
        # resolve each MATCH to the row it actually belongs to and honour that
        # row's own toggle. Every match is examined, not just the first, because a
        # command can carry both spellings and the leading one may be the disabled
        # row while the other is still enforced.
        for m in rx.finditer(command):
            matched_id = _exfil_rule_id_for_match(label, m.group(0), rule_ids)
            if _on(matched_id):
                return f"Blocked: command matches data-exfiltration pattern ({label})"
        continue
    if _on(_GH_FILE_BODY_RULE) and ("gh" in lower):
        flag = _gh_file_body_flag(command)
        if flag:
            return f"Blocked: command matches data-exfiltration pattern (gh {flag} from file)"
    return None
