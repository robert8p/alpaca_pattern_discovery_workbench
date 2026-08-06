from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence


class SqlBindingError(ValueError):
    """Raised when a SQL string is not safe for Psycopg parameter binding."""


@dataclass(frozen=True)
class SqlBindingReport:
    placeholder_count: int
    literal_percent_count: int


def inspect_psycopg_placeholders(query: str) -> SqlBindingReport:
    """Validate Psycopg's ``%s/%b/%t`` placeholder grammar.

    Psycopg parses percent markers across the complete query string, including
    SQL quoted literals. Consequently every literal percent sign must be
    written as ``%%`` whenever parameters are supplied. This deliberately
    mirrors the driver-level failure which escaped the v1.0.7 tests.
    """
    placeholders = 0
    literal_percents = 0
    index = 0
    while index < len(query):
        if query[index] != "%":
            index += 1
            continue
        if index + 1 >= len(query):
            raise SqlBindingError("SQL ends with an unescaped '%' character")
        marker = query[index + 1]
        if marker == "%":
            literal_percents += 1
            index += 2
            continue
        if marker in {"s", "b", "t"}:
            placeholders += 1
            index += 2
            continue
        excerpt = query[max(0, index - 24): min(len(query), index + 25)].replace("\n", " ")
        raise SqlBindingError(
            "Invalid Psycopg percent marker '%{}' near {!r}; literal '%' must be written as '%%'"
            .format(marker, excerpt)
        )
    return SqlBindingReport(placeholders, literal_percents)


def validate_sql_bindings(query: str, params: Sequence[Any] | None, *, name: str = "SQL") -> SqlBindingReport:
    report = inspect_psycopg_placeholders(query)
    supplied = 0 if params is None else len(params)
    if report.placeholder_count != supplied:
        raise SqlBindingError(
            f"{name} has {report.placeholder_count} Psycopg placeholders but {supplied} parameters were supplied"
        )
    return report


def validate_many_bindings(query: str, rows: Iterable[Sequence[Any]], *, name: str = "SQL") -> SqlBindingReport:
    report = inspect_psycopg_placeholders(query)
    for row_number, row in enumerate(rows, 1):
        if len(row) != report.placeholder_count:
            raise SqlBindingError(
                f"{name} row {row_number} has {len(row)} values for {report.placeholder_count} placeholders"
            )
    return report


def execute_checked(cursor: Any, query: str, params: Sequence[Any] | None = None, *, name: str = "SQL") -> Any:
    validate_sql_bindings(query, params, name=name)
    if params is None:
        return cursor.execute(query)
    return cursor.execute(query, params)
