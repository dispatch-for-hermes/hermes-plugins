"""Push alert copy: the words the Dispatch app's own alerts use (docs/notifications.md).

Title says who (the bot's display name), the body what: the reply's first words or the command awaiting
approval. Previews are plain text, clipped and masked where they hold a secret. The app's
renderer (src/notification-preview.ts) and native code (DispatchPlatformPolicy) shape text the same way;
src/notification-preview-cases.json holds the cases all three agree on. With the device's Show Previews
off, a push only says who needs you. Standard library only: no Hermes imports.
"""
from __future__ import annotations

import re

PREVIEW_MAX = 180
REDACTED = "••••"
_SOURCE_MAX = 4000

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_RULE = re.compile(r"^([-*_]\s*){3,}$")
_TABLE_RULE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$")
_BLOCK_TAGS = re.compile(r"</?(?:br|p|div|li|ul|ol|tr|table|h[1-6]|blockquote|details|summary|pre)\b[^>]*>", re.I)
_TAGS = re.compile(r"</?(?:a|b|i|u|s|em|strong|span|sup|sub|small|code|kbd|mark|del|ins|thead|tbody|td|th)\b[^>]*>", re.I)

# The app's clipboard check (fe-platform-policy.ts privateText): vendor key shapes count only with a
# random-looking run in them, so "sk-learn-..." stays readable.
_TOKENS = [re.compile(p) for p in (
    r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}",
    r"(?<![A-Za-z0-9])[rs]k_(?:live|test)_[A-Za-z0-9]{20,}",
    r"(?<![A-Za-z0-9])(?:xai-|gsk_|pplx-|r8_|hf_)[A-Za-z0-9]{20,}",
    r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|glpat-[A-Za-z0-9_-]{20,})",
    r"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9-]{10,}",
    r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}",
    r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])",
)]
# The rest mirror redactPrivate in src/bridge/fe-platform-policy.ts rule for rule, in the same order (the shared
# cases in src/notification-preview-cases.json check all three). Hermes' agent/redact.py was the reference for the
# shapes; it is never imported.
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*")
_TELEGRAM = re.compile(r"(?<![0-9])[0-9]{8,10}:[A-Za-z0-9_-]{30,}")
_SENDGRID = re.compile(r"(?<![A-Za-z0-9])SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}")
_DISCORD = re.compile(r"(?<![A-Za-z0-9_-])[MNO][A-Za-z0-9_-]{23,27}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,40}")
_NPM = re.compile(r"(?<![A-Za-z0-9])npm_[A-Za-z0-9]{30,}")
_AUTH_HEADER = re.compile(r"(authorization[\"']?\s*[:=]\s*[\"']?(?:basic|bearer|token|digest|negotiate|ntlm|apikey|key)\s+)([^\s\"'`,;]+)", re.I)
_BEARER_ANY = re.compile(r"(?<![A-Za-z0-9_])(bearer\s+)([A-Za-z0-9._~+/-]{20,}=*)", re.I)
_ARG = r"(\"[^\"]*\"?|'[^']*'?|[^\s\"']+)"
_CLI_USER = re.compile(r"(?<!\S)(-u|--user)(\s+|=)(?:\"([^\s:\"]+):([^\"]*)(\"?)|'([^\s:']+):([^']*)('?)|([^\s:\"']+):([^\s\"']+))")
_CLI_FLAG = re.compile(r"(?<!\S)(--(?:password|passwd|pass|passphrase|token|api-key|apikey|secret|client-secret|auth-token|access-token))(=|\s+)" + _ARG)
_CLI_SHORT = re.compile(r"(?<![A-Za-z0-9_.-])((?:mysql[A-Za-z0-9_-]*|mariadb[A-Za-z0-9_-]*|mongo(?:sh|dump|restore|import|export)?|sshpass|(?:docker|podman|nerdctl|buildah|skopeo|oras)\s+login|helm\s+registry\s+login)\b[^\n|;&]{0,200}?\s-p|redis-cli\b[^\n|;&]{0,200}?\s-a)([ \t]*)" + _ARG)
_HTPASSWD = re.compile(r"(?<![A-Za-z0-9_.-])(htpasswd\b[^\n|;&]{0,200}?\s-[A-Za-z]*b[A-Za-z]*\b[^\n|;&]{0,300}\s)(\"[^\"]*\"?|'[^']*'?|[^\s\"'|;&]+)(?=[ \t]*(?:[|;&\n]|\Z))")
_URL_PASSWORD = re.compile(r"(?<![A-Za-z0-9+.-])([a-z][a-z0-9+.-]*://[^\s/:@?#]*):([^\s/?#]*)@", re.I)
_URL_TOKEN = re.compile(r"(?<![A-Za-z0-9+.-])([a-z][a-z0-9+.-]*://)([^\s/:@?#]{8,})@", re.I)
_QUERY_SECRET = re.compile(r"([?&#](?:access_token|refresh_token|id_token|auth_token|session_token|token|api_key|apikey|key|secret|client_secret|password|passwd|pass|pwd|auth|jwt|code|sig|signature|x-amz-signature|x-amz-credential|x-amz-security-token)=)[^&\s#]+", re.I)
_VALUE = r"(?:\"([^\"]+)(\"?)|'([^']+)('?)|([^\s\"'`<>]+))"
_PROSE = re.compile(r"(?<![A-Za-z0-9_])(api[ _-]?key|access[ _-]?key|secret[ _-]?key|access[ _-]?token|auth[ _-]?token|password|passwd|passcode|passphrase|token|secret)(\s+(?:is|was)\s*:?\s+)" + _VALUE, re.I)
_KEY = r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]{0,40}?(?:pass|pw|secret|token|key|auth|credential|sig)[A-Za-z0-9_.-]{0,40})"
_PASSPHRASE = re.compile(_KEY + r"([\"']?(?:[ \t]*:[ \t]*|[ \t]+=[ \t]*|=[ \t]+))([^\s\"'`<>][^\n<`]*)", re.I)
_ASSIGNMENT = re.compile(_KEY + r"([\"']?\s*[:=]\s*)" + _VALUE, re.I)
_TRAIL = re.compile(r"[.,;:)\]}]+\Z")
_STRONG_KEYS = {"password", "passwd", "passphrase", "passcode", "secret", "secrets", "credential", "credentials", "apikey", "accesskey",
                "privatekey", "secretkey", "authtoken", "accesstoken", "refreshtoken", "idtoken", "sessiontoken", "clientsecret", "bearertoken"}
