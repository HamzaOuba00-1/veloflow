# VéloFlow

[![CI](https://github.com/HamzaOuba00-1/veloflow/actions/workflows/ci.yml/badge.svg)](https://github.com/HamzaOuba00-1/veloflow/actions/workflows/ci.yml)

Station-level forecasts for the Vélib' Métropole bike-sharing network in Paris:
for each of the ~1,500 stations, how many mechanical bikes, e-bikes and free
docks there will be in 15, 30 and 60 minutes, with calibrated probabilities
that the station will be empty or full.

> Work in progress. This README grows with the project.

## Status

- [x] Project scoping
- [x] Development environment
- [x] Data collector, running 24/7 since 8 November 2026 ([deployment](docs/deployment.md))
- [x] Data quality and context data ([quality](docs/data/quality.md), [weather](docs/data/weather.md), [calendar](docs/data/calendar.md), [bike counts](docs/data/counters.md))
- [x] Exploratory data analysis ([findings](docs/data/eda.md))
- [ ] Backtesting and baselines

## Development

Requirements: [uv](https://docs.astral.sh/uv/) and Git. uv installs the right
Python version by itself.

```bash
uv sync                               # create .venv from uv.lock
uv run pre-commit install             # run checks before every commit
uv run pytest --cov                   # tests and coverage
uv run pre-commit run --all-files     # format, lint and type-check everything
```

## License

The code is released under the [MIT License](LICENSE).

Data sources and their terms:

- Vélib' Métropole availability: [Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/).
- Weather forecasts: [Weather data by Open-Meteo.com](https://open-meteo.com/),
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), from Météo-France
  models. Free API, non-commercial use.
- Bike counts: Ville de Paris, Direction de la Voirie et des Déplacements,
  [ODbL](https://opendatacommons.org/licenses/odbl/1-0/).
- French school calendar: Ministère de l'Éducation nationale,
  [Licence Ouverte 2.0](https://www.etalab.gouv.fr/licence-ouverte-open-licence/).
