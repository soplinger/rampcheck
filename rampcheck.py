"""rampcheck: offline accessible-ramp planner and checker.

Reads a plain-text ramp file, checks it against the ADA 2010 Standards
section 405 limits (running slope, rise per run, landings, clear width,
cross slope and handrails), solves a minimum ramp for a target rise,
estimates rail length and concrete volume, and draws ASCII or SVG profiles.

All lengths are kept as exact fractions of a millimetre, so unit conversion
and slope ratios never pick up floating-point error.
"""

import argparse
import html
import math
import re
import sys
from dataclasses import dataclass, field
from fractions import Fraction

__version__ = "1.0.0"

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_INPUT_ERROR = 2

INCH_MM = Fraction(254, 10)  # exact: 1 in = 25.4 mm
UNIT_MM = {
    "mm": Fraction(1),
    "cm": Fraction(10),
    "m": Fraction(1000),
    "in": INCH_MM,
    "ft": Fraction(12) * INCH_MM,
}
UNIT_ALIASES = {'"': "in", "'": "ft", "inch": "in", "inches": "in", "feet": "ft"}

# ADA 2010 Standards, section 405 (ramps). Edit here to match local code.
MAX_SLOPE_RATIO = 12          # running slope no steeper than 1:12
MAX_CROSS_SLOPE_RATIO = 48    # cross slope no steeper than 1:48
MAX_RISE_MM = 30 * INCH_MM    # rise per run
MIN_LANDING_MM = 60 * INCH_MM
MIN_CLEAR_WIDTH_MM = 36 * INCH_MM
HANDRAIL_RISE_TRIGGER_MM = 6 * INCH_MM
HANDRAIL_RUN_TRIGGER_MM = 72 * INCH_MM
HANDRAIL_MIN_HEIGHT_MM = 34 * INCH_MM
HANDRAIL_MAX_HEIGHT_MM = 38 * INCH_MM
HANDRAIL_MIN_EXTENSION_MM = 12 * INCH_MM
TIGHT_MARGIN = Fraction(95, 100)  # slopes within 5% of the limit get a warning

LENGTH_RE = re.compile(
    r"^(?P<num>\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)\s*"
    r"(?P<unit>mm|cm|m|in|ft|inches|inch|feet|\"|')?$"
)
UNITS_LINE_RE = re.compile(r"^\s*units\s*:\s*([a-z]+)", re.IGNORECASE | re.MULTILINE)


class ParseError(Exception):
    """Raised for a single bad line; collected with its line number."""


@dataclass
class Landing:
    line: int
    length: Fraction
    width: Fraction
    turn: bool = False


@dataclass
class Run:
    line: int
    rise: Fraction
    horizontal: Fraction


@dataclass
class Ramp:
    name: str = "Unnamed ramp"
    width: Fraction | None = None
    cross_slope_ratio: Fraction | None = None
    handrails: str = "none"
    handrail_height: Fraction | None = None
    handrail_extension: Fraction = HANDRAIL_MIN_EXTENSION_MM
    items: list = field(default_factory=list)

    @property
    def runs(self):
        return [item for item in self.items if isinstance(item, Run)]

    @property
    def landings(self):
        return [item for item in self.items if isinstance(item, Landing)]


@dataclass
class Finding:
    level: str  # "fail" or "warn"
    rule: str
    line: int | None
    message: str


# ---------------------------------------------------------------- numbers

def parse_length(text, default_unit="in"):
    """Parse '36in', '2 1/2 ft', '3/4in', '1.2m' or '900' (default unit) to mm."""
    raw = text.strip().lower()
    match = LENGTH_RE.match(raw)
    if not match:
        raise ParseError(f"cannot read length '{text}'")
    num = match.group("num")
    if " " in num:
        whole, frac = num.split()
        value = Fraction(whole) + Fraction(frac)
    else:
        value = Fraction(num)
    unit = match.group("unit") or default_unit
    unit = UNIT_ALIASES.get(unit, unit)
    if unit not in UNIT_MM:
        raise ParseError(f"unknown unit '{unit}'")
    return value * UNIT_MM[unit]


