"""The exception the suite-wide network tripwire raises.

It lives outside ``conftest.py`` because pytest imports that file as a module
named ``conftest``: a test importing ``tests.conftest`` would get a second copy
of the class, and ``assertRaises`` would not recognise the one actually raised.
"""


class NetworkBlocked(RuntimeError):
    """A test tried to reach the network."""
