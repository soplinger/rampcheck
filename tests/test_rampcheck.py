import math
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

import rampcheck as rc

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "entrance.ramp"

GOOD_HEADER = """\
units: in
width: 48
cross-slope: 1:50
handrails: both
handrail-height: 36
handrail-extension: 12
"""


def rules(findings, level="fail"):
    return {f.rule for f in findings if f.level == level}


def check_text(body, header=GOOD_HEADER):
    ramp, errors = rc.parse_ramp(header + body)
    assert errors == []
    return rc.check_ramp(ramp)


# ---------------------------------------------------------------- numbers

def test_parse_length_handles_units_and_mixed_fractions():
    assert rc.parse_length("36in") == 36 * rc.INCH_MM
    assert rc.parse_length("1.5m") == 1500
    assert rc.parse_length("2 1/2 ft") == Fraction(5, 2) * 12 * rc.INCH_MM
    assert rc.parse_length("3/4in") == Fraction(3, 4) * rc.INCH_MM
    assert rc.parse_length("900") == 900 * rc.INCH_MM  # default unit is inches


def test_parse_length_rejects_nonsense():
    with pytest.raises(rc.ParseError):
        rc.parse_length("tall")
    with pytest.raises(rc.ParseError):
        rc.parse_length("12 furlongs")


def test_parse_ratio_accepts_colon_and_slash_and_rejects_other_forms():
    assert rc.parse_ratio("1:12") == 12
    assert rc.parse_ratio("1/48") == 48
    with pytest.raises(rc.ParseError):
        rc.parse_ratio("2:12")


def test_nearest_sixteenth_formats_inches():
    assert rc.nearest_sixteenth(Fraction(5, 2) * rc.INCH_MM) == "2 1/2 in"
    assert rc.nearest_sixteenth(0) == "0 in"
    assert rc.nearest_sixteenth(rc.INCH_MM / 16) == "1/16 in"
    assert rc.nearest_sixteenth(60 * rc.INCH_MM) == "60 in"


def test_fmt_ratio_reports_run_over_rise():
    assert rc.fmt_ratio(180, 15) == "1:12.00"
    assert rc.fmt_ratio(0, 0) == "flat"


# ---------------------------------------------------------------- parsing

def test_parse_reports_errors_with_line_numbers():
    text = "units: in\nwidth: 48\nrun 15 200\nrun fifteen 200\nlanding 60\n"
    _, errors = rc.parse_ramp(text)
    assert (4, "cannot read length 'fifteen'") in errors
    assert any(number == 5 for number, _ in errors)


def test_units_line_applies_to_the_whole_file_wherever_it_sits():
    text = "width: 1.2\nunits: m\nrun 0.15 2\nlanding 1.5 1.2\n"
    ramp, errors = rc.parse_ramp(text)
    assert errors == []
    assert ramp.width == 1200
    assert ramp.runs[0].rise == 150


def test_missing_width_and_runs_are_reported():
    _, errors = rc.parse_ramp("name: Empty\n")
    messages = [message for _, message in errors]
    assert "missing 'width:' line" in messages
    assert "no 'run' lines found" in messages


# ---------------------------------------------------------------- checks

def test_example_file_passes_and_has_no_warnings():
    ramp, errors = rc.parse_ramp(EXAMPLE.read_text(encoding="utf-8"))
    assert errors == []
    findings = rc.check_ramp(ramp)
    assert rc.passed(findings)
    assert findings == []


def test_steep_run_fails_running_slope():
    findings = check_text("landing 60 60\nrun 10 100\nlanding 60 60\n")
    assert "slope" in rules(findings)


def test_run_slope_near_limit_only_warns():
    findings = check_text("landing 60 60\nrun 15 185\nlanding 60 60\n")
    assert rc.passed(findings)
    assert "slope" in rules(findings, level="warn")


def test_rise_over_thirty_inches_in_one_run_fails():
    findings = check_text("landing 60 60\nrun 31 400\nlanding 60 60\n")
    assert "rise" in rules(findings)


def test_ramp_must_start_and_end_with_landings():
    findings = check_text("run 15 200\nlanding 60 60\n")
    assert "landing" in rules(findings)
    findings = check_text("landing 60 60\nrun 15 200\n")
    assert "landing" in rules(findings)


def test_consecutive_runs_without_a_landing_fail():
    findings = check_text("landing 60 60\nrun 6 80\nrun 6 80\nlanding 60 60\n")
    assert any("no landing between" in f.message for f in findings)


def test_short_landing_and_turning_landing_rules():
    short = check_text("landing 48 60\nrun 15 200\nlanding 60 60\n")
    assert "landing" in rules(short)
    turn = check_text("landing 60 60\nrun 15 200\nlanding 60 48 turn\nrun 15 200\nlanding 60 60\n")
    assert any("turning landing" in f.message for f in turn)


def test_clear_width_below_36_inches_fails():
    header = GOOD_HEADER.replace("width: 48", "width: 34")
    findings = check_text("landing 60 60\nrun 15 200\nlanding 60 60\n", header)
    assert "width" in rules(findings)