def parse_ratio(text):
    """Parse '1:48' or '1/48' into the denominator 48 as an exact Fraction."""
    match = re.fullmatch(r"\s*1\s*[:/]\s*(\d+(?:\.\d+)?)\s*", text)
    if not match:
        raise ParseError(f"cannot read ratio '{text}' (expected 1:N)")
    value = Fraction(match.group(1))
    if value <= 0:
        raise ParseError("ratio must be greater than zero")
    return value


def nearest_sixteenth(mm):
    """Format millimetres as inches rounded to the nearest 1/16 inch."""
    total = round(Fraction(mm) / INCH_MM * 16)
    sign = "-" if total < 0 else ""
    whole, rem = divmod(abs(total), 16)
    if rem == 0:
        return f"{sign}{whole} in"
    frac = Fraction(rem, 16)
    if whole:
        return f"{sign}{whole} {frac.numerator}/{frac.denominator} in"
    return f"{sign}{frac.numerator}/{frac.denominator} in"


def fmt_len(mm, unit="in"):
    """Format a length in the requested display unit."""
    if unit == "in":
        return nearest_sixteenth(mm)
    value = Fraction(mm) / UNIT_MM[unit]
    places = {"mm": 1, "cm": 1, "m": 3, "ft": 2}[unit]
    return f"{float(value):.{places}f} {unit}"


def fmt_ratio(run_mm, rise_mm):
    """Format the slope as 1:N, with N to two decimals."""
    if rise_mm == 0:
        return "flat"
    return f"1:{float(Fraction(run_mm) / Fraction(rise_mm)):.2f}"


# ---------------------------------------------------------------- parsing

def parse_ramp(text, default_unit="in"):
    """Return (Ramp, errors). errors is a list of (line_number, message)."""
    ramp = Ramp()
    errors = []
    width_seen = False
    unit = default_unit
    # A 'units:' line applies to the whole file, wherever it sits.
    declared = UNITS_LINE_RE.search(text)
    if declared and declared.group(1).lower() in UNIT_MM:
        unit = declared.group(1).lower()

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            if ":" in line and line.split(":", 1)[0].strip().lower() in {
                "name", "units", "width", "cross-slope", "handrails",
                "handrail-height", "handrail-extension",
            }:
                key, value = [part.strip() for part in line.split(":", 1)]
                key = key.lower()
                if key == "name":
                    ramp.name = value or ramp.name
                elif key == "units":
                    if value not in UNIT_MM:
                        raise ParseError(f"units must be one of {', '.join(UNIT_MM)}")
                    unit = value
                elif key == "width":
                    ramp.width = parse_length(value, unit)
                    width_seen = True
                elif key == "cross-slope":
                    ramp.cross_slope_ratio = parse_ratio(value)
                elif key == "handrails":
                    if value.lower() not in {"both", "left", "right", "none"}:
                        raise ParseError("handrails must be both, left, right or none")
                    ramp.handrails = value.lower()
                elif key == "handrail-height":
                    ramp.handrail_height = parse_length(value, unit)
                elif key == "handrail-extension":
                    ramp.handrail_extension = parse_length(value, unit)
                continue

            parts = line.split()
            keyword = parts[0].lower()
            if keyword == "landing":
                if len(parts) not in (3, 4):
                    raise ParseError("landing needs: landing <length> <width> [turn]")
                flag = parts[3].lower() if len(parts) == 4 else None
                if flag not in (None, "turn"):
                    raise ParseError(f"unknown landing option '{parts[3]}'")
                length = parse_length(parts[1], unit)
                landing_width = parse_length(parts[2], unit)
                if length <= 0 or landing_width <= 0:
                    raise ParseError("landing dimensions must be greater than zero")
                ramp.items.append(Landing(number, length, landing_width, flag == "turn"))
            elif keyword == "run":
                if len(parts) != 3:
                    raise ParseError("run needs: run <rise> <horizontal>")
                rise = parse_length(parts[1], unit)
                horizontal = parse_length(parts[2], unit)
                if rise <= 0 or horizontal <= 0:
                    raise ParseError("run rise and horizontal must be greater than zero")
                ramp.items.append(Run(number, rise, horizontal))
            else:
                raise ParseError(f"unknown line '{line}'")
        except ParseError as exc:
            errors.append((number, str(exc)))

    if not width_seen:
        errors.append((0, "missing 'width:' line"))
    if not ramp.runs:
        errors.append((0, "no 'run' lines found"))
    return ramp, errors


