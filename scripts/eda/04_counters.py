"""Exploration 3.4: how cycling demand in Paris reacts to the calendar and the weather.

Our own collection holds a few days; the City's bike counters hold ten years
(``veloflow.context.counters``). This script measures, on 2018-2025:

1. the weekly cycle: Saturday, Sunday and public holidays against working days;
2. school holidays, August and the months of the year;
3. the daily profile (hour of day) on working days, weekends and in August;
4. the effect of daily rain and temperature.

Effects are **ratios within a counter**: each counter-day is divided by the
median of the same counter's ordinary working days (Tuesday to Thursday, not a
holiday) of the same year, and ratios are summarised by their median over
counters. The counter network changes every year; ratios do not depend on how
many counters there are.

2020 is excluded (lockdowns), and so is the first half of 2021 (curfews).
Daily weather comes from the Open-Meteo archive (ERA5 reanalysis, observed
weather): fine to *measure* an effect, never used as a model feature (only
forecasts are, docs/data/weather.md). It is downloaded once and cached.

Usage:
    uv run python scripts/eda/04_counters.py --data-dir data
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import httpx
import matplotlib as mpl

mpl.use("Agg")
import matplotlib.pyplot as plt
import polars as pl

from veloflow.context.calendar import daily_calendar

PARIS = "Europe/Paris"
START, END = date(2018, 1, 1), date(2025, 12, 31)
EXCLUDED = [(date(2020, 1, 1), date(2021, 6, 30))]
ARCHIVE_URL = (
    "https://archive-api.open-meteo.com/v1/archive?latitude=48.8566&longitude=2.3522"
    "&daily=precipitation_sum,temperature_2m_mean&timezone=Europe%2FParis"
    f"&start_date={START}&end_date={END}"
)
# A counter must have most of its hours on a day for the day to count.
MIN_HOURS_PER_DAY = 22


def load_counts(data_dir: Path) -> pl.DataFrame:
    """Hourly counts of 2018-2025 in local time, outliers and excluded periods removed."""
    hourly = pl.read_parquet(data_dir / "silver" / "bike_counts" / "year=*" / "bike_counts.parquet")
    local = hourly.with_columns(
        local=pl.col("hour_start").dt.convert_time_zone(PARIS)
    ).with_columns(day=pl.col("local").dt.date(), hour=pl.col("local").dt.hour())
    in_window = pl.col("day").is_between(START, END)
    for first, last in EXCLUDED:
        in_window &= ~pl.col("day").is_between(first, last)
    local = local.filter(in_window)
    # Outliers: more than five times the counter's 99th percentile (sensor
    # glitches such as the 8,190 bikes an hour seen in 2023).
    limit = pl.max_horizontal(pl.lit(50.0), 5 * pl.col("count").quantile(0.99).over("counter_id"))
    outliers = local.filter(pl.col("count") > limit)
    print(f"hourly rows: {local.height:,}, outliers removed: {outliers.height}")
    return local.filter(pl.col("count") <= limit)


def daily_ratios(hourly: pl.DataFrame) -> pl.DataFrame:
    """Counter-days with their ratio to the counter's ordinary working days."""
    days = (
        hourly.group_by("counter_id", "day")
        .agg(pl.col("count").sum().alias("count"), pl.len().alias("hours"))
        .filter(pl.col("hours") >= MIN_HOURS_PER_DAY)
    )
    calendar = daily_calendar(START, END)
    days = days.join(calendar, left_on="day", right_on="date", how="left")
    ordinary = (
        pl.col("weekday").is_between(1, 3)  # Tuesday-Thursday (0 = Monday)
        & ~pl.col("is_public_holiday")
        & ~pl.col("is_school_holiday")
    )
    baseline = (
        days.filter(ordinary)
        .group_by("counter_id", pl.col("day").dt.year().alias("year"))
        .agg(pl.col("count").median().alias("baseline"))
        .filter(pl.col("baseline") >= 100)  # counters with real traffic
    )
    return (
        days.with_columns(year=pl.col("day").dt.year())
        .join(baseline, on=["counter_id", "year"])
        .with_columns(ratio=pl.col("count") / pl.col("baseline"))
    )


def summary(frame: pl.DataFrame, label: str) -> dict[str, object]:
    """Median ratio over counter-days, with its interquartile range."""
    stats = frame.select(
        pl.col("ratio").median().alias("median"),
        pl.col("ratio").quantile(0.25).alias("q1"),
        pl.col("ratio").quantile(0.75).alias("q3"),
        pl.len().alias("counter_days"),
    ).row(0, named=True)
    return {
        "category": label,
        **{k: round(v, 2) if isinstance(v, float) else v for k, v in stats.items()},
    }


def calendar_effects(days: pl.DataFrame) -> pl.DataFrame:
    """Median ratio for each kind of day."""
    working = pl.col("weekday") <= 4  # 0 = Monday
    no_holiday = ~pl.col("is_public_holiday") & ~pl.col("is_school_holiday")
    august = pl.col("day").dt.month() == 8
    rows = [
        summary(days.filter(working & no_holiday & ~august), "working day, no holiday"),
        summary(days.filter((pl.col("weekday") == 0) & no_holiday), "Monday"),
        summary(days.filter((pl.col("weekday") == 4) & no_holiday), "Friday"),
        summary(days.filter((pl.col("weekday") == 5) & ~august), "Saturday"),
        summary(days.filter((pl.col("weekday") == 6) & ~august), "Sunday"),
        summary(days.filter(working & pl.col("is_public_holiday")), "public holiday (weekday)"),
        summary(
            days.filter(
                working & pl.col("is_school_holiday") & ~pl.col("is_public_holiday") & ~august
            ),
            "school holiday weekday (not August)",
        ),
        summary(days.filter(working & august & ~pl.col("is_public_holiday")), "August weekday"),
    ]
    return pl.DataFrame(rows)


