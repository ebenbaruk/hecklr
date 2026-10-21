"""A throwaway GreenMail server for the tests that need a real SMTP/IMAP server."""

from collections.abc import Iterator

import pytest

from hecklr_adapters.email.testing import MailServer, docker_available, greenmail


@pytest.fixture(scope="module")
def mail_server() -> Iterator[MailServer]:
    """GreenMail in Docker for this module; the test is skipped when Docker is unavailable."""
    if not docker_available():
        pytest.skip("Docker is not available for the GreenMail mail server")
    with greenmail() as server:
        yield server
