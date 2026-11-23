"""Native review-collection validation worker for the Memr flashcard engine.

Memr's backup check runs this file by path
with `python -I -B <this file> <pinned anki version>`, feeding the collection
request list on stdin. It validates a private clone against the pinned Anki
26.9.3 backend and prints one compact JSON `{ok, reason}` result.
"""

import json, os, pathlib, re, sqlite3, sys
os.umask(0o077)
if os.name == 'posix':
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
    resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024**3, 4 * 1024**3))
    # Darwin rejects RLIMIT_AS; the parent enforces a 2 GiB RSS watchdog there.
    if sys.platform != 'darwin':
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))

def result(ok, reason=None):
    print(json.dumps({'ok': ok, 'reason': reason}, separators=(',', ':')), flush=True)

try:
    import importlib.metadata
    if importlib.metadata.version('anki') != sys.argv[1]:
        raise RuntimeError('unsupported engine version')
    from anki.collection import Collection
except Exception:
    result(False, 'engine')
    sys.exit(2)

core = {
    'col': 'id crt mod scm ver dty usn ls conf models decks dconf tags',
    'notes': 'id guid mid mod usn tags flds sfld csum flags data',
    'cards': 'id nid did ord mod usn type queue due ivl factor reps lapses left odue odid flags data',
    'revlog': 'id cid usn ease ivl lastIvl factor time type',
}
def core_schema(query):
    for table, required in core.items():
        if query("select type from sqlite_schema where name=?", table) != [('table',)]:
            raise ValueError('missing collection table')
        columns = {str(row[1]).lower() for row in query('pragma table_info(' + table + ')')}
        if not set(required.lower().split()).issubset(columns):
            raise ValueError('missing collection column')

def quote(name):
    return '"' + name.replace('"', '""') + '"'

def table_shape(collection, name):
    if collection.db.all('select type from sqlite_schema where name=?', name) != [['table']]:
        raise ValueError('missing native table')
    return collection.db.all('pragma table_xinfo(' + quote(name) + ')')

sql_tokens = re.compile(r'''\s+|--[^\n]*(?:\n|$)|/\*.*?\*/|'(?:''|[^'])*'|"(?:""|[^"])*"|\x60(?:\x60\x60|[^\x60])*\x60|\[[^\]]*\]|[\w$]+|<>|!=|==|<=|>=|<<|>>|\|\||->>|->|[^\w\s]''', re.S)
ascii_lower = str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')
def normalized_sql(sql):
    if sql is None:
        return None
    tokens = []
    for match in sql_tokens.finditer(sql):
        value = match.group()
        if value.isspace() or value.startswith(('--', '/*')):
            continue
        if value.startswith("'"):
            tokens.append(('string', value[1:-1].replace("''", "'")))
        elif value.startswith(('"', '\x60', '[')):
            delimiter = value[0]
            name = value[1:-1]
            if delimiter != '[':
                name = name.replace(delimiter * 2, delimiter)
            tokens.append(('word', name.translate(ascii_lower)))
        elif value[0].isalnum() or value[0] in '_$':
            tokens.append(('word', value.translate(ascii_lower)))
        else:
            tokens.append(('operator', {'<>': '!=', '==': '='}.get(value, value)))
    return tokens

def indexes(collection, table):
    return {row[1]: [row[2], row[3], row[4], collection.db.all('pragma index_xinfo(' + quote(row[1]) + ')'),
                    normalized_sql(collection.db.scalar('select sql from sqlite_schema where type=\'index\' and name=?', row[1]))]
            for row in collection.db.all('pragma index_list(' + quote(table) + ')')}

references = {}
def validate(path, server):
    # Guard against Anki treating an empty/noncollection SQLite file as a new
    # collection. No native open or upgrade may manufacture the required core.
    # mode=rw cannot create a collection, but lets SQLite recover a genuine hot
    # rollback journal before inspecting schema. This path is the private clone.
    db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=0)
    try:
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 16 * 1024**2)
        db.execute('pragma trusted_schema=off')
        core_schema(lambda sql, *args: db.execute(sql, args).fetchall())
        rows = db.execute('select id, ver from col limit 2').fetchall()
        if len(rows) != 1 or rows[0][0] != 1 or type(rows[0][1]) is not int:
            raise ValueError('invalid collection metadata')
    finally:
        db.close()
    # Obtain the complete required schema from the pinned engine itself. Extra
    # user tables/indexes are allowed, but required columns, keys, nullability,
    # index expressions and native collations must match the trusted reference.
    if server not in references:
        reference = Collection(str(pathlib.Path.cwd() / ('schema-reference-' + str(int(server)) + '.anki2')), server=server)
        try:
            required = {row[0]: [table_shape(reference, row[0]), indexes(reference, row[0])]
                        for row in reference.db.all("select name from sqlite_schema where type='table' and name not glob 'sqlite_*'")}
            references[server] = required, reference.db.scalar('select ver from col')
        finally:
            reference.close()
    required, version = references[server]
    col = Collection(str(path), server=server)
    try:
        col.db.execute('pragma trusted_schema=off')
        # Full integrity_check also verifies index entries against table rows.
        if col.db.all('pragma integrity_check') != [['ok']]:
            raise ValueError('collection integrity failure')
        for table, (columns, expected_indexes) in required.items():
            if table_shape(col, table) != columns:
                raise ValueError('incompatible native columns')
            actual_indexes = indexes(col, table)
            if any(actual_indexes.get(name) != shape for name, shape in expected_indexes.items()):
                raise ValueError('incompatible native index')
        if col.db.scalar('select ver from col') != version:
            raise ValueError('unsupported collection schema')
    finally:
        col.close()

try:
    payload = sys.stdin.buffer.read(8 * 1024**2 + 1)
    if len(payload) > 8 * 1024**2:
        raise ValueError('validation request too large')
    requests = json.loads(payload)
    if type(requests) is not list or not 1 <= len(requests) <= 1001:
        raise ValueError('invalid collection count')
    for request in requests:
        validate(pathlib.Path(request['path']), request['server'])
    result(True)
except Exception:
    result(False, 'database')
    sys.exit(1)