def test_cross_slope_steeper_than_1_in_48_fails():
    header = GOOD_HEADER.replace("1:50", "1:40")
    findings = check_text("landing 60 60\nrun 15 200\nlanding 60 60\n", header)
    assert "cross-slope" in rules(findings)


def test_handrails_required_on_both_sides_for_long_runs():
    header = GOOD_HEADER.replace("handrails: both", "handrails: left")
    findings = check_text("landing 60 60\nrun 15 200\nlanding 60 60\n", header)
    assert "handrails" in rules(findings)


def test_handrail_height_outside_34_to_38_inches_fails():
    header = GOOD_HEADER.replace("handrail-height: 36", "handrail-height: 30")
    findings = check_text("landing 60 60\nrun 15 200\nlanding 60 60\n", header)
    assert any("handrail height" in f.message for f in findings)


def test_short_run_needs_no_handrails():
    header = "units: in\nwidth: 48\nhandrails: none\n"
    findings = check_text("landing 60 60\nrun 4 48\nlanding 60 60\n", header)
    assert rc.passed(findings)


# ---------------------------------------------------------------- geometry and solver

def test_solver_gives_fewest_runs_and_exact_footprint():
    result = rc.solve(60 * rc.INCH_MM)
    assert result["minimum_runs"] == 2
    assert result["per_run_rise"] == 30 * rc.INCH_MM
    assert result["horizontal_total"] == 720 * rc.INCH_MM
    assert result["landings"] == 3
    assert result["footprint"] == 900 * rc.INCH_MM


def test_solver_rejects_too_few_runs():
    with pytest.raises(ValueError):
        rc.solve(60 * rc.INCH_MM, runs=1)


def test_concrete_volume_is_footprint_times_width_times_thickness():
    ramp, _ = rc.parse_ramp(EXAMPLE.read_text(encoding="utf-8"))
    volume = rc.concrete_m3(ramp, 4 * rc.INCH_MM, 0)
    expected = 580 * 48 * 4 * (25.4 ** 3) / 1e9  # 580 in footprint, 48 in wide, 4 in thick
    assert float(volume) == pytest.approx(expected)


def test_rail_length_counts_sloped_runs_intermediate_landing_and_extensions():
    ramp, _ = rc.parse_ramp(EXAMPLE.read_text(encoding="utf-8"))
    expected_in = 2 * math.hypot(200, 15) + 60 + 2 * 12
    assert rc.rail_length_mm(ramp) == pytest.approx(expected_in * 25.4)


def test_ascii_profile_draws_the_ramp_and_ground_line():
    ramp, _ = rc.parse_ramp(EXAMPLE.read_text(encoding="utf-8"))
    lines = rc.ascii_profile(ramp).splitlines()
    assert lines[-2] == "_" * 60
    assert "#" in lines[-3]


def test_svg_profile_is_well_formed_and_escapes_the_name():
    text = "units: in\nname: A & B <test>\nwidth: 48\nlanding 60 60\nrun 15 200\nlanding 60 60\n"
    ramp, _ = rc.parse_ramp(text)
    root = ET.fromstring(rc.svg_profile(ramp))
    assert root.tag.endswith("svg")
    assert root.find("{http://www.w3.org/2000/svg}title").text == "A & B <test>"


# ---------------------------------------------------------------- command line

def test_cli_check_exit_codes(tmp_path, capsys):
    assert rc.main(["check", str(EXAMPLE)]) == rc.EXIT_OK
    assert "PASS" in capsys.readouterr().out

    steep = tmp_path / "steep.ramp"
    steep.write_text("width: 48\nlanding 60 60\nrun 10 100\nlanding 60 60\n", encoding="utf-8")
    assert rc.main(["check", str(steep)]) == rc.EXIT_VIOLATIONS

    tight = tmp_path / "tight.ramp"
    tight.write_text(
        "width: 48\ncross-slope: 1:50\nhandrails: both\nhandrail-height: 36\n"
        "landing 60 60\nrun 15 185\nlanding 60 60\n",
        encoding="utf-8",
    )
    assert rc.main(["check", str(tight)]) == rc.EXIT_OK
    assert rc.main(["check", "--strict", str(tight)]) == rc.EXIT_VIOLATIONS

    broken = tmp_path / "broken.ramp"
    broken.write_text("width: 48\nrun banana 100\n", encoding="utf-8")
    assert rc.main(["check", str(broken)]) == rc.EXIT_INPUT_ERROR


def test_cli_solve_prints_plan(capsys):
    assert rc.main(["solve", "--rise", "60in"]) == rc.EXIT_OK
    out = capsys.readouterr().out
    assert "Runs:                2" in out
    assert "Footprint length:    900 in" in out


def test_cli_profile_writes_svg(tmp_path):
    target = tmp_path / "profile.svg"
    assert rc.main(["profile", str(EXAMPLE), "--svg", str(target)]) == rc.EXIT_OK
    assert ET.parse(target).getroot().tag.endswith("svg")
