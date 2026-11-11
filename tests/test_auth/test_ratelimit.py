from writer_service.auth.ratelimit import LoginLimiter


def test_window_expires() -> None:
    now = [0.0]
    lim = LoginLimiter(max_failures=2, window_seconds=60, clock=lambda: now[0])
    lim.record_failure("u")
    lim.record_failure("u")
    assert lim.blocked("u")
    now[0] = 61
    assert not lim.blocked("u")


def test_any_key_blocks_and_reset_clears() -> None:
    lim = LoginLimiter(max_failures=1)
    lim.record_failure("user:a", "ip:1")
    assert lim.blocked("user:b", "ip:1")  # same client, other username
    lim.reset("ip:1")
    assert not lim.blocked("user:b", "ip:1")