_PASSWORD_KEYS = {"password", "passwd", "passphrase", "passcode"}
_WEAK_KEYS = {"token", "tokens", "key", "keys", "auth", "authorization", "bearer", "signature", "sig"}
_PROSE_WORDS = {"the", "a", "an", "i", "we", "you", "your", "my", "our", "their", "his", "her", "its", "it", "this", "that", "these", "those",
                "same", "not", "no", "none", "null", "nil", "true", "false", "yes", "required", "optional", "hidden", "redacted", "empty",
                "blank", "unset", "unchanged", "provided", "needed", "missing", "see", "use", "is", "was", "are", "be", "will", "should",
                "must", "can", "set", "reset", "changed", "stored", "saved", "sent", "and", "or", "in", "on", "at", "for", "from", "with",
                "of", "to", "if", "when", "only", "also", "still", "just", "now", "here", "there", "below", "above", "as", "by", "via"}


def _random(text: str) -> bool:
    return bool(re.search(r"\d", text) and re.search(r"[A-Za-z]", text) and re.search(r"[A-Za-z0-9]{16,}", text))


def secret_key_kind(key: str) -> str:
    """"password", "strong", "weak" or "" (fe-platform-policy.ts secretKeyKind)."""
    caps = not re.search(r"[a-z]", key)
    split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)).lower()
    parts = [part for part in re.split(r"[^a-z0-9]+", split) if part]
    pairs = [parts[index] + part for index, part in enumerate(parts[1:])]
    last = parts[-1] if parts else ""
    if (any(part in _PASSWORD_KEYS for part in parts) or last in ("pass", "pw") or (last == "pwd" and len(parts) > 1)
            or (caps and any(re.search(r"(password|passwd)$", part) for part in parts))):
        return "password"
    if any(part in _STRONG_KEYS for part in parts + pairs) or (caps and any(part.endswith("secret") for part in parts)):
        return "strong"
    return "weak" if any(part in _WEAK_KEYS for part in parts) or (caps and any(part.endswith("token") for part in parts)) else ""


