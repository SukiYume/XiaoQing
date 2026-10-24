# Astro Tools

[中文](README.md)

Astro Tools supplies time, coordinate, unit, redshift, formula, object, and constant functions through `/astro`.

## Requirements

Commands work in group chats and private chats.
Astropy supplies time, coordinate, unit, and cosmology calculations.
SciPy supplies integrals for cosmological distance and time.
An external network connection is necessary for SIMBAD requests.

## Commands

| Command | Function |
| --- | --- |
| `/astro time ...` | ISO, JD, MJD, Unix, and current time. |
| `/astro coord ...` | ICRS, Galactic, and geocentric true ecliptic coordinates. |
| `/astro convert <value> <source-unit> <target-unit>` | Astropy physical unit conversion. |
| `/astro redshift <z>` | Planck18 distances and universe age. The range is `0 ≤ z ≤ 1100`. |
| `/astro formula ...` | Formula information and mass calculations. |
| `/astro obj <name>` | Solar System information or SIMBAD object data. |
| `/astro const [name]` | Astronomical and physical constants. |
| `/astro help` | Plugin help. |

```text
/astro time now
/astro time unix 1706616000
/astro coord 12:30:00 +15:00:00
/astro coord galactic 120 30
/astro convert 1 pc ly
/astro redshift 0.5
/astro obj M31
/astro const ly
```

`/help astro_tools` shows formula names, constant aliases, argument limits, and error examples.

## Settings and data

The plugin uses the Core time zone and shared HTTP Session.
The plugin calculates results for each request.
The framework uses the persistent data directory to establish ownership of paths.
The directory serves this purpose only.

SIMBAD requests have a client timeout and a total deadline.
The plugin validates values, coordinates, units, and redshift before calculation.

Unit parsing first uses standard Astropy letter case.
If standard parsing fails, the plugin tries the internal aliases.
`MJy` and `mJy`, `MK` and `mK`, and `Mpc` and `mpc` keep their different prefix meanings.

`formula calc luminosity` uses a main-sequence approximation in solar units:

| Mass range | Luminosity |
| --- | --- |
| Below 0.43 | `0.23 M^2.3` |
| From 0.43 to below 2 | `M^4` |
| From 2 to below 20 | `1.5 M^3.5` |
| 20 or more | `2500 M` |

Each boundary uses the interval on its right.
The result is an empirical estimate.
`formula calc lifetime` uses a separate lifetime approximation.

## Fault isolation

1. Send `/astro const c` to test the basic command.
2. Send `/astro time now` to test Astropy.
3. Send `/astro redshift 0.5` to test SciPy cosmology integrals.
4. Send `/astro obj M31` to test the SIMBAD connection.
5. Examine parsed fields, unit errors, and external request status in the log.

## Verification

```bash
python -m pytest tests/plugins/astro_tools/test_astro_tools.py -q
python -m ruff check plugins/astro_tools tests/plugins/astro_tools/test_astro_tools.py
```
