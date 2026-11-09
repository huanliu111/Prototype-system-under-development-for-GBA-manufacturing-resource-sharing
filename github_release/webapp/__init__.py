"""Reviewer-facing web prototype for the manufacturing double-auction testbed."""

__all__ = ["create_app"]


def create_app(*args, **kwargs):
    from .app import create_app as factory

    return factory(*args, **kwargs)
