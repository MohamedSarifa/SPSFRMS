"""Retired: bulk password resets are intentionally unavailable."""

import sys


def main():
    print(
        "This bulk password reset utility has been retired. "
        "Use the admin-only reset action for one explicitly selected account."
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
