from __future__ import annotations

from .config import SqlctlConfig
from .database import QueryResult, execute_query, get_connection_config

DEFAULT_QUERY_STORE_TABLE = "query_store.dbo.sql_queries"
DEFAULT_QUERY_STORE_SQL_COLUMN = "SQL_Query"


def stored_query_sql(
    config: SqlctlConfig,
    *,
    connection_name: str,
    table: str = DEFAULT_QUERY_STORE_TABLE,
    sql_column: str = DEFAULT_QUERY_STORE_SQL_COLUMN,
    query_name: str,
    connection_name_value: str,
    app_name: str,
) -> str | None:
    result = stored_query_row(
        config,
        connection_name=connection_name,
        table=table,
        sql_column=sql_column,
        query_name=query_name,
        connection_name_value=connection_name_value,
        app_name=app_name,
    )
    if not result.rows:
        return None
    value = result.rows[0].get("sql_text")
    return str(value) if value is not None else None


def stored_query_row(
    config: SqlctlConfig,
    *,
    connection_name: str,
    table: str,
    sql_column: str,
    query_name: str,
    connection_name_value: str,
    app_name: str,
) -> QueryResult:
    lookup_sql = stored_query_lookup_sql(config, connection_name, table, sql_column)
    return execute_query(
        config,
        connection_name=connection_name,
        sql=lookup_sql,
        parameters={
            "query_name": query_name,
            "connection_name": connection_name_value,
            "app_name": app_name,
        },
    )


def stored_query_lookup_sql(
    config: SqlctlConfig, connection_name: str, table: str, sql_column: str
) -> str:
    driver = get_connection_config(config, connection_name).driver
    if driver == "mssql":
        return (
            f"select top 1 {sql_column} as sql_text\n"
            f"from {table}\n"
            "where Query_Name = :query_name\n"
            "and Connection_Name = :connection_name\n"
            "and App_Name = :app_name"
        )
    return (
        f"select {sql_column} as sql_text\n"
        f"from {table}\n"
        "where Query_Name = :query_name\n"
        "and Connection_Name = :connection_name\n"
        "and App_Name = :app_name\n"
        "limit 1"
    )
