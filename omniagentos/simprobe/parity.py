from collections.abc import Iterable


def even_odd_counts(numbers: Iterable[int]) -> tuple[int, int]:
    evens = 0
    odds = 0

    for number in numbers:
        if number % 2 == 0:
            evens += 1
        else:
            odds += 1

    return evens, odds
