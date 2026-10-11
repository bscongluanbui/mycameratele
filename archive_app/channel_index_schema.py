"""Transactional widening of the durable navigation-node CHECK constraint.

Only navigation metadata is touched. Existing message references, uncertain
send states, pins and AUTOINCREMENT high-water marks survive the migration.
"""
import re
import secrets


_TABLE = 'channel_index_messages'
_OLD_KINDS = frozenset(('root', 'year', 'month', 'day'))
_NEW_KINDS = _OLD_KINDS | frozenset(('years', 'hour'))
_COLUMNS = (
    'id', 'camera_id', 'channel_chat_id', 'index_type', 'period_key',
    'tg_message_id', 'tg_message_url', 'state', 'revision',
    'last_render_hash', 'pinned', 'attempt_id', 'last_error', 'updated_at',
)
_CHECK = re.compile(
    r'''CHECK\s*\(\s*(?:index_type|"index_type"|`index_type`|\[index_type\])
        \s+IN\s*\(\s*(?P<values>'(?:[^']|'')*'
        (?:\s*,\s*'(?:[^']|'')*')*)\s*\)\s*\)''',
    re.IGNORECASE | re.VERBOSE,
)
_CREATE = re.compile(
    r'''\A\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?
        (?P<name>channel_index_messages|"channel_index_messages"|
        `channel_index_messages`|\[channel_index_messages\])(?=\s*\()''',
    re.IGNORECASE | re.VERBOSE,
)
_TOKENS = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`|\[[^]]*\]|[A-Za-z_][A-Za-z_0-9]*|\d+|[^\s]")


def _quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def _constraint(sql):
    checks = list(_CHECK.finditer(sql))
    if len(checks) != 1 or _CREATE.match(sql) is None:
        raise ValueError('Unexpected channel index message table definition')
    match = checks[0]
    values = re.findall(r"'((?:[^']|'')*)'", match.group('values'))
    kinds = frozenset(value.replace("''", "'") for value in values)
    if len(kinds) != len(values):
        raise ValueError('Duplicate channel index CHECK kind')
    return match, kinds


def _canonical(sql, constraint):
    # Compare every other declaration/constraint, not merely column names.
    sql = sql[:constraint.start()] + 'CHECK (SCHEMA_KIND_PLACEHOLDER)' + sql[constraint.end():]
    sql = re.sub(r'\bIF\s+NOT\s+EXISTS\b', '', sql, count=1, flags=re.IGNORECASE)
    tokens = []
    for token in _TOKENS.findall(sql):
        if token == ';':
            continue
        if token.startswith("'"):
            tokens.append(token)
        elif token.startswith(('"', '`', '[')):
            tokens.append(token[1:-1].replace('""', '"').lower())
        else:
            tokens.append(token.lower())
    return tuple(tokens)


def _reject_external_foreign_keys(conn):
    tables = conn.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name<>?", (_TABLE,)).fetchall()
    for table in tables:
        for key in conn.execute('PRAGMA foreign_key_list(' + _quote(table[0]) + ')'):
            if key[2].lower() == _TABLE:
                raise ValueError('Channel index migration requires no external foreign-key references')


def ensure_index_message_schema(conn, create_statement):
    """Ensure ``years``/``hour`` nodes are accepted; return whether migrated.

    A savepoint preserves an already-open caller transaction (including on
    failure). A no-row UPDATE obtains the write lock *before* reading the DDL,
    so concurrent constructors cannot independently rebuild stale schemas.
    The SQLite connection's configured busy timeout remains authoritative.
    The caller must first execute its CREATE TABLE IF NOT EXISTS statement.
    """
    target_constraint, target_kinds = _constraint(create_statement)
    if target_kinds != _NEW_KINDS:
        raise ValueError('Expected complete channel index navigation CHECK kinds')
    expected = _canonical(create_statement, target_constraint)
    savepoint = 'channel_index_schema_' + secrets.token_hex(8)
    conn.execute('SAVEPOINT ' + savepoint)
    try:
        conn.execute('UPDATE channel_index_messages SET id=id WHERE 0')
        row = conn.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (_TABLE,)).fetchone()
        if row is None or not row[0]:
            raise ValueError('Missing channel index message table definition')
        old_sql = row[0]
        old_constraint, kinds = _constraint(old_sql)
        columns = tuple(row[1] for row in conn.execute('PRAGMA table_xinfo(channel_index_messages)'))
        if columns != _COLUMNS or _canonical(old_sql, old_constraint) != expected:
            raise ValueError('Unexpected channel index columns or constraints; migration left unchanged')
        if kinds == _NEW_KINDS:
            conn.execute('RELEASE SAVEPOINT ' + savepoint)
            return False
        if kinds != _OLD_KINDS:
            raise ValueError('Unexpected channel index CHECK kinds; migration left unchanged')

        _reject_external_foreign_keys(conn)
        objects = conn.execute("""SELECT sql FROM sqlite_schema
            WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL
            ORDER BY type,name""", (_TABLE,)).fetchall()
        sequence = conn.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (_TABLE,)).fetchall()
        if len(sequence) > 1 or (sequence and (type(sequence[0][0]) is not int or sequence[0][0] < 0)):
            raise ValueError('Unexpected channel index AUTOINCREMENT sequence')

        temporary = 'channel_index_messages_migration_' + secrets.token_hex(8)
        widened_sql = old_sql[:old_constraint.start()] + target_constraint.group(0) + old_sql[old_constraint.end():]
        name = _CREATE.match(widened_sql)
        replacement = widened_sql[:name.start('name')] + _quote(temporary) + widened_sql[name.end('name'):]
        conn.execute(replacement)
        quoted_columns = ','.join(_quote(column) for column in _COLUMNS)
        conn.execute('INSERT INTO ' + _quote(temporary) + ' (' + quoted_columns + ') SELECT '
                     + quoted_columns + ' FROM channel_index_messages')
        conn.execute('DROP TABLE channel_index_messages')
        conn.execute('ALTER TABLE ' + _quote(temporary) + ' RENAME TO channel_index_messages')
        # ALTER/DROP update sqlite_sequence automatically. Restore its original
        # presence/value explicitly, including deleted-ID gaps above MAX(id).
        conn.execute('DELETE FROM sqlite_sequence WHERE name=?', (_TABLE,))
        if sequence:
            conn.execute('INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)', (_TABLE, sequence[0][0]))
        for definition in objects:
            conn.execute(definition[0])
        conn.execute('RELEASE SAVEPOINT ' + savepoint)
        return True
    except BaseException:
        # Never commit or roll back the caller's unrelated pending writes.
        try:
            conn.execute('ROLLBACK TO SAVEPOINT ' + savepoint)
        finally:
            conn.execute('RELEASE SAVEPOINT ' + savepoint)
        raise
