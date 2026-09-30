from os.path import abspath
from typing import Optional
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

from .base import Base
from .migrate import add_missing_columns
from .. import config

app = None
db: Optional[SQLAlchemy] = None
cache_url = None


def init(sqlite_path: str = None) -> SQLAlchemy:
    global db
    if db:
        return db
    global app
    global cache_url
    app = Flask(__name__)
    if not sqlite_path:
        sqlite_path = config.SQLITE_PATH
    cache_path = abspath(sqlite_path)
    cache_url = f'sqlite:///{cache_path}'
    app.config['SQLALCHEMY_DATABASE_URI'] = cache_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db = SQLAlchemy(app)
    app.app_context().push()
    Base.metadata.create_all(db.engine)
    add_missing_columns(db.engine)
    return db


def reinit() -> None:
    """Rebind the DB to the current `config.SQLITE_PATH` after a root change.

    A no-op until the DB has actually been initialized — the next `init()` then
    picks up the new path on its own. Registered as a `config.on_root_change`
    hook so opening a library re-points the engine.
    """
    global db, app, cache_url
    if db is None:
        return
    db.engine.dispose()
    db = None
    app = None
    cache_url = None
    init()


config.on_root_change(reinit)
