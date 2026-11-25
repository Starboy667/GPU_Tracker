"""Data access. Each module wraps one aggregate; every function takes an open connection, so a
caller decides the transaction boundary (see ``Database.connect``)."""
