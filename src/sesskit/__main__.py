"""Allow ``python -m sesskit ...`` as an alias for the ``sesskit`` entry point."""

from sesskit.cli import main

if __name__ == "__main__":
    main()