# ---------------------------------------------------------------- checking

def check_ramp(ramp):
    """Return a list of Finding objects for every rule the ramp breaks or nears."""
    findings = []

    def fail(rule, line, message):
        findings.append(Finding("fail", rule, line, message))

    def warn(rule, line, message):
        findings.append(Finding("warn", rule, line, message))

    items = ramp.items
    runs = ramp.runs

    # Running slope and rise per run.
    for index, run in enumerate(runs, start=1):
        label = f"run {index}"
        if run.horizontal < run.rise * MAX_SLOPE_RATIO:
            fail("slope", run.line,
                 f"{label} slope {fmt_ratio(run.horizontal, run.rise)} is steeper than 1:{MAX_SLOPE_RATIO}")
        elif run.horizontal <= run.rise * MAX_SLOPE_RATIO / TIGHT_MARGIN:
            warn("slope", run.line,
                 f"{label} slope {fmt_ratio(run.horizontal, run.rise)} is within 5% of the 1:{MAX_SLOPE_RATIO} limit")
        if run.rise > MAX_RISE_MM:
            fail("rise", run.line,
                 f"{label} rises {fmt_len(run.rise)}, more than the {fmt_len(MAX_RISE_MM)} allowed per run")

    # Landings at the top, bottom and between runs.
    if not items or not isinstance(items[0], Landing):
        fail("landing", items[0].line if items else None,
             "ramp must start with a landing at the bottom")
    if not items or not isinstance(items[-1], Landing):
        fail("landing", items[-1].line if items else None,
             "ramp must end with a landing at the top")
    for before, after in zip(items, items[1:]):
        if isinstance(before, Run) and isinstance(after, Run):
            fail("landing", after.line,
                 f"run on line {after.line} follows a run with no landing between them")

    for landing in ramp.landings:
        if landing.length < MIN_LANDING_MM:
            fail("landing", landing.line,
                 f"landing is {fmt_len(landing.length)} long; minimum is {fmt_len(MIN_LANDING_MM)}")
        if ramp.width is not None and landing.width < ramp.width:
            fail("landing", landing.line,
                 f"landing is {fmt_len(landing.width)} wide, narrower than the ramp's {fmt_len(ramp.width)}")
        if landing.turn and (landing.length < MIN_LANDING_MM or landing.width < MIN_LANDING_MM):
            fail("landing", landing.line,
                 f"turning landing must be at least {fmt_len(MIN_LANDING_MM)} by {fmt_len(MIN_LANDING_MM)}")

    # Clear width and cross slope.
    if ramp.width is not None and ramp.width < MIN_CLEAR_WIDTH_MM:
        fail("width", None,
             f"clear width {fmt_len(ramp.width)} is below the {fmt_len(MIN_CLEAR_WIDTH_MM)} minimum")
    if ramp.cross_slope_ratio is None:
        warn("cross-slope", None, "no 'cross-slope:' line; cross slope not checked")
    elif ramp.cross_slope_ratio < MAX_CROSS_SLOPE_RATIO:
        fail("cross-slope", None,
             f"cross slope 1:{float(ramp.cross_slope_ratio):g} is steeper than 1:{MAX_CROSS_SLOPE_RATIO}")

    # Handrails.
    needs_rails = any(
        run.rise > HANDRAIL_RISE_TRIGGER_MM or run.horizontal > HANDRAIL_RUN_TRIGGER_MM
        for run in runs
    )
    if needs_rails and ramp.handrails != "both":
        fail("handrails", None,
             "handrails are required on both sides (a run rises over 6 in or runs over 72 in)")
    if ramp.handrails != "none":
        if ramp.handrail_height is None:
            fail("handrails", None, "handrail height not given ('handrail-height:')")
        elif not (HANDRAIL_MIN_HEIGHT_MM <= ramp.handrail_height <= HANDRAIL_MAX_HEIGHT_MM):
            fail("handrails", None,
                 f"handrail height {fmt_len(ramp.handrail_height)} must be between "
                 f"{fmt_len(HANDRAIL_MIN_HEIGHT_MM)} and {fmt_len(HANDRAIL_MAX_HEIGHT_MM)}")
        if ramp.handrail_extension < HANDRAIL_MIN_EXTENSION_MM:
            fail("handrails", None,
                 f"handrail extension {fmt_len(ramp.handrail_extension)} is below the "
                 f"{fmt_len(HANDRAIL_MIN_EXTENSION_MM)} minimum")
    return findings


