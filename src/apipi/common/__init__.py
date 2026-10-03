"""Code that both the API and the worker use.

Nothing here imports FastAPI, SQLAlchemy, asyncpg, `apipi.store`, or
`apipi.api`, so a worker process can import it without loading the API.
"""
