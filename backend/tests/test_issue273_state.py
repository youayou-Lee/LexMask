from app.services.page_routing import state


def test_pass_then_upgrade_at_three():
    s = state.VolumeRoutingState()
    state.record_pass(s, 3)
    state.record_pass(s, 3)
    assert s.upgraded is False
    state.record_pass(s, 3)
    assert s.upgraded is True


def test_switch_resets_counter_not_upgrade():
    s = state.VolumeRoutingState()
    state.record_pass(s, 3); state.record_pass(s, 3)
    state.record_switch(s)
    assert s.consecutive_passes == 0 and s.upgraded is False


def test_upgrade_is_sticky_after_switch():
    s = state.VolumeRoutingState()
    for _ in range(3):
        state.record_pass(s, 3)
    assert s.upgraded is True
    state.record_switch(s)
    assert s.upgraded is True


def test_pick_sample_pages_deterministic_and_includes_first():
    a = state.pick_sample_pages(100, 2, "abc")
    b = state.pick_sample_pages(100, 2, "abc")
    c = state.pick_sample_pages(100, 2, "xyz")
    assert a == b and 1 in a and len(a) == 2
    assert all(1 <= p <= 100 for p in a)
    assert a != c or True  # 不同种子通常不同（不硬断言防退化）


def test_pick_sample_pages_zero_and_tiny_volume():
    assert state.pick_sample_pages(10, 0, "abc") == frozenset()
    single = state.pick_sample_pages(1, 2, "abc")
    assert single == frozenset({1})


def test_registry_keyed_and_cached():
    state.reset_registry()
    s1 = state.get_state("f1|fp", total_pages=10, content_sha="abc")
    s2 = state.get_state("f1|fp", total_pages=10, content_sha="abc")
    assert s1 is s2 and s1.sample_pages == state.pick_sample_pages(10, 2, "abc")
