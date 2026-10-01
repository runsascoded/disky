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