def _opaque(value: str) -> bool:
    return bool(_random(value) or (len(value) >= 16 and re.fullmatch(r"[A-Fa-f0-9]+", value))
                or (len(value) >= 20 and re.fullmatch(r"[A-Za-z0-9_./+=-]+", value))
                or (len(value) >= 12 and sum(bool(re.search(p, value)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]")) >= 2))


def _reference(value: str) -> bool:
    return bool(re.match(r"(?:\$\{?[A-Za-z_][A-Za-z0-9_]*\}?\Z|\$\(|\$\{\{|process\.env\.|os\.(?:getenv|environ))", value))


def _file_path(value: str) -> bool:
    return bool(re.fullmatch(r"(?:~|\.{1,2})?/[A-Za-z0-9._/-]*", value)) and not any(_random(part) for part in value.split("/"))


def _mask_value(kind: str, value: str, quoted: bool) -> str:
    trail = "" if quoted else (_TRAIL.search(value).group(0) if _TRAIL.search(value) else "")
    core = value[:len(value) - len(trail)]
    if not kind or not re.search(r"[A-Za-z0-9]", core) or _reference(core):
        return value
    if kind == "weak":
        keep = not _opaque(core) or _file_path(core)
    else:
        keep = (not quoted and core.lower() in _PROSE_WORDS) or (kind == "strong" and _file_path(core))
    return value if keep else REDACTED + trail


def _valued(kind: str, double, double_end, single, single_end, bare) -> str:
    """A _VALUE match rebuilt, its value masked, or kept and scanned again (a kept run can hold the next key=value)."""
    def quoted(quote: str, content: str, end) -> str:
        shown = _mask_value(kind, content, True)
        return quote + (_assignments(content) if shown == content else shown) + (end or "")
    if double is not None:
        return quoted('"', double, double_end)
    if single is not None:
        return quoted("'", single, single_end)
    value = bare or ""
    head = re.match(r"[^,;&]*", value).group(0)
    if kind in ("password", "strong"):
        # A reference, path, word or mask before the punctuation is kept (the rest is its own text); a credential runs on.
        if head and _mask_value(kind, head, False) == head:
            return head + _assignments(value[len(head):])
        return _mask_value(kind, value, False)
    return _mask_value(kind, head, False) + _assignments(value[len(head):])


def _assignments(text: str) -> str:
    return _ASSIGNMENT.sub(lambda m: m.group(1) + m.group(2) + _valued(secret_key_kind(m.group(1)), *m.groups()[2:7]), text)


def _phrase(value: str) -> int:
    """Where a password written as words ends: two or more words, none reading as prose ("and", "the", "(…"), up to
    one that ends a clause. 0 when it is one word (the assignment rule's) or none."""
    end = words = 0
    for match in re.finditer(r"\S+", value):
        word = match.group(0)
        core = re.sub(r"[.,;:!?)\]}]+\Z", "", word)
        if not re.search(r"[A-Za-z0-9]", core) or core.lower() in _PROSE_WORDS or word[0] in "([{":
            break
        words += 1
        end = match.start() + len(core)
        if core != word:
            break
    return end if words >= 2 else 0


def _argument(value: str):
    """A quoted or bare argument masked in place ("-p pw", '--password "a b"'), or None when it names no secret."""
    quote = value[0] if value[:1] in ("\"", "'") else ""
    closed = bool(quote) and len(value) > 1 and value.endswith(quote)
    content = (value[1:-1] if closed else value[1:]) if quote else value
    if not re.search(r"[A-Za-z0-9]", content) or content.startswith("-") or _reference(content):
        return None
    return quote + REDACTED + (quote if closed else "") if quote else REDACTED


def _prose(m) -> str:
    key, joint, double, double_end, single, single_end, bare = m.groups()
    core = (double if double is not None else single or "") if bare is None else _TRAIL.sub("", bare)
    hide = bare is None or _opaque(core) or (bool(re.search("pass", key, re.I)) and bool(re.search(r"[^A-Za-z]", core)) and len(core) >= 4)
    return key + joint + _valued("password", double, double_end, single, single_end, bare) if hide else m.group(0)


def _cli_user(m) -> str:
    flag, gap, double_user, _double_secret, double_end, single_user, _single_secret, single_end, user, _secret = m.groups()
    if double_user is not None:
        return f'{flag}{gap}"{double_user}:{REDACTED}{double_end}'
    if single_user is not None:
        return f"{flag}{gap}'{single_user}:{REDACTED}{single_end}"
    return f"{flag}{gap}{user}:{REDACTED}"


def _masked_argument(m, head: str, value: str) -> str:
    shown = _argument(value)
    return m.group(0) if shown is None else head + shown


def _passphrase(m) -> str:
    key, joint, value = m.groups()
    end = _phrase(value) if secret_key_kind(key) == "password" else 0
    return key + joint + REDACTED + value[end:] if end else m.group(0)


def redact(text: str) -> str:
    out = _SENDGRID.sub(REDACTED, _TELEGRAM.sub(REDACTED, _JWT.sub(REDACTED, _PRIVATE_KEY.sub(REDACTED, text))))
    out = _DISCORD.sub(lambda m: REDACTED if _random(m.group(0)) else m.group(0), out)
    for shape in (*_TOKENS, _NPM):
        out = shape.sub(lambda m: REDACTED if _random(m.group(0)) else m.group(0), out)
    out = _AUTH_HEADER.sub(lambda m: m.group(1) + REDACTED if re.search(r"[A-Za-z0-9]", m.group(2)) else m.group(0), out)
    out = _BEARER_ANY.sub(lambda m: m.group(1) + REDACTED, out)
    out = _CLI_USER.sub(_cli_user, out)
    out = _CLI_FLAG.sub(lambda m: _masked_argument(m, m.group(1) + m.group(2), m.group(3)), out)
    out = _CLI_SHORT.sub(lambda m: _masked_argument(m, m.group(1) + m.group(2), m.group(3)), out)
    out = _HTPASSWD.sub(lambda m: _masked_argument(m, m.group(1), m.group(2)), out)
    out = _URL_PASSWORD.sub(lambda m: f"{m.group(1)}:{REDACTED}@" if m.group(2) else m.group(0), out)
    out = _URL_TOKEN.sub(lambda m: f"{m.group(1)}{REDACTED}@" if _opaque(m.group(2)) else m.group(0), out)
    out = _QUERY_SECRET.sub(lambda m: m.group(1) + REDACTED, out)
    out = _PROSE.sub(_prose, out)
    out = _PASSPHRASE.sub(_passphrase, out)
    return _assignments(out)


def _inline(text: str) -> str:
    text = _TAGS.sub("", _BLOCK_TAGS.sub(" ", text))
    text = re.sub(r"(\*\*|__)(?=\S)([^\n]+?)(?<=\S)\1", r"\2", text)
    text = re.sub(r"(?<![A-Za-z0-9_*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![A-Za-z0-9_*])", r"\1", text)
    text = re.sub(r"(?<![A-Za-z0-9_])_(?=\S)([^_\n]+?)(?<=\S)_(?![A-Za-z0-9_])", r"\1", text)
    return re.sub(r"~~(?=\S)([^~\n]+?)(?<=\S)~~", r"\1", text)


def plain_text(markdown: str) -> str:
    """Markdown as plain text: code blocks are "[code]", images "[image]", links their words."""
    lines: list[tuple[str, str]] = []
    fence = ""
    for raw in str(markdown or "")[:_SOURCE_MAX].replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        opened = _FENCE.match(raw)
        if fence:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence) \
                    and not raw.strip()[len(opened.group(1)):].strip():
                fence = ""
            continue
        if opened:
            fence = opened.group(1)
            lines.append(("[code]", ""))
            continue
        line, end = raw.strip(), ""
        if not line or _RULE.match(line) or _TABLE_RULE.match(line):
            continue
        line = re.sub(r"^(>\s?)+", "", line)
        if re.match(r"^#{1,6}\s+", line):
            line, end = re.sub(r"^#{1,6}\s+", "", line), ":"
        elif re.match(r"^([-*+]|\d{1,3}[.)])\s+", line):
            line, end = re.sub(r"^\[[ xX]\]\s+", "", re.sub(r"^([-*+]|\d{1,3}[.)])\s+", "", line)), "."
        elif line.startswith("|"):
            line, end = re.sub(r"\s*\|\s*", " · ", re.sub(r"^\||\|$", "", line).strip()), "."
        lines.append((line, end))
    text = "\n".join(line + end if index < len(lines) - 1 and end and not re.search(r"[.!?:;,…]$", line) else line
                     for index, (line, end) in enumerate(lines))
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "[image]", text)
    text = re.sub(r"<img\b[^>]*>", "[image]", text, flags=re.I)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<(https?://[^>\s]+)>", r"\1", text)
    parts = re.split(r"(`[^`\n]+`)", text)
    return "".join(part[1:-1] if index % 2 else _inline(part) for index, part in enumerate(parts))


def clip(text: str, max_len: int = PREVIEW_MAX) -> str:
    """At most ``max_len`` characters, cut at a word when one ends in the last 40%, with an ellipsis."""
    if len(text) <= max_len:
        return text
    cut = text[:max_len - 1]
    space = cut.rfind(" ")
    if space >= max_len * 0.6:
        cut = cut[:space]
    return re.sub(r"[\s,.;:!?\-–—]+$", "", cut) + "…"


def preview(markdown: str, max_len: int = PREVIEW_MAX) -> str:
    return clip(re.sub(r"\s+", " ", redact(plain_text(redact(str(markdown or ""))))).strip(), max_len)


def command_summary(command: str, max_len: int = 120) -> str:
    lines = [line.strip() for line in str(command or "").splitlines() if line.strip()]
    if not lines:
        return ""
    first = clip(re.sub(r"\s+", " ", redact(lines[0])), max_len)
    return f"{first} …" if len(lines) > 1 and not first.endswith("…") else first


def approvable(summary: str) -> bool:
    """Whether an alert showing this command summary may offer Approve: the whole command is on it (no "…")."""
    return bool(summary) and not summary.endswith("\u2026")


_LOWER = {"usage", "this", "the", "your", "that", "a", "an", "it", "there"}


def error_sentence(reason: str) -> str:
    text = re.sub(r"\.+$", "", reason.strip())
    word = re.match(r"[A-Za-z]*", text).group(0)
    return text[:1].lower() + text[1:] if word.lower() in _LOWER else text


def readable_profile(name: str) -> str:
    """A profile id in words, as the Bots roster names one without a display name ("Hermes" for default). An
    unknown profile has no name: the alert then says what happened without saying who."""
    text = (name or "").strip()
    if not text:
        return ""
    if text == "default":
        return "Hermes"
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", text):
        return text
    return re.sub(r"\b\w", lambda m: m.group(0).upper(), re.sub(r"[-_]+", " ", text))


def alert(kind: str, sender: str, text: str = "", previews: bool = True, about: str = "") -> dict:
    """``{title, body}`` for an APNs alert. ``text`` is the reply or the command awaiting approval; ``about`` an
    approval's description. Off, previews name only the sender."""
    who = clip(redact(re.sub(r"\s+", " ", sender or "").strip()), 60)
    if not previews:
        if kind == "approval":
            return {"body": f"{who} needs your approval" if who else "A request needs your approval"}
        return {"body": f"New reply from {who}" if who else "New reply"}
    if kind == "approval":
        command = command_summary(text)
        described = preview(about)
        line = f"Wants to run: {command}" if command else described or "Needs your approval"
        return {"title": who or "Approval needed", "body": line}
    return {"title": who or "New reply", "body": preview(text) or "New reply"}
