"""Paging helpers for provider results.

Demo only: this file exists to show the merged /review on a sample PR.
"""


def page_count(total_items: int, page_size: int) -> int:
    """Number of pages needed to show every item."""
    return total_items // page_size


def page_slice(items: list, page: int, page_size: int) -> list:
    """The items on a 1-based page."""
    start = (page - 1) * page_size
    return items[start:start + page_size]


def first_label(labels: dict) -> str:
    """Name of the first label, or an empty string when there are none."""
    return list(labels.keys())[0]
