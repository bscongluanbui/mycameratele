"""Persistent single administrator credentials, separate from the video catalog."""
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
from contextlib import contextmanager


PBKDF2_ITERATIONS = 600000
USERNAME_PATTERN = re.compile(r'[A-Za-z0-9_.-]{3,64}\Z')


class DashboardAuth:
    """Bootstrap once, hash passwords, and version credentials to revoke sessions.

    A connection belongs to only one operation, so HTTP request threads and
    independently started dashboard processes can safely share the database.
    SQLite's immediate transaction serializes bootstrap and credential changes.
    """

    def __init__(self, state_dir):
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / 'dashboard_auth.sqlite'
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        os.chmod(self.path, 0o600)
        with self.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('''CREATE TABLE IF NOT EXISTS administrator (
                id INTEGER PRIMARY KEY CHECK(id=1),
                username TEXT NOT NULL,
                password_salt BLOB NOT NULL,
                password_hash BLOB NOT NULL,
                iterations INTEGER NOT NULL,
                password_change_required INTEGER NOT NULL CHECK(password_change_required IN (0,1)),
                version INTEGER NOT NULL CHECK(version>=1)
            )''')
            if connection.execute('SELECT 1 FROM administrator WHERE id=1').fetchone() is None:
                salt = secrets.token_bytes(32)
                digest = self.password_hash('admin', salt, PBKDF2_ITERATIONS)
                connection.execute('INSERT INTO administrator VALUES (1,?,?,?,?,1,1)',
                                   ('admin', salt, digest, PBKDF2_ITERATIONS))
            connection.commit()

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def password_hash(password, salt, iterations):
        return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)

    @staticmethod
    def metadata(row):
        return {'username': row['username'],
                'password_change_required': bool(row['password_change_required']),
                'version': row['version']}

    def account(self):
        with self.connection() as connection:
            row = connection.execute('SELECT username,password_change_required,version FROM administrator WHERE id=1').fetchone()
            return self.metadata(row)

    def authenticate(self, username, password):
        """Invalid username/password combinations share one verification path."""
        valid = (isinstance(username, str) and USERNAME_PATTERN.fullmatch(username) is not None
                 and isinstance(password, str) and 1 <= len(password) <= 128)
        try:
            supplied_username = username.encode('utf-8') if valid else b''
            supplied_password = password if valid else 'invalid-credential'
            supplied_password.encode('utf-8')
        except UnicodeError:
            valid = False
            supplied_username = b''
            supplied_password = 'invalid-credential'
        with self.connection() as connection:
            row = connection.execute('SELECT * FROM administrator WHERE id=1').fetchone()
        digest = self.password_hash(supplied_password, row['password_salt'], row['iterations'])
        name_matches = hmac.compare_digest(supplied_username, row['username'].encode('utf-8'))
        password_matches = hmac.compare_digest(digest, row['password_hash'])
        if valid & name_matches & password_matches:
            return self.metadata(row)
        return None

    def change(self, current_password, username, new_password, expected_version):
        if not isinstance(username, str) or USERNAME_PATTERN.fullmatch(username) is None:
            raise ValueError('Invalid username')
        if not isinstance(new_password, str) or not 8 <= len(new_password) <= 128:
            raise ValueError('New password must contain 8 to 128 characters')
        new_password.encode('utf-8')
        current_valid = isinstance(current_password, str) and 1 <= len(current_password) <= 128
        supplied_password = current_password if current_valid else 'invalid-credential'
        try:
            supplied_password.encode('utf-8')
        except UnicodeError:
            current_valid = False
            supplied_password = 'invalid-credential'
        with self.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM administrator WHERE id=1').fetchone()
            digest = self.password_hash(supplied_password, row['password_salt'], row['iterations'])
            password_matches = hmac.compare_digest(digest, row['password_hash'])
            if not current_valid or not password_matches or row['version'] != expected_version:
                raise PermissionError('Current password invalid')
            salt = secrets.token_bytes(32)
            digest = self.password_hash(new_password, salt, PBKDF2_ITERATIONS)
            connection.execute('''UPDATE administrator SET username=?,password_salt=?,password_hash=?,
                iterations=?,password_change_required=0,version=version+1 WHERE id=1''',
                (username, salt, digest, PBKDF2_ITERATIONS))
            connection.commit()
