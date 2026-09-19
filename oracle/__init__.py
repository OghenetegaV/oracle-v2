"""Oracle — Package Root

Purpose:
    Root of the one canonical Oracle Python package. Holds the package version and nothing else;
    functionality lives in subpackages.

Role in Oracle:
    Namespace for the V2 architecture: oracle.core (the engineering domain model) and
    oracle.adapters (import adapters that translate other representations into it). Future
    engines and services will be added under this same package rather than a competing one.
    This directory is the package, not a copy of the repository.

Dependencies:
    None.

Consumers:
    oracle.core.project (records __version__ in project files); tests.

Status:
    Core.

Migration:
    Remains the single package root.
"""

__version__ = "2.0.0.dev0"
