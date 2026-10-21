import socket

import pytest
from pytest_socket import SocketBlockedError


def test_unit_tests_cannot_open_network_sockets() -> None:
    # pytest-socket also warns; under filterwarnings=error that warning alone fails the test.
    with (
        pytest.warns(UserWarning, match="tried to use socket"),
        pytest.raises(SocketBlockedError),
    ):
        socket.create_connection(("127.0.0.1", 9))


def test_unix_sockets_stay_available_for_the_event_loop() -> None:
    left, right = socket.socketpair()
    left.close()
    right.close()
