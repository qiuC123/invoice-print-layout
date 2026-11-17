"""Transport guard shared by pytest and standalone browser fixture processes."""
import socket


def guard_personal_agent(monkeypatch, blocked):
    for method in ('connect', 'connect_ex'):
        original = getattr(socket.socket, method)

        def guarded(sock, address, *args, _original=original, **kwargs):
            if isinstance(address, tuple) and address[1] == 25820:
                blocked.append(address)
                raise AssertionError('Tests must never connect to the real event port 25820')
            return _original(sock, address, *args, **kwargs)

        monkeypatch.setattr(socket.socket, method, guarded)
