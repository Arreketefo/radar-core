from radar_core.stats import drop_display, robust_baseline


def test_robust_baseline_median_and_mad():
    median, mad = robust_baseline([100.0, 110.0, 105.0, 500.0, 95.0])
    assert median == 105.0
    assert mad == 5.0


def test_robust_baseline_degenerate_window_is_zero_mad():
    median, mad = robust_baseline([100.0, 100.0, 100.0])
    assert (median, mad) == (100.0, 0.0)


def test_drop_display_signs_and_zero():
    assert drop_display(0.30) == "-30%"
    assert drop_display(-1 / 6) == "+17%"
    assert drop_display(0.0) == "+0%"
    assert drop_display(-1e-9) == "+0%"  # never IEEE '-0%'
