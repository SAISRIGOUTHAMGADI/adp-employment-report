"""Allow ``python -m adp_forecast`` without installing the console script.

Useful on a fresh clone that has not run an editable install yet.
"""

from .cli.app import main

if __name__ == "__main__":
    main()
