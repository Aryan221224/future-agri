"""Task 6: the spray drone's spec, power model and battery, calibrated to the spec sheet."""
import pytest

from fa_drone.battery import Battery
from fa_drone.power import PowerModel
from fa_drone.spec import load_spec


@pytest.fixture(scope="module")
def spec():
    return load_spec("t1_10l")


def test_spec_values_from_sheet(spec):
    assert spec.tank_l == 10 and spec.mtow_kg == 28 and spec.standard_kg == 24
    assert spec.pump_lpm == (3.5, 5.5) and spec.swath_m == (3.0, 5.0)
    assert spec.fc.max_waypoints_per_group == 400 and spec.fc.hover_accuracy_h_m == 2.0
    assert spec.battery.nominal_wh == pytest.approx(710.4)
    assert spec.empty_kg + spec.battery.mass_kg + spec.tank_l == pytest.approx(spec.standard_kg)


def test_hover_points_within_5pct(spec):
    pm = PowerModel.calibrate(spec)
    assert pm.endurance_min(24.0) == pytest.approx(12.0, rel=0.05)
    assert pm.endurance_min(14.0) == pytest.approx(28.0, rel=0.05)


def test_eta_physical(spec):
    pm = PowerModel.calibrate(spec)
    assert 0.3 < pm.eta < 0.8
    assert 0.4 < pm.figure_of_merit < 0.85


def test_forward_power_bucket(spec):
    pm = PowerModel.calibrate(spec)
    p0 = pm.forward_w(24.0, 0.0)
    assert p0 == pytest.approx(pm.hover_w(24.0), rel=1e-6)
    assert pm.forward_w(24.0, 5.0) < p0                 # translational lift
    assert pm.forward_w(24.0, 20.0) > pm.forward_w(24.0, 6.0)   # parasitic drag wins at speed


def test_strong_headwind_costs_power(spec):
    """Moderate headwind *lowers* power (translational lift up to ~10 m/s airspeed);
    a strong one raises it above calm flight as drag takes over."""
    pm = PowerModel.calibrate(spec)
    assert pm.forward_w(24.0, 5.0, headwind_ms=12.0) > pm.forward_w(24.0, 5.0, headwind_ms=0.0)
    assert pm.forward_w(24.0, 5.0, headwind_ms=12.0) > pm.hover_w(24.0)


def test_battery_hover_endurance_consistent(spec):
    """The battery model and the power model agree on the 24 kg hover point."""
    pm = PowerModel.calibrate(spec)
    b = Battery(spec)
    t = 0.0
    while not b.low_voltage() and t < 3600:
        b.draw(pm.hover_w(24.0), 1.0)
        t += 1.0
    assert t / 60 == pytest.approx(12.0, rel=0.10)


def test_cold_battery_less_energy(spec):
    assert Battery(spec, temp_c=-10).usable_wh_left() < Battery(spec, temp_c=25).usable_wh_left()


def test_aged_battery_less_energy(spec):
    assert Battery(spec, cycles=400).usable_wh_left() < Battery(spec, cycles=0).usable_wh_left()


def test_low_voltage_triggers_before_empty(spec):
    b = Battery(spec)
    while not b.low_voltage():
        b.draw(3000.0, 1.0)
    assert b.soc > 0.05
