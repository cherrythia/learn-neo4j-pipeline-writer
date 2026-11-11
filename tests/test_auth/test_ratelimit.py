import threading

from writer_service.auth.ratelimit import LoginLimiter, username_key


def test_window_expires() -> None:
    now = [0.0]
    lim = LoginLimiter(max_per_pair=2, window_seconds=60, clock=lambda: now[0])
    pair, client = lim.keys("u", "1.1.1.1")
    assert lim.attempt(pair, client) and lim.attempt(pair, client)
    assert not lim.attempt(pair, client)
    now[0] = 61
    assert lim.attempt(pair, client)


def test_attacker_elsewhere_cannot_lock_out_the_real_user() -> None:
    lim = LoginLimiter(max_per_pair=2)
    for _ in range(5):
        lim.attempt(*lim.keys("admin", "6.6.6.6"))
    assert lim.attempt(*lim.keys("admin", "10.0.0.5"))  # the admin's own client is unaffected


def test_client_budget_stops_password_spraying() -> None:
    lim = LoginLimiter(max_per_pair=5, max_per_client=3)
    assert all(lim.attempt(*lim.keys(f"user{i}", "6.6.6.6")) for i in range(3))
    assert not lim.attempt(*lim.keys("user99", "6.6.6.6"))


def test_rejected_attempts_do_not_grow_memory() -> None:
    lim = LoginLimiter(max_per_pair=1, max_per_client=1)
    lim.attempt(*lim.keys("a", "6.6.6.6"))
    before = lim.size()
    for i in range(500):
        assert not lim.attempt(*lim.keys(f"x{i}" * 10, "6.6.6.6"))
    assert lim.size() == before


def test_total_keys_are_capped() -> None:
    lim = LoginLimiter(max_keys=50)
    for i in range(200):
        lim.attempt(*lim.keys(f"u{i}", f"10.0.0.{i}"))
    assert lim.size() <= 50


def test_empty_keys_are_pruned_after_window() -> None:
    now = [0.0]
    lim = LoginLimiter(window_seconds=10, clock=lambda: now[0])
    pair, client = lim.keys("u", "c")
    lim.attempt(pair, client)
    now[0] = 11
    lim.attempt(*lim.keys("v", "d"))  # touching other keys doesn't prune these...
    assert lim.attempt(pair, client)  # ...but checking them does, then re-adds one hit
    assert lim.size() == 4


def test_parallel_attempts_never_exceed_budget() -> None:
    lim = LoginLimiter(max_per_pair=5, max_per_client=100)
    keys = lim.keys("admin", "6.6.6.6")
    results: list[bool] = []
    barrier = threading.Barrier(40)

    def go() -> None:
        barrier.wait()
        results.append(lim.attempt(*keys))

    threads = [threading.Thread(target=go) for _ in range(40)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert results.count(True) == 5


def test_username_key_folds_case_and_accents_like_mysql() -> None:
    assert username_key("ÄDMIN") == username_key("ádmin") == username_key("admin")
    lim = LoginLimiter(max_per_pair=2)
    assert lim.keys("ÄDMIN", "c") == lim.keys("admin", "c")


def test_refund_returns_one_slot() -> None:
    lim = LoginLimiter(max_per_pair=10, max_per_client=2)
    _, client = lim.keys("u", "c")
    assert lim.attempt(client) and lim.attempt(client) and not lim.attempt(client)
    lim.refund(client)
    assert lim.attempt(client)
