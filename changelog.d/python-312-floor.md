bump: minor

Lower the Python floor from 3.14 to 3.12, the interpreter Google Colab provides. The only
3.14-only syntax in the library was the unparenthesized `except A, B:` form (PEP 758),
now written `except (A, B):` in 54 files; ruff, black and mypy target 3.12; `zeocore
doctor` checks the new floor; CI also runs the suite on 3.12; the README, quickstart and
contributor docs state the new floor. The full suite passes on CPython 3.12 and 3.14.
