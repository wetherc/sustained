"""
Tests that run against a real database server.

Each module here needs a server. When the environment variable that points
at one is missing, its tests skip and say which variable to set. Set
SUSTAINED_TEST_STRICT=1 to turn those skips into failures, which is what
matrix.py does, so a server that never started cannot pass quietly.
"""
