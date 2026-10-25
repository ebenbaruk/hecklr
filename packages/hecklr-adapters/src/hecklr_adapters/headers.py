"""Header values from the environment: an adapter's ``headers_env`` (RFC 0023, issue #115).

An agent behind authentication needs a token in a header; ``headers_env`` names the variable
holding it, so the token never goes in ``hecklr.yaml``. Variables are read once, when the adapter
is built, so a missing one fails before any trial. The values stay in the adapter: error messages
name the variable and the header, never the value.
"""

from collections.abc import Mapping

from hecklr_adapters.errors import AdapterConfigError


def resolve_headers(
    headers: Mapping[str, str],
    headers_env: Mapping[str, str],
    *,
    environ: Mapping[str, str],
    adapter: str,
) -> dict[str, str]:
    """``headers`` plus each ``headers_env`` header with its variable's value.

    Raises:
        AdapterConfigError: a variable is unset or empty, or a header is set both ways (no
            silent precedence between a literal and a secret).
    """
    literal = {name.casefold() for name in headers}
    clashes = sorted(name for name in headers_env if name.casefold() in literal)
    if clashes:
        raise AdapterConfigError(
            f"{adapter} adapter: header(s) {', '.join(clashes)} set in both headers and "
            "headers_env; keep one"
        )
    resolved = dict(headers)
    for header, variable in headers_env.items():
        value = environ.get(variable)
        if not value:
            raise AdapterConfigError(
                f"environment variable {variable} ({adapter} adapter headers_env, header "
                f"{header}) is not set"
            )
        resolved[header] = value
    return resolved
