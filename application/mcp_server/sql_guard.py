"""Statement-level guard for ``honk_sql.execute``.

The MCP SQL tool exposes a Postgres connection to remote claudebox
clients. Two layers of defense keep this read-only:

1. **This guard** — rejects anything that isn't a single statement
   starting with ``SELECT`` or ``WITH``. Mirrors the cloxy-sql rule
   already documented in ``.claude/rules/cloxy-sql.md`` so operators
   share a mental model across the two SQL surfaces.
2. **Postgres role** — the MCP server connects as ``docsgpt_mcp_ro``,
   a role with hand-listed ``GRANT SELECT`` on non-secret-bearing
   tables (column-level on tables that carry bearers / OAuth /
   PII) and ``default_transaction_read_only = on``. This is the real
   backstop: regex-based parsers can be fooled by clever comment
   placement, but the role layer can't. See
   ``application/alembic/versions/0006_mcp_admin_tables.py`` for the
   full grant list.

Belt and braces: catching obvious junk early gives a clean error
message instead of a Postgres permission error, but we never rely on
the guard alone.

Why we don't try to use a real SQL parser (e.g. ``sqlparse``,
``pglast``): the guard is intentionally narrow. We accept exactly two
statement shapes (``SELECT``, ``WITH``), reject anything multi-statement,
and let the role enforcement catch the long tail. A full parser would
add a dependency, a performance cost, and the temptation to start
allowing arbitrary read-shaped statements.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Maximum length of a single accepted statement. Anything longer is
# almost certainly someone trying to smuggle a payload through and
# rarely a legitimate analyst query — we still want to be generous
# (large IN-lists, hand-written CTE chains) so the cap is high.
MAX_STATEMENT_BYTES = 16 * 1024


@dataclass(frozen=True)
class GuardResult:
    """Outcome of guarding a single statement."""

    ok: bool
    reason: str | None = None
    statement: str | None = None
    """The cleaned statement (whitespace stripped, trailing ``;`` removed).

    Set when ``ok`` is True. Callers should pass *this* string to psycopg
    rather than the raw user input so behaviour is consistent with what
    was validated.
    """


# Match a leading line/block comment so we can strip them before looking
# at the first keyword. We only strip *leading* comments — comments
# embedded later in the statement are fine; the guard's job is to make
# sure the statement *starts* with SELECT/WITH after any preamble of
# comments and whitespace, not to comment-strip the whole text.
_LEADING_LINE_COMMENT = re.compile(r"^\s*--[^\n]*\n")
_LEADING_BLOCK_COMMENT = re.compile(r"^\s*/\*.*?\*/", re.DOTALL)


# Keywords that indicate the statement is data-modifying even when wrapped
# inside a ``WITH`` CTE — e.g. ``WITH x AS (DELETE FROM foo RETURNING *)
# SELECT * FROM x``. Detected by token scan over the cleaned (non-string,
# non-dollar-quoted) portion of the input. The role layer rejects writes
# anyway, but catching it here gives a clean operator-facing error
# instead of a Postgres permission failure 30s later.
_WRITE_TOKENS: frozenset[str] = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "TRUNCATE",
        "DROP",
        "CREATE",
        "ALTER",
        "GRANT",
        "REVOKE",
        "COPY",  # COPY TO can write to server filesystem; superuser-gated but reject anyway.
        "CALL",  # function calls with side effects.
        "DO",  # anonymous code blocks.
        "VACUUM",
        "ANALYZE",
        "REINDEX",
        "CLUSTER",
    }
)


def _strip_leading_comments(s: str) -> str:
    """Remove leading whitespace and SQL comments, repeatedly."""
    while True:
        m = _LEADING_LINE_COMMENT.match(s)
        if m:
            s = s[m.end() :]
            continue
        m = _LEADING_BLOCK_COMMENT.match(s)
        if m:
            s = s[m.end() :]
            continue
        return s.lstrip()


# Recognises the opening of a dollar-quoted string: either ``$$`` or
# ``$tag$`` where tag is a Postgres identifier. We use this for both the
# multistatement and forbidden-keyword scanners so an operator query
# like ``SELECT $tag$;DELETE$tag$`` doesn't trip either check.
_DOLLAR_OPEN_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def _strip_quoted_and_comments(s: str) -> str:
    """Return ``s`` with strings / identifiers / dollar-quoted text /
    comments replaced by a single space.

    The single-space replacement is load-bearing: ``DELETE/**/FROM agents``
    must NOT collapse to ``DELETEFROM``, or word-boundary keyword
    matching would miss the ``DELETE``. One space per stripped region
    preserves token boundaries without preserving exact offsets.

    Recognised regions:

    * ``'...'`` single-quoted strings (``''`` escapes a quote)
    * ``"..."`` double-quoted identifiers (``""`` escapes a quote)
    * ``$$...$$`` and ``$tag$...$tag$`` dollar-quoted strings
    * ``-- ...`` line comments
    * ``/* ... */`` block comments (nested, per Postgres semantics)

    Unterminated regions consume to end-of-input and are still replaced
    with a single space; we err on the side of treating malformed input
    as opaque rather than letting the scanner walk back into "code".
    """
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        # Line comment: -- ... \n
        if ch == "-" and i + 1 < n and s[i + 1] == "-":
            nl = s.find("\n", i + 2)
            i = nl + 1 if nl != -1 else n
            out.append(" ")
            continue
        # Block comment: /* ... */ (nested-aware).
        if ch == "/" and i + 1 < n and s[i + 1] == "*":
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                if s[j] == "/" and j + 1 < n and s[j + 1] == "*":
                    depth += 1
                    j += 2
                elif s[j] == "*" and j + 1 < n and s[j + 1] == "/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            i = j
            out.append(" ")
            continue
        # Single-quoted string.
        if ch == "'":
            j = i + 1
            while j < n:
                if s[j] == "'":
                    if j + 1 < n and s[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            i = j
            out.append(" ")
            continue
        # Double-quoted identifier.
        if ch == '"':
            j = i + 1
            while j < n:
                if s[j] == '"':
                    if j + 1 < n and s[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            i = j
            out.append(" ")
            continue
        # Dollar-quoted string (either $$ or $tag$).
        if ch == "$":
            m = _DOLLAR_OPEN_RE.match(s, i)
            if m:
                tag = m.group(0)
                end = s.find(tag, m.end())
                if end != -1:
                    i = end + len(tag)
                else:
                    i = n  # unterminated — consume the rest.
                out.append(" ")
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _looks_multistatement(s: str) -> bool:
    """Return True if the input contains more than one logical statement.

    We reject anything with a ``;`` followed by non-whitespace, so a
    trailing semicolon is fine but ``SELECT 1; DROP TABLE users`` is
    not. The check ignores ``;`` inside string literals, dollar-quoted
    text, comments, and quoted identifiers — see
    :func:`_strip_quoted_and_comments`.
    """
    stripped = _strip_quoted_and_comments(s)
    # Strip trailing whitespace and any trailing semicolons (a clean
    # statement may end with one).
    stripped = stripped.rstrip().rstrip(";").rstrip()
    return ";" in stripped


def _find_forbidden_keyword(s: str) -> str | None:
    """Return a forbidden write keyword present in ``s`` outside of strings
    / identifiers / dollar-quoted text / comments.

    Word-boundary match, case-insensitive. Defends against data-modifying
    CTEs like ``WITH x AS (DELETE FROM ...) SELECT * FROM x``: the
    leading ``WITH`` passes the first-token check, but the body still
    contains ``DELETE`` outside any string literal. Returns the matched
    keyword for the error message, or ``None`` if the input is clean.
    """
    code = _strip_quoted_and_comments(s)
    for token in _WRITE_TOKENS:
        if re.search(rf"\b{token}\b", code, re.IGNORECASE):
            return token
    return None


def guard_select(query: str) -> GuardResult:
    """Validate that ``query`` is a single SELECT/WITH statement.

    Returns a :class:`GuardResult`. On success, ``statement`` holds the
    cleaned form callers should pass to psycopg; on failure, ``reason``
    holds a short human-readable explanation suitable for surfacing
    back to the MCP client.

    The guard rejects:

    * empty / whitespace-only input
    * anything longer than :data:`MAX_STATEMENT_BYTES`
    * input that is not a single statement (``;`` followed by
      non-whitespace, ignoring quoted text)
    * statements whose first non-comment keyword is anything other
      than ``SELECT`` or ``WITH``
    """
    if query is None:
        return GuardResult(ok=False, reason="query is required")
    if not isinstance(query, str):
        return GuardResult(ok=False, reason="query must be a string")
    if len(query.encode("utf-8")) > MAX_STATEMENT_BYTES:
        return GuardResult(
            ok=False,
            reason=(f"query exceeds maximum length of {MAX_STATEMENT_BYTES} bytes"),
        )

    stripped = query.strip()
    if not stripped:
        return GuardResult(ok=False, reason="query is empty")

    if _looks_multistatement(stripped):
        return GuardResult(
            ok=False,
            reason=("multi-statement input is not allowed; submit one SELECT or WITH at a time"),
        )

    # Strip a single trailing semicolon if present so psycopg sees a
    # clean statement.
    cleaned = stripped.rstrip().rstrip(";").rstrip()
    if not cleaned:
        return GuardResult(ok=False, reason="query is empty")

    head = _strip_leading_comments(cleaned).lstrip()
    # Split off the first whitespace-delimited token, case-insensitive.
    first_token_match = re.match(r"[A-Za-z]+", head)
    if not first_token_match:
        return GuardResult(
            ok=False,
            reason="query must start with SELECT or WITH",
        )
    first_token = first_token_match.group(0).upper()
    if first_token not in {"SELECT", "WITH"}:
        return GuardResult(
            ok=False,
            reason=(
                f"only SELECT/WITH are allowed; got {first_token}. "
                "DML/DDL is rejected by both this guard and the "
                "docsgpt_mcp_ro Postgres role."
            ),
        )

    # Even when the first token is SELECT/WITH, Postgres allows
    # data-modifying CTEs (``WITH x AS (DELETE FROM foo RETURNING *)
    # SELECT * FROM x``). The role rejects them, but reject here too so
    # the operator gets a clear error instead of a Postgres permission
    # failure 30s into the call.
    forbidden = _find_forbidden_keyword(cleaned)
    if forbidden is not None:
        return GuardResult(
            ok=False,
            reason=(
                f"forbidden keyword {forbidden} found outside of string literals; "
                "data-modifying CTEs and side-effecting statements are rejected. "
                "DML/DDL is rejected by both this guard and the "
                "docsgpt_mcp_ro Postgres role."
            ),
        )

    return GuardResult(ok=True, statement=cleaned)