def passed(findings):
    return not any(f.level == "fail" for f in findings)


# ---------------------------------------------------------------- geometry

def profile_points(ramp):
    """Return [(x_mm, z_mm)] corners along the ramp, starting at (0, 0)."""
    x, z = Fraction(0), Fraction(0)
    points = [(x, z)]
    for item in ramp.items:
        if isinstance(item, Landing):
            x += item.length
        else:
            x += item.horizontal
            z += item.rise
        points.append((x, z))
    return points


def footprint_mm(ramp):
    return sum((item.length if isinstance(item, Landing) else item.horizontal)
               for item in ramp.items)


def rail_length_mm(ramp):
    """Length of one handrail: sloped run lengths, intermediate landings and extensions."""
    total = 0.0
    for index, item in enumerate(ramp.items):
        if isinstance(item, Run):
            total += math.hypot(float(item.horizontal), float(item.rise))
        elif 0 < index < len(ramp.items) - 1:
            total += float(item.length)
    return total + 2 * float(ramp.handrail_extension)


def concrete_m3(ramp, thickness_mm, waste_percent):
    """Slab volume in cubic metres. Vertical thickness t over horizontal length h gives area h*t."""
    if ramp.width is None:
        return None
    volume_mm3 = footprint_mm(ramp) * ramp.width * Fraction(thickness_mm)
    waste = 1 + Fraction(waste_percent) / 100
    return volume_mm3 * waste / Fraction(1000) ** 3