def monthly(days: pl.DataFrame) -> pl.DataFrame:
    """Median ratio of working days without holiday, by month."""
    return (
        days.filter((pl.col("weekday") <= 4) & ~pl.col("is_public_holiday"))
        .group_by(pl.col("day").dt.month().alias("month"))
        .agg(
            pl.col("ratio").median().round(2).alias("median_ratio"), pl.len().alias("counter_days")
        )
        .sort("month")
    )


def weather(data_dir: Path) -> pl.DataFrame | None:
    """Daily observed weather in Paris, downloaded once and cached."""
    cache = data_dir / "exploration" / "weather_daily_2018_2025.json"
    if not cache.exists():
        try:
            response = httpx.get(ARCHIVE_URL, timeout=60.0)
            response.raise_for_status()
        except httpx.HTTPError as error:
            print(f"weather archive unavailable ({error!r}): weather effects skipped")
            return None
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(response.content)
    daily = json.loads(cache.read_text(encoding="utf-8"))["daily"]
    return pl.DataFrame(
        {
            "day": pl.Series(daily["time"]).str.to_date(),
            "rain_mm": daily["precipitation_sum"],
            "temp_c": daily["temperature_2m_mean"],
        }
    )


def weather_effects(days: pl.DataFrame, daily_weather: pl.DataFrame) -> pl.DataFrame:
    """Median ratio of ordinary working days by daily rain and temperature."""
    ordinary = days.filter(
        (pl.col("weekday") <= 4) & ~pl.col("is_public_holiday") & ~pl.col("is_school_holiday")
    ).join(daily_weather, on="day")
    rain = ordinary.with_columns(
        bin=pl.when(pl.col("rain_mm") < 0.1)
        .then(pl.lit("rain: dry (< 0.1 mm)"))
        .when(pl.col("rain_mm") < 2)
        .then(pl.lit("rain: light (0.1-2 mm)"))
        .when(pl.col("rain_mm") < 10)
        .then(pl.lit("rain: moderate (2-10 mm)"))
        .otherwise(pl.lit("rain: heavy (>= 10 mm)"))
    )
    temperature = ordinary.with_columns(
        bin=pl.when(pl.col("temp_c") < 5)
        .then(pl.lit("temp: < 5 C"))
        .when(pl.col("temp_c") < 15)
        .then(pl.lit("temp: 5-15 C"))
        .when(pl.col("temp_c") < 25)
        .then(pl.lit("temp: 15-25 C"))
        .otherwise(pl.lit("temp: >= 25 C"))
    )
    return pl.DataFrame(
        [summary(frame, name) for (name,), frame in pl.concat([rain, temperature]).group_by("bin")]
    ).sort("category")


def plot_profiles(hourly: pl.DataFrame, days: pl.DataFrame, path: Path) -> None:
    """Share of the daily count by local hour, for three kinds of days."""
    kinds = days.select(
        "counter_id",
        "day",
        pl.when((pl.col("day").dt.month() == 8) & (pl.col("weekday") <= 4))
        .then(pl.lit("August weekday"))
        .when(pl.col("weekday") >= 5)
        .then(pl.lit("weekend"))
        .when(~pl.col("is_public_holiday") & ~pl.col("is_school_holiday"))
        .then(pl.lit("working day"))
        .alias("kind"),
    ).filter(pl.col("kind").is_not_null())
    profile = (
        hourly.join(kinds, on=["counter_id", "day"])
        .with_columns(total=pl.col("count").sum().over("counter_id", "day"))
        .filter(pl.col("total") > 0)
        .with_columns(share=pl.col("count") / pl.col("total"))
        .group_by("kind", "hour")
        .agg(pl.col("share").mean())
        .sort("hour")
    )
    figure, axis = plt.subplots(figsize=(9, 4))
    for (kind,), rows in profile.group_by("kind", maintain_order=True):
        axis.plot(rows["hour"], 100 * rows["share"], marker="o", label=str(kind))
    axis.set_xlabel("hour of the day (Paris), start of the counted hour")
    axis.set_ylabel("% of the day's bikes")
    axis.set_title("Paris bike counters 2018-2025: daily profile")
    axis.set_xticks(range(0, 24, 2))
    axis.grid(alpha=0.3)
    axis.legend()
    figure.tight_layout()
    figure.savefig(path, dpi=110)
    plt.close(figure)


def main(argv: list[str] | None = None) -> int:
    """Print the effects and write the figure."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args(argv)
    out = args.data_dir / "eda"
    out.mkdir(parents=True, exist_ok=True)
    pl.Config.set_tbl_rows(30)
    pl.Config.set_fmt_str_lengths(45)

    hourly = load_counts(args.data_dir)
    days = daily_ratios(hourly)
    print(
        f"counter-days kept: {days.height:,}, counters: {days['counter_id'].n_unique()}, "
        f"years: {sorted(days['year'].unique().to_list())}"
    )
    print("\n## 1-2. Calendar (ratio to the same counter's ordinary working days)")
    print(calendar_effects(days))
    print("\n## Months (working days without public holiday)")
    print(monthly(days))

    daily_weather = weather(args.data_dir)
    if daily_weather is not None:
        print("\n## 4. Weather (ordinary working days)")
        print(weather_effects(days, daily_weather))

    plot_profiles(hourly, days, out / "04_counter_profiles.png")
    print(f"\nfigure: {out / '04_counter_profiles.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
