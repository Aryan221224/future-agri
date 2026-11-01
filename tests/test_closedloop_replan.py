"""The remaining job after each sortie: sprayed cells drop out, skipped strips come back,
cells the camera found clean leave the plan, unseen cells keep their prior."""
import numpy as np

from fa_runner.closedloop.replan import merge_observations, remaining_targets


class _SpotTwin:
    """The planner's spot rule at a fixed threshold, as TwinFarm.herbicide_targets returns it."""
    threshold = 10.0

    def herbicide_targets(self, w):
        tg = np.asarray(w) > self.threshold
        return ("spot" if tg.any() else "none"), tg


def test_sprayed_cells_drop_out_and_skipped_ones_come_back():
    w = np.full(100, 30.0)                                   # all above threshold
    dose = np.zeros(100)
    dose[:60] = 20.0
    dose[60:70] = 5.0                                        # 60 done, 10 skipped strips
    tg, why = remaining_targets(_SpotTwin(), w, dose, 20.0, "herbicide")
    assert not tg[:60].any() and tg[60:].all() and "60 cells done" in why and "10 under-dosed" in why


def test_ninety_percent_counts_as_done():
    dose = np.full(10, 18.0)                                 # exactly 90 % of 20 L/ha
    tg, _ = remaining_targets(_SpotTwin(), np.full(10, 30.0), dose, 20.0, "herbicide")
    assert not tg.any()


def test_clean_observed_cells_leave_the_plan():
    w = np.full(100, 30.0)
    w[:50] = 0.0
    tg, _ = remaining_targets(_SpotTwin(), w, np.zeros(100), 20.0, "herbicide")
    assert not tg[:50].any() and tg[50:].all()


def test_fungicide_keeps_the_original_targets_minus_done_cells():
    orig = np.zeros(20, bool)
    orig[:10] = True
    dose = np.zeros(20)
    dose[:4] = 20.0
    tg, why = remaining_targets(_SpotTwin(), np.zeros(20), dose, 20.0, "fungicide", original=orig)
    assert tg.tolist() == [False] * 4 + [True] * 6 + [False] * 10 and "4 cells done" in why


def test_merge_keeps_prior_where_unseen():
    prior = np.full(10, 5.0)
    est = np.full(10, np.nan)
    est[:3] = 1.0
    seen = np.zeros(10, bool)
    seen[:3] = True
    m, _, cum = merge_observations(prior, np.full(10, 2.0), np.zeros(10, bool), est, np.full(10, 0.5), seen)
    assert np.allclose(m[:3], 1.0) and np.allclose(m[3:], 5.0) and cum.sum() == 3


def test_a_cell_seen_twice_takes_the_inverse_variance_mean():
    prior = np.full(4, 5.0)
    seen1 = np.array([True, True, False, False])
    m1, s1, c1 = merge_observations(prior, np.full(4, 3.0), np.zeros(4, bool), np.array([2.0, 2.0, np.nan, np.nan]),
                                    np.array([1.0, 1.0, np.inf, np.inf]), seen1)
    assert m1[0] == 2.0 and s1[0] == 1.0                    # first look replaces the scan prior
    m2, s2, c2 = merge_observations(m1, s1, c1, np.array([4.0, np.nan, np.nan, np.nan]),
                                    np.array([1.0, np.inf, np.inf, np.inf]), np.array([True, False, False, False]))
    assert m2[0] == 3.0 and s2[0] < 1.0 and m2[1] == 2.0 and m2[2] == 5.0 and c2.sum() == 2


def test_uninformative_observation_leaves_the_prior():
    m, _, cum = merge_observations(np.full(3, 7.0), np.full(3, 2.0), np.zeros(3, bool), np.zeros(3),
                                   np.full(3, np.inf), np.ones(3, bool))
    assert np.allclose(m, 7.0) and not cum.any()