def elevation_at(points, x):
    """Height of the ramp surface at horizontal position x (mm)."""
    for (x0, z0), (x1, z1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return z1
            return z0 + (z1 - z0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def ascii_profile(ramp, columns=60, rows=10):
    points = profile_points(ramp)
    total_x = points[-1][0]
    peak = max(z for _, z in points) or Fraction(1)
    lines = []
    heights = []
    for col in range(columns):
        x = total_x * col / (columns - 1)
        heights.append(round(elevation_at(points, x) / peak * rows))
    for row in range(rows, 0, -1):
        lines.append("".join("#" if h >= row else " " for h in heights).rstrip())
    lines.append("_" * columns)
    lines.append(f"bottom {fmt_len(0)}  ->  horizontal {fmt_len(total_x)}  ->  top rise {fmt_len(peak)}")
    return "\n".join(lines)


def svg_profile(ramp, unit="in"):
    points = profile_points(ramp)
    total_x = float(points[-1][0])
    peak = float(max(z for _, z in points)) or 1.0
    width, height, margin = 640, 320, 40
    scale_x = (width - 2 * margin) / (total_x or 1.0)
    scale_z = (height - 2 * margin - 40) / peak
    coords = " ".join(
        f"{margin + float(x) * scale_x:.1f},{height - margin - float(z) * scale_z:.1f}"
        for x, z in points
    )
    base_y = height - margin
    title = html.escape(ramp.name)
    summary = html.escape(f"rise {fmt_len(points[-1][1], unit)}, run {fmt_len(points[-1][0], unit)}")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">\n'
        f'  <title>{title}</title>\n'
        f'  <rect width="100%" height="100%" fill="#ffffff"/>\n'
        f'  <line x1="{margin}" y1="{base_y}" x2="{width - margin}" y2="{base_y}" stroke="#444"/>\n'
        f'  <polyline points="{coords}" fill="none" stroke="#1f6feb" stroke-width="3"/>\n'
        f'  <text x="{margin}" y="24" font-family="sans-serif" font-size="16">{title}</text>\n'
        f'  <text x="{margin}" y="{height - 8}" font-family="sans-serif" font-size="12">{summary}</text>\n'
        f'</svg>\n'
    )


# ---------------------------------------------------------------- solver

def solve(rise_mm, runs=None, landing_mm=MIN_LANDING_MM):
    """Minimum compliant ramp for a total rise. Returns a dict of exact results."""
    rise_mm = Fraction(rise_mm)
    if rise_mm <= 0:
        raise ValueError("rise must be greater than zero")
    minimum_runs = math.ceil(rise_mm / MAX_RISE_MM)
    count = runs if runs is not None else minimum_runs
    if count < minimum_runs:
        raise ValueError(
            f"{count} run(s) cannot climb {fmt_len(rise_mm)}; at least {minimum_runs} needed")
    per_run_rise = rise_mm / count
    per_run_horizontal = per_run_rise * MAX_SLOPE_RATIO
    landing_count = count + 1
    landing_total = landing_mm * landing_count
    return {
        "runs": count,
        "minimum_runs": minimum_runs,
        "per_run_rise": per_run_rise,
        "per_run_horizontal": per_run_horizontal,
        "horizontal_total": per_run_horizontal * count,
        "landings": landing_count,
        "landing_total": landing_total,
        "footprint": per_run_horizontal * count + landing_total,
    }


# ---------------------------------------------------------------- reporting

def format_report(ramp, findings, unit="in"):
    out = [f"Ramp: {ramp.name}"]
    out.append(f"Runs: {len(ramp.runs)}   Landings: {len(ramp.landings)}   "
               f"Width: {fmt_len(ramp.width, unit) if ramp.width is not None else 'not set'}")
    for index, run in enumerate(ramp.runs, start=1):
        out.append(f"  run {index} (line {run.line}): rise {fmt_len(run.rise, unit)}, "
                   f"horizontal {fmt_len(run.horizontal, unit)}, slope {fmt_ratio(run.horizontal, run.rise)}")
    out.append("")
    if not findings:
        out.append("PASS: no ADA section 405 problems found.")
    for finding in findings:
        where = f"line {finding.line}: " if finding.line else ""
        out.append(f"{finding.level.upper():4} [{finding.rule}] {where}{finding.message}")
    fails = sum(1 for f in findings if f.level == "fail")
    warns = len(findings) - fails
    if findings:
        out.append("")
        out.append(f"{fails} failure(s), {warns} warning(s).")
    return "\n".join(out)


def format_summary(ramp, unit="in", thickness="4in", waste=5):
    rail = rail_length_mm(ramp)
    lines = [
        f"Ramp: {ramp.name}",
        f"Total rise:          {fmt_len(profile_points(ramp)[-1][1], unit)}",
        f"Total horizontal:    {fmt_len(sum(r.horizontal for r in ramp.runs), unit)}",
        f"Landing length:      {fmt_len(sum(l.length for l in ramp.landings), unit)}",
        f"Footprint length:    {fmt_len(footprint_mm(ramp), unit)}",
        f"Handrail (each side): {fmt_len(rail, unit)} (approximate)",
    ]
    thickness_mm = parse_length(thickness, "in")
    volume = concrete_m3(ramp, thickness_mm, waste)
    if volume is not None:
        yards = volume / Fraction(9144, 10000) ** 3
        lines.append(f"Concrete slab:       {float(volume):.3f} m3 "
                     f"({float(yards):.3f} yd3) at {thickness} thick, {waste}% waste")
    return "\n".join(lines)


def _read(path):
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _load(path, default_unit):
    ramp, errors = parse_ramp(_read(path), default_unit)
    if errors:
        for number, message in errors:
            where = f"line {number}" if number else "file"
            print(f"{path}: {where}: {message}", file=sys.stderr)
        return None
    return ramp


def build_parser():
    parser = argparse.ArgumentParser(
        prog="rampcheck",
        description="Offline accessible-ramp planner and ADA section 405 checker.")
    parser.add_argument("--version", action="version", version=f"rampcheck {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="check a ramp file against ADA 405 limits")
    check.add_argument("file", help="ramp file, or - for stdin")
    check.add_argument("--unit", choices=["in", "mm", "cm", "m", "ft"], default="in")
    check.add_argument("--strict", action="store_true", help="treat warnings as failures")

    summary = sub.add_parser("summary", help="totals, handrail length and concrete volume")
    summary.add_argument("file")
    summary.add_argument("--unit", choices=["in", "mm", "cm", "m", "ft"], default="in")
    summary.add_argument("--thickness", default="4in", help="slab thickness, e.g. 100mm")
    summary.add_argument("--waste", type=float, default=5.0, help="waste percent")

    profile = sub.add_parser("profile", help="draw the side profile in ASCII or SVG")
    profile.add_argument("file")
    profile.add_argument("--svg", metavar="OUT", help="write an SVG profile to OUT")
    profile.add_argument("--unit", choices=["in", "mm", "cm", "m", "ft"], default="in")

    solve_cmd = sub.add_parser("solve", help="minimum compliant ramp for a target rise")
    solve_cmd.add_argument("--rise", required=True, help="total rise, e.g. 36in or 900mm")
    solve_cmd.add_argument("--runs", type=int, help="number of runs (default: fewest that comply)")
    solve_cmd.add_argument("--unit", choices=["in", "mm", "cm", "m", "ft"], default="in")

    for command in (check, summary, profile):
        command.add_argument("--units", dest="default_unit", default="in",
                             choices=["in", "mm", "cm", "m", "ft"],
                             help="unit for bare numbers in the file (default: in)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.command == "solve":
        try:
            rise = parse_length(args.rise, "in")
            result = solve(rise, runs=args.runs)
        except (ParseError, ValueError) as exc:
            print(f"rampcheck: {exc}", file=sys.stderr)
            return EXIT_INPUT_ERROR
        unit = args.unit
        print(f"Target rise:         {fmt_len(rise, unit)}")
        print(f"Runs:                {result['runs']} (fewest possible: {result['minimum_runs']})")
        print(f"Rise per run:        {fmt_len(result['per_run_rise'], unit)}")
        print(f"Horizontal per run:  {fmt_len(result['per_run_horizontal'], unit)} (slope 1:12)")
        print(f"Landings:            {result['landings']} x {fmt_len(MIN_LANDING_MM, unit)} minimum")
        print(f"Footprint length:    {fmt_len(result['footprint'], unit)}")
        return EXIT_OK

    ramp = _load(args.file, args.default_unit)
    if ramp is None:
        return EXIT_INPUT_ERROR

    if args.command == "check":
        findings = check_ramp(ramp)
        print(format_report(ramp, findings, args.unit))
        failed = any(f.level == "fail" for f in findings)
        if args.strict:
            failed = failed or bool(findings)
        return EXIT_VIOLATIONS if failed else EXIT_OK

    if args.command == "summary":
        try:
            print(format_summary(ramp, args.unit, args.thickness, args.waste))
        except ParseError as exc:
            print(f"rampcheck: --thickness: {exc}", file=sys.stderr)
            return EXIT_INPUT_ERROR
        return EXIT_OK

    if args.command == "profile":
        if args.svg:
            with open(args.svg, "w", encoding="utf-8") as handle:
                handle.write(svg_profile(ramp, args.unit))
            print(f"wrote {args.svg}")
        else:
            print(ascii_profile(ramp))
        return EXIT_OK

    return EXIT_INPUT_ERROR


if __name__ == "__main__":
    sys.exit(main())
