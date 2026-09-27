from alembic import context

connection = context.config.attributes["connection"]
context.configure(connection=connection, version_table="design_agent_schema_version")
with context.begin_transaction():
    context.run_migrations()
