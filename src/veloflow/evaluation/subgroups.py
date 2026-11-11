"""Subgroups of acceptance criterion 5: suburbs, peak hours, rain, weekend.

Criterion 5 (design, section 4.5): no degradation of more than 15 % on a
subgroup. Degradation is measured on the ratio of the model's mean absolute
error to persistence's (the three counts summed): a subgroup degrades when its
ratio is more than 15 % above the ratio on all examples. Comparing ratios, not
raw errors, keeps groups that are simply harder for everyone (peak hours) from
counting as a degradation of the model.
"""

import polars as pl

from veloflow.evaluation.examples import TARGETS
from veloflow.weather.grid import nearest_point

PARIS = "Europe/Paris"
SUBGROUPS: tuple[str, ...] = ("suburbs", "peak_hours", "rain", "weekend")
# Paris stations have codes below 21000 (arrondissement x 1000 + n), plus the
# 600xx block (two stations on the Seine banks, Invalides and Concorde).
PARIS_CODE_LIMIT = 21_000
PARIS_SPECIAL_BLOCK = 60
PEAK_HOURS: tuple[tuple[int, int], ...] = ((7, 10), (17, 20))
RAIN_MM = 0.1
MAX_DEGRADATION = 0.15


def station_areas(stations: pl.DataFrame) -> pl.DataFrame:
    """``station_id`` and ``suburbs`` (outside Paris) from the station code."""
    code = pl.col("station_code").cast(pl.Int64, strict=False)
    paris = (code < PARIS_CODE_LIMIT) | (code // 1000 == PARIS_SPECIAL_BLOCK)
    return stations.select("station_id", (~paris).alias("suburbs"))


def add_subgroups(
    frame: pl.DataFrame, stations: pl.DataFrame, weather: pl.DataFrame | None
) -> pl.DataFrame:
    """Flag each scored example with the subgroups it belongs to.

    ``rain`` is the forecast precipitation at the target (at least 0.1 mm in
    the quarter hour) and stays null without a forecast, so that examples with
    no weather count neither as rain nor as dry.
    """
    local = pl.col("origin").dt.convert_time_zone(PARIS)
    hour = local.dt.hour()
    weekday = local.dt.weekday() <= 5
    peak = pl.any_horizontal([hour.is_between(a, b - 1) for a, b in PEAK_HOURS]) & weekday
    flagged = frame.join(station_areas(stations), on="station_id", how="left").with_columns(
        peak_hours=peak, weekend=~weekday
    )
    if weather is None or weather.height == 0:
        return flagged.with_columns(rain=pl.lit(None, dtype=pl.Boolean))
    points = pl.DataFrame(
        {
            "station_id": stations["station_id"],
            "point": [nearest_point(a, b).code for a, b in stations.select("lat", "lon").rows()],
        },
        schema={"station_id": pl.Int64, "point": pl.String},
    )
    rain = weather.select("point", "valid_at", "precipitation").sort("valid_at")
    return (
        flagged.with_row_index("_row")
        .join(points, on="station_id", how="left")
        .sort("target_at")
        .join_asof(
            rain,
            left_on="target_at",
            right_on="valid_at",
            by="point",
            strategy="backward",
            tolerance="15m",
            check_sortedness=False,
        )
        .sort("_row")
        .with_columns(rain=pl.col("precipitation") >= RAIN_MM)
        .drop("_row", "point", "valid_at", "precipitation")
    )


def _ratio(frame: pl.DataFrame) -> pl.Expr:
    model = pl.sum_horizontal([(pl.col(f"pred_{n}") - pl.col(f"y_{n}")).abs() for n in TARGETS])
    base = pl.sum_horizontal([(pl.col(n) - pl.col(f"y_{n}")).abs() for n in TARGETS])
    return (model.sum() / base.sum()).alias("ratio")


def subgroup_ratios(frame: pl.DataFrame) -> pl.DataFrame:
    """Error ratio to persistence by horizon, overall and per subgroup.

    Returns:
        ``group`` (``all`` or a subgroup), ``horizon_min``, ``n`` and
        ``ratio`` (model MAE / persistence MAE, three counts summed).
    """
    parts = [
        frame.group_by("horizon_min")
        .agg(pl.len().alias("n"), _ratio(frame))
        .with_columns(group=pl.lit("all"))
    ]
    for group in SUBGROUPS:
        parts.append(
            frame.filter(pl.col(group).fill_null(value=False))
            .group_by("horizon_min")
            .agg(pl.len().alias("n"), _ratio(frame))
            .with_columns(group=pl.lit(group))
        )
    return (
        pl.concat(parts).select("group", "horizon_min", "n", "ratio").sort("group", "horizon_min")
    )
