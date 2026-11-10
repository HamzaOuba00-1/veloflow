"""Data quality rules applied on top of the silver layer (docs/data/quality.md).

The silver layer keeps every valid feed version as received. The rules live
here, as pure functions applied when the data is read, so that changing a rule
never requires rebuilding the silver files.
"""
