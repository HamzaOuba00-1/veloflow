# VéloFlow

Station-level forecasts for the Vélib' Métropole bike-sharing network in Paris:
for each of the ~1,500 stations, how many mechanical bikes, e-bikes and free
docks there will be in 15, 30 and 60 minutes, with calibrated probabilities
that the station will be empty or full.

> Work in progress. This README grows with the project.

## Status

- [x] Project scoping
- [x] Development environment
- [ ] Data collector

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

The code is released under the [MIT License](LICENSE). Vélib' Métropole data is
published under the ODbL; data terms and attribution will be documented with
the collector.
