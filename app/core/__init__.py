"""Calculation core of Deputat.

Pure Python (standard library only). Nothing in this package may import from
``app.db``, FastAPI or SQLAlchemy; services translate database rows into the
plain data types defined in :mod:`app.core.types` and call the functions here.

All durations are integer seconds.
"""
