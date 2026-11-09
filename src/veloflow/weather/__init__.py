"""Weather forecasts from Open-Meteo, used as model features (decision D23).

Only forecasts are used, never observations: at prediction time the model can
only know the weather that was forecast, so training on observed weather would
leak the future into the features.
"""
