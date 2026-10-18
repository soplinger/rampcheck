# rampcheck

**Plan and check accessible ramps offline.** Describe a ramp in a plain-text file, and rampcheck tests it against the ADA 2010 Standards §405 limits, solves the smallest compliant ramp for any rise, estimates handrail length and concrete, and draws the side profile.

No servers, no accounts, no network calls. Python 3 standard library only.

> **Note:** rampcheck is a planning aid. The limits are set as constants at the top of `rampcheck.py`. Check them against your local code and have a qualified person sign off on real construction.

## Features

- **Plain-text ramp files** with runs (`run <rise> <horizontal>`), landings (`landing <length> <width> [turn]`) and site settings, all with `#` comments.
- **Units per file or per value**: `in`, `ft`, `mm`, `cm`, `m`, mixed fractions such as `2 1/2 ft`. A `units:` line sets the default for the whole file.
- **Line-numbered input errors**, reported to stderr with exit code 2.
- **Exact slope maths**: every length is an exact fraction of a millimetre, and slopes are shown as `1:N`.
- **Running-slope check** against 1:12, with a warning when a run is within 5% of the limit.
- **Rise-per-run check** against 30 in.
- **Landing checks**: a landing at the bottom and at the top, one between every pair of runs, at least 60 in long, at least as wide as the ramp, and 60 × 60 in for turning landings.
- **Clear-width check** against 36 in.
- **Cross-slope check** against 1:48.
- **Handrail checks**: both sides required for any run over 6 in rise or 72 in horizontal, height between 34 and 38 in, and extensions of at least 12 in.
- **Handrail length estimate** per side, covering sloped runs, intermediate landings and both extensions.
- **Minimum-ramp solver** (`solve`): the fewest runs for a target rise, rise and run per flight, landing count and footprint.
- **Concrete slab volume** in m³ and yd³, using vertical slab thickness and an optional waste allowance.
- **ASCII side profile** in the terminal.
- **SVG side profile** export for documents.
- **CI-friendly exit codes**: `0` pass, `1` violations (or any warning with `--strict`), `2` input error.

## Quick start

The repository includes a sample ramp in `examples/entrance.ramp`:

```text
units: in
name: Front entrance
width: 48
cross-slope: 1:50
handrails: both
handrail-height: 36
handrail-extension: 12

landing 60 60          # bottom landing
run 15 200             # rise 15 in over 200 in: 1:13.33
landing 60 60 turn     # turning landing
run 15 200
landing 60 60          # top landing
```

Check it, then summarise it, draw it and solve a fresh ramp:

```sh
python3 rampcheck.py check examples/entrance.ramp
python3 rampcheck.py summary examples/entrance.ramp --thickness 100mm --unit m
python3 rampcheck.py profile examples/entrance.ramp
python3 rampcheck.py profile examples/entrance.ramp --svg entrance.svg
python3 rampcheck.py solve --rise 36in
```

`check --strict` turns warnings into failures, which is useful in CI. Use `--units mm` (or `cm`, `m`, `ft`) as the default for bare numbers in a file that has no `units:` line.

## How to run

Requires Python 3.10 or newer (the code uses `X | None` annotations). Nothing needs installing to run it.

```sh
python3 rampcheck.py --help
python3 rampcheck.py check examples/entrance.ramp; echo "exit code: $?"
```

A bad file gets line-numbered errors:

```text
broken.ramp: line 4: cannot read length 'fifteen'
```

## How to test

The tests use pytest, the only dependency:

```sh
python3 -m pip install pytest
python3 -m pytest
```

The suite covers unit parsing, the parser's line-numbered errors, every compliance rule (passing and failing), the solver, rail and concrete maths, the ASCII and SVG output, and the command-line exit codes.

## Layout

```text
rampcheck.py            the whole tool: parser, checks, geometry, solver, CLI
examples/entrance.ramp  a sample ramp that passes every check
tests/                  pytest suite
```

## Scope and limits

- Limits come from ADA 2010 Standards §405 and are hard-coded as named constants so they're easy to change.
- Handrail length is an estimate: it doesn't model rail returns, landing-level details or rail profile.
- Concrete volume assumes a slab of uniform vertical thickness under the sloped runs.
- Only ramp runs and landings are modelled. Steps, curbs and changes in level are out of scope.
