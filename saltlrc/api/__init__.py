"""API routes, grouped by domain — layer 3 dispatch.

This package splits the routes by domain. Each module here owns a
group of routes and exposes a single ``handle(self, path, data)`` that returns
True when it answered, so ``dispatch`` tries them in order and stops at the
first module that handles the path.

Order matters only in that a module must not swallow another's path; within a
module the order is fixed, so each path resolves through exactly one handler.
"""

from . import align, debug, login, lyrics, project, projects, system

MODULES = (debug, login, align, lyrics, project, projects, system)


def dispatch(self, path, data):
    """Answer a POST body. Returns True when a module handled it."""
    for module in MODULES:
        if module.handle(self, path, data):
            return True
    return False
