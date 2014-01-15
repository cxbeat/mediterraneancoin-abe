# Copyright(C) 2011,2012,2013 by Abe developers.

# DataStore.py: back end database access for Abe.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Affero General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public
# License along with this program.  If not, see
# <http://www.gnu.org/licenses/agpl.html>.

# This module combines four functions that might be better split up:
# 1. A feature-detecting, SQL-transforming database abstraction layer
# 2. Abe's schema
# 3. Abstraction over the schema for importing blocks, etc.
# 4. Code to load data by scanning blockfiles or using JSON-RPC.

import os
import re
import errno

# bitcointools -- modified deserialize.py to return raw transaction
import BCDataStream
import deserialize
import util
import logging
import base58

SCHEMA_VERSION = "Abe35"

CONFIG_DEFAULTS = {
    "dbtype":             None,
    "connect_args":       None,
    "binary_type":        None,
    "int_type":           None,
    "upgrade":            None,
    "rescan":             None,
    "commit_bytes":       None,
    "log_sql":            None,
    "log_rpc":            None,
    "datadir":            None,
    "ignore_bit8_chains": None,
    "use_firstbits":      False,
    "keep_scriptsig":     True,
    "import_tx":          [],
    "default_loader":     "default",
}

WORK_BITS = 304  # XXX more than necessary.

CHAIN_CONFIG = [
    #{"chain":"Bitcoin",
    # "code3":"BTC", "address_version":"\x00", "magic":"\xf9\xbe\xb4\xd9"},
    #{"chain":"Testnet",
    # "code3":"BC0", "address_version":"\x6f", "magic":"\xfa\xbf\xb5\xda"},
    #{"chain":"Namecoin",
    # "code3":"NMC", "address_version":"\x34", "magic":"\xf9\xbe\xb4\xfe"},
    #{"chain":"Weeds", "network":"Weedsnet",
    # "code3":"WDS", "address_version":"\xf3", "magic":"\xf8\xbf\xb5\xda"},
    #{"chain":"BeerTokens",
    # "code3":"BER", "address_version":"\xf2", "magic":"\xf7\xbf\xb5\xdb"},
    #{"chain":"SolidCoin",
    # "code3":"SCN", "address_version":"\x7d", "magic":"\xde\xad\xba\xbe"},
    #{"chain":"ScTestnet",
    # "code3":"SC0", "address_version":"\x6f", "magic":"\xca\xfe\xba\xbe"},
    #{"chain":"Worldcoin",
    # "code3":"WDC", "address_version":"\x49", "magic":"\xfb\xc0\xb6\xdb"},
    {"chain":"Mediterraneancoin",
     "code3":"MED", "address_version":"\x33", "magic":"\xfb\xc0\xb6\xdb"},     
    #{"chain":"",
    # "code3":"", "address_version":"\x", "magic":""},
    ]

NULL_HASH = "\0" * 32
GENESIS_HASH_PREV = NULL_HASH

NULL_PUBKEY_HASH = "\0" * 20
NULL_PUBKEY_ID = 0
PUBKEY_ID_NETWORK_FEE = NULL_PUBKEY_ID

# Regex to match a pubkey hash ("Bitcoin address transaction") in
# txout_scriptPubKey.  Tolerate OP_NOP (0x61) at the end, seen in Bitcoin
# 127630 and 128239.
SCRIPT_ADDRESS_RE = re.compile("\x76\xa9\x14(.{20})\x88\xac\x61?\\Z", re.DOTALL)

# Regex to match a pubkey ("IP address transaction") in txout_scriptPubKey.
SCRIPT_PUBKEY_RE = re.compile(
    ".((?<=\x41)(?:.{65})|(?<=\x21)(?:.{33}))\xac\\Z", re.DOTALL)

# Script that can never be redeemed, used in Namecoin.
SCRIPT_NETWORK_FEE = '\x6a'

# Size of the script columns.
MAX_SCRIPT = 1000000

NO_CLOB = 'BUG_NO_CLOB'

# XXX This belongs in another module.
class InvalidBlock(Exception):
    pass
class MerkleRootMismatch(InvalidBlock):
    def __init__(ex, block_hash, tx_hashes):
        ex.block_hash = block_hash
        ex.tx_hashes = tx_hashes
    def __str__(ex):
        return 'Block header Merkle root does not match its transactions. ' \
            'block hash=%s' % (ex.block_hash[::-1].encode('hex'),)

class DataStore(object):

    """
    Bitcoin data storage class based on DB-API 2 and SQL1992 with
    workarounds to support SQLite3, PostgreSQL/psycopg2, MySQL,
    Oracle, ODBC, and IBM DB2.
    """

    def __init__(store, args):
        """
        Open and store a connection to the SQL database.

        args.dbtype should name a DB-API 2 driver module, e.g.,
        "sqlite3".

        args.connect_args should be an argument to the module's
        connect() method, or None for no argument, or a list of
        arguments, or a dictionary of named arguments.

        args.datadir names Bitcoin data directories containing
        blk0001.dat to scan for new blocks.
        """
        if args.dbtype is None:
            raise TypeError(
                "dbtype is required; please see abe.conf for examples")

        if args.datadir is None:
            args.datadir = util.determine_db_dir()
        if isinstance(args.datadir, str):
            args.datadir = [args.datadir]

        store.args = args
        store.log = logging.getLogger(__name__)
        store.sqllog = logging.getLogger(__name__ + ".sql")
        if not args.log_sql:
            store.sqllog.setLevel(logging.ERROR)
        store.rpclog = logging.getLogger(__name__ + ".rpc")
        if not args.log_rpc:
            store.rpclog.setLevel(logging.ERROR)
        store.module = __import__(args.dbtype)
        store.auto_reconnect = False
        store.init_conn()
        store._blocks = {}

        # Read the CONFIG and CONFIGVAR tables if present.
        store.config = store._read_config()

        if store.config is None:
            store.keep_scriptsig = args.keep_scriptsig
        elif 'keep_scriptsig' in store.config:
            store.keep_scriptsig = store.config.get('keep_scriptsig') == "true"
        else:
            store.keep_scriptsig = CONFIG_DEFAULTS['keep_scriptsig']

        store.refresh_ddl()

        if store.config is None:
            store.initialize()
        elif store.config['schema_version'] == SCHEMA_VERSION:
            pass
        elif args.upgrade:
            store._set_sql_flavour()
            import upgrade
            upgrade.upgrade_schema(store)
        else:
            raise Exception(
                "Database schema version (%s) does not match software"
                " (%s).  Please run with --upgrade to convert database."
                % (store.config['schema_version'], SCHEMA_VERSION))

        store._set_sql_flavour()
        store.auto_reconnect = True

        if args.rescan:
            store.sql("UPDATE datadir SET blkfile_number=1, blkfile_offset=0")

        store._init_datadirs()
        store.no_bit8_chain_ids = store._find_no_bit8_chain_ids(
            args.ignore_bit8_chains)

        store.commit_bytes = args.commit_bytes
        if store.commit_bytes is None:
            store.commit_bytes = 0  # Commit whenever possible.
        else:
            store.commit_bytes = int(store.commit_bytes)
        store.bytes_since_commit = 0

        store.use_firstbits = (store.config['use_firstbits'] == "true")

        for hex_tx in args.import_tx:
            store.maybe_import_binary_tx(str(hex_tx).decode('hex'))

        store.default_loader = args.default_loader

        if store.in_transaction:
            store.commit()


    def init_conn(store):
        store.conn = store.connect()
        store.cursor = store.conn.cursor()
        store.in_transaction = False

    def connect(store):
        cargs = store.args.connect_args

        if cargs is None:
            conn = store.module.connect()
        else:
            try:
                conn = store._connect(cargs)
            except UnicodeError:
                # Perhaps this driver needs its strings encoded.
                # Python's default is ASCII.  Let's try UTF-8, which
                # should be the default anyway.
                #import locale
                #enc = locale.getlocale()[1] or locale.getdefaultlocale()[1]
                enc = 'UTF-8'
                def to_utf8(obj):
                    if isinstance(obj, dict):
                        for k in obj.keys():
                            obj[k] = to_utf8(obj[k])
                    if isinstance(obj, list):
                        return map(to_utf8, obj)
                    if isinstance(obj, unicode):
                        return obj.encode(enc)
                    return obj
                conn = store._connect(to_utf8(cargs))
                store.log.info("Connection required conversion to UTF-8")

        return conn

    def _connect(store, cargs):
        if isinstance(cargs, dict):
            if ""  in cargs:
                cargs = cargs.copy()
                nkwargs = cargs[""]
                del(cargs[""])
                if isinstance(nkwargs, list):
                    return store.module.connect(*nkwargs, **cargs)
                return store.module.connect(nkwargs, **cargs)
            else:
                return store.module.connect(**cargs)
        if isinstance(cargs, list):
            return store.module.connect(*cargs)
        return store.module.connect(cargs)

    def reconnect(store):
        store.log.info("Reconnecting to database.")
        try:
            store.cursor.close()
        except:
            pass
        try:
            store.conn.close()
        except:
            pass
        store.init_conn()

    def _read_config(store):
        # Read table CONFIGVAR if it exists.
        config = {}
        try:
            store.cursor.execute("""
                SELECT configvar_name, configvar_value
                  FROM configvar""")
            for name, value in store.cursor.fetchall():
                config[name] = '' if value is None else value
            if config:
                return config

        except store.module.DatabaseError:
            try:
                store.rollback()
            except:
                pass

        # Read legacy table CONFIG if it exists.
        try:
            store.cursor.execute("""
                SELECT schema_version, binary_type
                  FROM config
                 WHERE config_id = 1""")
            row = store.cursor.fetchone()
            sv, btype = row
            return { 'schema_version': sv, 'binary_type': btype }
        except:
            try:
                store.rollback()
            except:
                pass

        # Return None to indicate no schema found.
        return None

    # Accommodate SQL quirks.
    def _set_sql_flavour(store):
        def identity(x):
            return x
        transform = identity
        selectall = store._selectall

        if store.module.paramstyle in ('format', 'pyformat'):
            transform = store._qmark_to_format(transform)
        elif store.module.paramstyle == 'named':
            transform = store._qmark_to_named(transform)
        elif store.module.paramstyle != 'qmark':
            store.log.warning("Database parameter style is "
                              "%s, trying qmark", module.paramstyle)
            pass

        # Binary I/O with the database.
        # Hashes are a special type; since the protocol treats them as
        # 256-bit integers and represents them as little endian, we
        # have to reverse them in hex to satisfy human expectations.
        def rev(x):
            return x[::-1]
        def to_hex(x):
            return None if x is None else str(x).encode('hex')
        def from_hex(x):
            return None if x is None else x.decode('hex')
        def to_hex_rev(x):
            return None if x is None else str(x)[::-1].encode('hex')
        def from_hex_rev(x):
            return None if x is None else x.decode('hex')[::-1]

        val = store.config.get('binary_type')

        if val in (None, 'str', "binary"):
            binin       = identity
            binin_hex   = from_hex
            binout      = identity
            binout_hex  = to_hex
            hashin      = rev
            hashin_hex  = from_hex
            hashout     = rev
            hashout_hex = to_hex

            if val == "binary":
                transform = store._sql_binary_as_binary(transform)

        elif val in ("buffer", "bytearray", "pg-bytea"):
            if val == "bytearray":
                def to_btype(x):
                    return None if x is None else bytearray(x)
            else:
                def to_btype(x):
                    return None if x is None else buffer(x)

            def to_str(x):
                return None if x is None else str(x)

            binin       = to_btype
            binin_hex   = lambda x: to_btype(from_hex(x))
            binout      = to_str
            binout_hex  = to_hex
            hashin      = lambda x: to_btype(rev(x))
            hashin_hex  = lambda x: to_btype(from_hex(x))
            hashout     = rev
            hashout_hex = to_hex

            if val == "pg-bytea":
                transform = store._sql_binary_as_bytea(transform)

        elif val == "hex":
            transform = store._sql_binary_as_hex(transform)
            binin       = to_hex
            binin_hex   = identity
            binout      = from_hex
            binout_hex  = identity
            hashin      = to_hex_rev
            hashin_hex  = identity
            hashout     = from_hex_rev
            hashout_hex = identity

        else:
            raise Exception("Unsupported binary-type %s" % (val,))

        val = store.config.get('int_type')

        if val in (None, 'int'):
            intin = identity

        elif val == 'decimal':
            import decimal
            def _intin(x):
                return None if x is None else decimal.Decimal(x)
            intin = _intin

        elif val == 'str':
            def _intin(x):
                return None if x is None else str(x)
            intin = _intin
            # Work around sqlite3's integer overflow.
            transform = store._approximate_txout(transform)

        else:
            raise Exception("Unsupported int-type %s" % (val,))

        val = store.config.get('sequence_type')
        if val in (None, 'update'):
            new_id = lambda key: store._new_id_update(key)
            create_sequence = lambda key: store._create_sequence_update(key)
            drop_sequence = lambda key: store._drop_sequence_update(key)

        elif val == 'mysql':
            new_id = lambda key: store._new_id_mysql(key)
            create_sequence = lambda key: store._create_sequence_mysql(key)
            drop_sequence = lambda key: store._drop_sequence_mysql(key)

        else:
            create_sequence = lambda key: store._create_sequence(key)
            drop_sequence = lambda key: store._drop_sequence(key)

            if val == 'oracle':
                new_id = lambda key: store._new_id_oracle(key)
            elif val == 'nvf':
                new_id = lambda key: store._new_id_nvf(key)
            elif val == 'postgres':
                new_id = lambda key: store._new_id_postgres(key)
            elif val == 'db2':
                new_id = lambda key: store._new_id_db2(key)
                create_sequence = lambda key: store._create_sequence_db2(key)
            else:
                raise Exception("Unsupported sequence-type %s" % (val,))

        # Convert Oracle LOB to str.
        if hasattr(store.module, "LOB") and isinstance(store.module.LOB, type):
            def fix_lob(fn):
                def ret(x):
                    return None if x is None else fn(str(x))
                return ret
            binout = fix_lob(binout)
            binout_hex = fix_lob(binout_hex)

        val = store.config.get('limit_style')
        if val in (None, 'native'):
            pass
        elif val == 'emulated':
            selectall = store.emulate_limit(selectall)

        store.sql_transform = transform
        store.selectall = selectall
        store._sql_cache = {}

        store.binin       = binin
        store.binin_hex   = binin_hex
        store.binout      = binout
        store.binout_hex  = binout_hex
        store.hashin      = hashin
        store.hashin_hex  = hashin_hex
        store.hashout     = hashout
        store.hashout_hex = hashout_hex

        # Might reimplement these someday...
        def binout_int(x):
            if x is None:
                return None
            return int(binout_hex(x), 16)
        def binin_int(x, bits):
            if x is None:
                return None
            return binin_hex(("%%0%dx" % (bits / 4)) % x)
        store.binout_int  = binout_int
        store.binin_int   = binin_int

        store.intin       = intin
        store.new_id      = new_id
        store.create_sequence = create_sequence
        store.drop_sequence = drop_sequence

    def _execute(store, stmt, params):
        try:
            store.cursor.execute(stmt, params)
        except (store.module.OperationalError, store.module.InternalError,
                store.module.ProgrammingError) as e:
            if store.in_transaction or not store.auto_reconnect:
                raise

            store.log.warning("Replacing possible stale cursor: %s", e)

            try:
                store.reconnect()
            except:
                store.log.exception("Failed to reconnect")
                raise e

            store.cursor.execute(stmt, params)

    def sql(store, stmt, params=()):
        cached = store._sql_cache.get(stmt)
        if cached is None:
            cached = store.sql_transform(stmt)
            store._sql_cache[stmt] = cached
        store.sqllog.info("EXEC: %s %s", cached, params)
        try:
            store._execute(cached, params)
        except Exception, e:
            store.sqllog.info("EXCEPTION: %s", e)
            raise
        finally:
            store.in_transaction = True

    def ddl(store, stmt):
        if stmt.lstrip().startswith("CREATE TABLE "):
            stmt += store.config['create_table_epilogue']
        stmt = store._sql_fallback_to_lob(store.sql_transform(stmt))
        store.sqllog.info("DDL: %s", stmt)
        try:
            store.cursor.execute(stmt)
        except Exception, e:
            store.sqllog.info("EXCEPTION: %s", e)
            raise
        if store.config['ddl_implicit_commit'] == 'false':
            store.commit()
        else:
            store.in_transaction = False

    # Convert standard placeholders to Python "format" style.
    def _qmark_to_format(store, fn):
        def ret(stmt):
            # XXX Simplified by assuming no literals contain "?".
            return fn(stmt.replace('%', '%%').replace("?", "%s"))
        return ret

    # Convert standard placeholders to Python "named" style.
    def _qmark_to_named(store, fn):
        def ret(stmt):
            i = [0]
            def newname(m):
                i[0] += 1
                return ":p%d" % (i[0],)
            # XXX Simplified by assuming no literals contain "?".
            return fn(re.sub("\\?", newname, stmt))
        return ret

    # Convert the standard BIT type to a hex string for databases
    # and drivers that don't support BIT.
    def _sql_binary_as_hex(store, fn):
        patt = re.compile("BIT((?: VARYING)?)\\(([0-9]+)\\)")
        def fixup(match):
            # XXX This assumes no string literals match.
            return (("VARCHAR(" if match.group(1) else "CHAR(") +
                    str(int(match.group(2)) / 4) + ")")
        def ret(stmt):
            # XXX This assumes no string literals match.
            return fn(patt.sub(fixup, stmt).replace("X'", "'"))
        return ret

    # Convert the standard BIT type to a binary string for databases
    # and drivers that don't support BIT.
    def _sql_binary_as_binary(store, fn):
        patt = re.compile("BIT((?: VARYING)?)\\(([0-9]+)\\)")
        def fixup(match):
            # XXX This assumes no string literals match.
            return (("VARBINARY(" if match.group(1) else "BINARY(") +
                    str(int(match.group(2)) / 8) + ")")
        def ret(stmt):
            # XXX This assumes no string literals match.
            return fn(patt.sub(fixup, stmt))
        return ret

    # Convert the standard BIT type to the PostgreSQL BYTEA type.
    def _sql_binary_as_bytea(store, fn):
        type_patt = re.compile("BIT((?: VARYING)?)\\(([0-9]+)\\)")
        lit_patt = re.compile("X'((?:[0-9a-fA-F][0-9a-fA-F])*)'")
        def fix_type(match):
            # XXX This assumes no string literals match.
            return "BYTEA"
        def fix_lit(match):
            ret = "'"
            for i in match.group(1).decode('hex'):
                ret += r'\\%03o' % ord(i)
            ret += "'::bytea"
            return ret
        def ret(stmt):
            stmt = type_patt.sub(fix_type, stmt)
            stmt = lit_patt.sub(fix_lit, stmt)
            return fn(stmt)
        return ret

    # Converts VARCHAR types that are too long to CLOB or similar.
    def _sql_fallback_to_lob(store, stmt):
        try:
            max_varchar = int(store.config['max_varchar'])
            clob_type = store.config['clob_type']
        except:
            return stmt

        patt = re.compile("VARCHAR\\(([0-9]+)\\)")

        def fixup(match):
            # XXX This assumes no string literals match.
            width = int(match.group(1))
            if width > max_varchar and clob_type != NO_CLOB:
                return clob_type
            return "VARCHAR(%d)" % (width,)

        return patt.sub(fixup, stmt)

    def _approximate_txout(store, fn):
        def ret(stmt):
            return fn(re.sub(
                    r'\btxout_value txout_approx_value\b',
                    'CAST(txout_value AS DOUBLE PRECISION) txout_approx_value',
                    stmt))
        return ret

    def emulate_limit(store, selectall):
        limit_re = re.compile(r"(.*)\bLIMIT\s+(\?|\d+)\s*\Z", re.DOTALL)
        def ret(stmt, params=()):
            match = limit_re.match(stmt)
            if match:
                if match.group(2) == '?':
                    n = params[-1]
                    params = params[:-1]
                else:
                    n = int(match.group(2))
                store.sql(match.group(1), params)
                return [ store.cursor.fetchone() for i in xrange(n) ]
            return selectall(stmt, params)
        return ret

    def selectrow(store, stmt, params=()):
        store.sql(stmt, params)
        ret = store.cursor.fetchone()
        store.sqllog.debug("FETCH: %s", ret)
        return ret

    def _selectall(store, stmt, params=()):
        store.sql(stmt, params)
        ret = store.cursor.fetchall()
        store.sqllog.debug("FETCHALL: %s", ret)
        return ret

    def _init_datadirs(store):
        if store.args.datadir == []:
            store.datadirs = []
            return

        datadirs = {}
        for row in store.selectall("""
            SELECT datadir_id, dirname, blkfile_number, blkfile_offset,
                   chain_id, datadir_loader
              FROM datadir"""):
            id, dir, num, offs, chain_id, loader = row
            datadirs[dir] = {
                "id": id,
                "dirname": dir,
                "blkfile_number": int(num),
                "blkfile_offset": int(offs),
                "chain_id": None if chain_id is None else int(chain_id),
                "loader": loader}

        # By default, scan every dir we know.  This doesn't happen in
        # practise, because abe.py sets ~/.bitcoin as default datadir.
        if store.args.datadir is None:
            store.datadirs = datadirs.values()
            return

        store.datadirs = []
        for dircfg in store.args.datadir:
            if isinstance(dircfg, dict):
                dirname = dircfg.get('dirname')
                if dirname is None:
                    raise ValueError(
                        'Missing dirname in datadir configuration: '
                        + str(dircfg))
                if dirname in datadirs:
                    store.datadirs.append(datadirs[dirname])
                    continue

                chain_id = dircfg.get('chain_id')
                if chain_id is None:
                    chain_name = dircfg.get('chain')
                    row = store.selectrow(
                        "SELECT chain_id FROM chain WHERE chain_name = ?",
                        (chain_name,))

                    if row is not None:
                        chain_id = row[0]

                    elif chain_name is not None:
                        chain_id = store.new_id('chain')

                        code3 = dircfg.get('code3')
                        if code3 is None:
                            code3 = '000' if chain_id > 999 else "%03d" % (
                                chain_id,)

                        addr_vers = dircfg.get('address_version')
                        if addr_vers is None:
                            addr_vers = "\0"
                        elif isinstance(addr_vers, unicode):
                            addr_vers = addr_vers.encode('latin_1')
                        store.sql("""
                            INSERT INTO chain (
                                chain_id, chain_name, chain_code3,
                                chain_address_version
                            ) VALUES (?, ?, ?, ?)""",
                                  (chain_id, chain_name, code3,
                                   store.binin(addr_vers)))
                        store.commit()
                        store.log.warning("Assigned chain_id %d to %s",
                                          chain_id, chain_name)

                loader = dircfg.get('loader')

            elif dircfg in datadirs:
                store.datadirs.append(datadirs[dircfg])
                continue
            else:
                # Not a dict.  A string naming a directory holding
                # standard chains.
                dirname = dircfg
                chain_id = None
                loader = None

            store.datadirs.append({
                "id": store.new_id("datadir"),
                "dirname": dirname,
                "blkfile_number": 1,
                "blkfile_offset": 0,
                "chain_id": chain_id,
                "loader": loader,
                })

    def _find_no_bit8_chain_ids(store, no_bit8_chains):
        chains = no_bit8_chains
        if chains is None:
            chains = ["Bitcoin", "Testnet"]
        if isinstance(chains, str):
            chains = [chains]
        ids = set()
        for name in chains:
            rows = store.selectall(
                "SELECT chain_id FROM chain WHERE chain_name = ?", (name,))
            if not rows:
                if no_bit8_chains is not None:
                    # Make them fix their config.
                    raise ValueError(
                        "Unknown chain name in ignore-bit8-chains: " + name)
                continue
            for row in rows:
                ids.add(int(row[0]))
        return ids

    def _new_id_update(store, key):
        """
        Allocate a synthetic identifier by updating a table.
        """
        while True:
            row = store.selectrow(
                "SELECT nextid FROM abe_sequences WHERE sequence_key = ?",
                (key,))
            if row is None:
                raise Exception("Sequence %s does not exist" % (key,))

            ret = row[0]
            store.sql("UPDATE abe_sequences SET nextid = nextid + 1"
                      " WHERE sequence_key = ? AND nextid = ?",
                      (key, ret))
            if store.cursor.rowcount == 1:
                return ret
            store.log.info('Contention on abe_sequences %s:%d', key, ret)

    def _get_sequence_initial_value(store, key):
        (ret,) = store.selectrow("SELECT MAX(" + key + "_id) FROM " + key)
        ret = 1 if ret is None else ret + 1
        return ret

    def _create_sequence_update(store, key):
        store.commit()
        ret = store._get_sequence_initial_value(key)
        try:
            store.sql("INSERT INTO abe_sequences (sequence_key, nextid)"
                      " VALUES (?, ?)", (key, ret))
        except store.module.DatabaseError, e:
            store.rollback()
            try:
                store.ddl(store._ddl['abe_sequences'])
            except:
                store.rollback()
                raise e
            store.sql("INSERT INTO abe_sequences (sequence_key, nextid)"
                      " VALUES (?, ?)", (key, ret))

    def _drop_sequence_update(store, key):
        store.commit()
        store.sql("DELETE FROM abe_sequences WHERE sequence_key = ?", (key,))
        store.commit()

    def _new_id_oracle(store, key):
        (ret,) = store.selectrow("SELECT " + key + "_seq.NEXTVAL FROM DUAL")
        return ret

    def _create_sequence(store, key):
        store.ddl("CREATE SEQUENCE %s_seq START WITH %d"
                  % (key, store._get_sequence_initial_value(key)))

    def _drop_sequence(store, key):
        store.ddl("DROP SEQUENCE %s_seq" % (key,))

    def _new_id_nvf(store, key):
        (ret,) = store.selectrow("SELECT NEXT VALUE FOR " + key + "_seq")
        return ret

    def _new_id_postgres(store, key):
        (ret,) = store.selectrow("SELECT NEXTVAL('" + key + "_seq')")
        return ret

    def _create_sequence_db2(store, key):
        store.commit()
        try:
            rows = store.selectall("SELECT 1 FROM abe_dual")
            if len(rows) != 1:
                store.sql("INSERT INTO abe_dual(x) VALUES ('X')")
        except store.module.DatabaseError, e:
            store.rollback()
            store.drop_table_if_exists('abe_dual')
            store.ddl("CREATE TABLE abe_dual (x CHAR(1))")
            store.sql("INSERT INTO abe_dual(x) VALUES ('X')")
            store.log.info("Created silly table abe_dual")
        store._create_sequence(key)

    def _new_id_db2(store, key):
        (ret,) = store.selectrow("SELECT NEXTVAL FOR " + key + "_seq"
                                 " FROM abe_dual")
        return ret

    def _create_sequence_mysql(store, key):
        store.ddl("CREATE TABLE %s_seq (id BIGINT AUTO_INCREMENT PRIMARY KEY)"
                  " AUTO_INCREMENT=%d"
                  % (key, store._get_sequence_initial_value(key)))

    def _drop_sequence_mysql(store, key):
        store.ddl("DROP TABLE %s_seq" % (key,))

    def _new_id_mysql(store, key):
        store.sql("INSERT INTO " + key + "_seq () VALUES ()")
        (ret,) = store.selectrow("SELECT LAST_INSERT_ID()")
        if ret % 1000 == 0:
            store.sql("DELETE FROM " + key + "_seq WHERE id < ?", (ret,))
        return ret

    def commit(store):
        store.sqllog.info("COMMIT")
        store.conn.commit()
        store.in_transaction = False

    def rollback(store):
        store.sqllog.info("ROLLBACK")
        try:
            store.conn.rollback()
            store.in_transaction = False
        except store.module.OperationalError, e:
            store.log.warning("Reconnecting after rollback error: %s", e)
            store.reconnect()

    def close(store):
        store.sqllog.info("CLOSE")
        store.conn.close()

    def get_ddl(store, key):
        return store._ddl[key]

    def refresh_ddl(store):
        store._ddl = {
            "chain_summary":
# XXX I could do a lot with MATERIALIZED views.
"""CREATE VIEW chain_summary AS SELECT
    cc.chain_id,
    cc.in_longest,
    b.block_id,
    b.block_hash,
    b.block_version,
    b.block_hashMerkleRoot,
    b.block_nTime,
    b.block_nBits,
    b.block_nNonce,
    cc.block_height,
    b.prev_block_id,
    prev.block_hash prev_block_hash,
    b.block_chain_work,
    b.block_num_tx,
    b.block_value_in,
    b.block_value_out,
    b.block_total_satoshis,
    b.block_total_seconds,
    b.block_satoshi_seconds,
    b.block_total_ss,
    b.block_ss_destroyed
FROM chain_candidate cc
JOIN block b ON (cc.block_id = b.block_id)
LEFT JOIN block prev ON (b.prev_block_id = prev.block_id)""",

            "txout_detail":
"""CREATE VIEW txout_detail AS SELECT
    cc.chain_id,
    cc.in_longest,
    cc.block_id,
    b.block_hash,
    b.block_height,
    block_tx.tx_pos,
    tx.tx_id,
    tx.tx_hash,
    tx.tx_lockTime,
    tx.tx_version,
    tx.tx_size,
    txout.txout_id,
    txout.txout_pos,
    txout.txout_value,
    txout.txout_scriptPubKey,
    pubkey.pubkey_id,
    pubkey.pubkey_hash,
    pubkey.pubkey
  FROM chain_candidate cc
  JOIN block b ON (cc.block_id = b.block_id)
  JOIN block_tx ON (b.block_id = block_tx.block_id)
  JOIN tx    ON (tx.tx_id = block_tx.tx_id)
  JOIN txout ON (tx.tx_id = txout.tx_id)
  LEFT JOIN pubkey ON (txout.pubkey_id = pubkey.pubkey_id)""",

            "txin_detail":
"""CREATE VIEW txin_detail AS SELECT
    cc.chain_id,
    cc.in_longest,
    cc.block_id,
    b.block_hash,
    b.block_height,
    block_tx.tx_pos,
    tx.tx_id,
    tx.tx_hash,
    tx.tx_lockTime,
    tx.tx_version,
    tx.tx_size,
    txin.txin_id,
    txin.txin_pos,
    txin.txout_id prevout_id""" + (""",
    txin.txin_scriptSig,
    txin.txin_sequence""" if store.keep_scriptsig else """,
    NULL txin_scriptSig,
    NULL txin_sequence""") + """,
    prevout.txout_value txin_value,
    pubkey.pubkey_id,
    pubkey.pubkey_hash,
    pubkey.pubkey
  FROM chain_candidate cc
  JOIN block b ON (cc.block_id = b.block_id)
  JOIN block_tx ON (b.block_id = block_tx.block_id)
  JOIN tx    ON (tx.tx_id = block_tx.tx_id)
  JOIN txin  ON (tx.tx_id = txin.tx_id)
  LEFT JOIN txout prevout ON (txin.txout_id = prevout.txout_id)
  LEFT JOIN pubkey
      ON (prevout.pubkey_id = pubkey.pubkey_id)""",

            "txout_approx":
# View of txout for drivers like sqlite3 that can not handle large
# integer arithmetic.  For them, we transform the definition of
# txout_approx_value to DOUBLE PRECISION (approximate) by a CAST.
"""CREATE VIEW txout_approx AS SELECT
    txout_id,
    tx_id,
    txout_value txout_approx_value
  FROM txout""",

            "configvar":
# ABE accounting.  This table is read without knowledge of the
# database's SQL quirks, so it must use only the most widely supported
# features.
"""CREATE TABLE configvar (
    configvar_name  VARCHAR(100) NOT NULL PRIMARY KEY,
    configvar_value VARCHAR(255)
)""",

            "abe_sequences":
"""CREATE TABLE abe_sequences (
    sequence_key VARCHAR(100) NOT NULL PRIMARY KEY,
    nextid NUMERIC(30)
)""",
            }

    def initialize(store):
        """
        Create the database schema.
        """
        store.configure()

        for stmt in (

store._ddl['configvar'],

"""CREATE TABLE datadir (
    datadir_id  NUMERIC(10) NOT NULL PRIMARY KEY,
    dirname     VARCHAR(2000) NOT NULL,
    blkfile_number NUMERIC(8) NULL,
    blkfile_offset NUMERIC(20) NULL,
    chain_id    NUMERIC(10) NULL,
    datadir_loader VARCHAR(100) NULL
)""",

# MAGIC lists the magic numbers seen in messages and block files, known
# in the original Bitcoin source as `pchMessageStart'.
"""CREATE TABLE magic (
    magic_id    NUMERIC(10) NOT NULL PRIMARY KEY,
    magic       BIT(32)     UNIQUE NOT NULL,
    magic_name  VARCHAR(100) UNIQUE NOT NULL
)""",

# POLICY identifies a block acceptance policy.  Not currently used,
# but required by CHAIN.
"""CREATE TABLE policy (
    policy_id   NUMERIC(10) NOT NULL PRIMARY KEY,
    policy_name VARCHAR(100) UNIQUE NOT NULL
)""",

# A block of the type used by Bitcoin.
"""CREATE TABLE block (
    block_id      NUMERIC(14) NOT NULL PRIMARY KEY,
    block_hash    BIT(256)    UNIQUE NOT NULL,
    block_version NUMERIC(10),
    block_hashMerkleRoot BIT(256),
    block_nTime   NUMERIC(20),
    block_nBits   NUMERIC(10),
    block_nNonce  NUMERIC(10),
    block_height  NUMERIC(14) NULL,
    prev_block_id NUMERIC(14) NULL,
    search_block_id NUMERIC(14) NULL,
    block_chain_work BIT(""" + str(WORK_BITS) + """),
    block_value_in NUMERIC(30) NULL,
    block_value_out NUMERIC(30),
    block_total_satoshis NUMERIC(26) NULL,
    block_total_seconds NUMERIC(20) NULL,
    block_satoshi_seconds NUMERIC(28) NULL,
    block_total_ss NUMERIC(28) NULL,
    block_num_tx  NUMERIC(10) NOT NULL,
    block_ss_destroyed NUMERIC(28) NULL,
    FOREIGN KEY (prev_block_id)
        REFERENCES block (block_id),
    FOREIGN KEY (search_block_id)
        REFERENCES block (block_id)
)""",

# CHAIN comprises a magic number, a policy, and (indirectly via
# CHAIN_LAST_BLOCK_ID and the referenced block's ancestors) a genesis
# block, possibly null.  A chain may have a currency code.
"""CREATE TABLE chain (
    chain_id    NUMERIC(10) NOT NULL PRIMARY KEY,
    magic_id    NUMERIC(10) NULL,
    policy_id   NUMERIC(10) NULL,
    chain_name  VARCHAR(100) UNIQUE NOT NULL,
    chain_code3 CHAR(3)     NULL,
    chain_address_version BIT VARYING(800) NOT NULL,
    chain_last_block_id NUMERIC(14) NULL,
    FOREIGN KEY (magic_id)  REFERENCES magic (magic_id),
    FOREIGN KEY (policy_id) REFERENCES policy (policy_id),
    FOREIGN KEY (chain_last_block_id)
        REFERENCES block (block_id)
)""",

# CHAIN_CANDIDATE lists blocks that are, or might become, part of the
# given chain.  IN_LONGEST is 1 when the block is in the chain, else 0.
# IN_LONGEST denormalizes information stored canonically in
# CHAIN.CHAIN_LAST_BLOCK_ID and BLOCK.PREV_BLOCK_ID.
"""CREATE TABLE chain_candidate (
    chain_id      NUMERIC(10) NOT NULL,
    block_id      NUMERIC(14) NOT NULL,
    in_longest    NUMERIC(1),
    block_height  NUMERIC(14),
    PRIMARY KEY (chain_id, block_id),
    FOREIGN KEY (block_id) REFERENCES block (block_id)
)""",
"""CREATE INDEX x_cc_block ON chain_candidate (block_id)""",
"""CREATE INDEX x_cc_chain_block_height
    ON chain_candidate (chain_id, block_height)""",
"""CREATE INDEX x_cc_block_height ON chain_candidate (block_height)""",

# An orphan block must remember its hashPrev.
"""CREATE TABLE orphan_block (
    block_id      NUMERIC(14) NOT NULL PRIMARY KEY,
    block_hashPrev BIT(256)   NOT NULL,
    FOREIGN KEY (block_id) REFERENCES block (block_id)
)""",
"""CREATE INDEX x_orphan_block_hashPrev ON orphan_block (block_hashPrev)""",

# Denormalize the relationship inverse to BLOCK.PREV_BLOCK_ID.
"""CREATE TABLE block_next (
    block_id      NUMERIC(14) NOT NULL,
    next_block_id NUMERIC(14) NOT NULL,
    PRIMARY KEY (block_id, next_block_id),
    FOREIGN KEY (block_id) REFERENCES block (block_id),
    FOREIGN KEY (next_block_id) REFERENCES block (block_id)
)""",

# A transaction of the type used by Bitcoin.
"""CREATE TABLE tx (
    tx_id         NUMERIC(26) NOT NULL PRIMARY KEY,
    tx_hash       BIT(256)    UNIQUE NOT NULL,
    tx_version    NUMERIC(10),
    tx_lockTime   NUMERIC(10),
    tx_size       NUMERIC(10)
)""",

# Presence of transactions in blocks is many-to-many.
"""CREATE TABLE block_tx (
    block_id      NUMERIC(14) NOT NULL,
    tx_id         NUMERIC(26) NOT NULL,
    tx_pos        NUMERIC(10) NOT NULL,
    PRIMARY KEY (block_id, tx_id),
    UNIQUE (block_id, tx_pos),
    FOREIGN KEY (block_id)
        REFERENCES block (block_id),
    FOREIGN KEY (tx_id)
        REFERENCES tx (tx_id)
)""",
"""CREATE INDEX x_block_tx_tx ON block_tx (tx_id)""",

# A public key for sending bitcoins.  PUBKEY_HASH is derivable from a
# Bitcoin or Testnet address.
"""CREATE TABLE pubkey (
    pubkey_id     NUMERIC(26) NOT NULL PRIMARY KEY,
    pubkey_hash   BIT(160)    UNIQUE NOT NULL,
    pubkey        BIT(520)    NULL
)""",

# A transaction out-point.
"""CREATE TABLE txout (
    txout_id      NUMERIC(26) NOT NULL PRIMARY KEY,
    tx_id         NUMERIC(26) NOT NULL,
    txout_pos     NUMERIC(10) NOT NULL,
    txout_value   NUMERIC(30) NOT NULL,
    txout_scriptPubKey BIT VARYING(""" + str(8 * MAX_SCRIPT) + """),
    pubkey_id     NUMERIC(26),
    UNIQUE (tx_id, txout_pos),
    FOREIGN KEY (pubkey_id)
        REFERENCES pubkey (pubkey_id)
)""",
"""CREATE INDEX x_txout_pubkey ON txout (pubkey_id)""",

# A transaction in-point.
"""CREATE TABLE txin (
    txin_id       NUMERIC(26) NOT NULL PRIMARY KEY,
    tx_id         NUMERIC(26) NOT NULL,
    txin_pos      NUMERIC(10) NOT NULL,
    txout_id      NUMERIC(26)""" + (""",
    txin_scriptSig BIT VARYING(""" + str(8 * MAX_SCRIPT) + """),
    txin_sequence NUMERIC(10)""" if store.keep_scriptsig else "") + """,
    UNIQUE (tx_id, txin_pos),
    FOREIGN KEY (tx_id)
        REFERENCES tx (tx_id)
)""",
"""CREATE INDEX x_txin_txout ON txin (txout_id)""",

# While TXIN.TXOUT_ID can not be found, we must remember TXOUT_POS,
# a.k.a. PREVOUT_N.
"""CREATE TABLE unlinked_txin (
    txin_id       NUMERIC(26) NOT NULL PRIMARY KEY,
    txout_tx_hash BIT(256)    NOT NULL,
    txout_pos     NUMERIC(10) NOT NULL,
    FOREIGN KEY (txin_id) REFERENCES txin (txin_id)
)""",
"""CREATE INDEX x_unlinked_txin_outpoint
    ON unlinked_txin (txout_tx_hash, txout_pos)""",

"""CREATE TABLE block_txin (
    block_id      NUMERIC(14) NOT NULL,
    txin_id       NUMERIC(26) NOT NULL,
    out_block_id  NUMERIC(14) NOT NULL,
    PRIMARY KEY (block_id, txin_id),
    FOREIGN KEY (block_id) REFERENCES block (block_id),
    FOREIGN KEY (txin_id) REFERENCES txin (txin_id),
    FOREIGN KEY (out_block_id) REFERENCES block (block_id)
)""",

store._ddl['chain_summary'],
store._ddl['txout_detail'],
store._ddl['txin_detail'],
store._ddl['txout_approx'],

"""CREATE TABLE abe_lock (
    lock_id       NUMERIC(10) NOT NULL PRIMARY KEY,
    pid           VARCHAR(255) NULL
)""",
):
            try:
                store.ddl(stmt)
            except:
                store.log.error("Failed: %s", stmt)
                raise

        for key in ['magic', 'policy', 'chain', 'datadir',
                    'tx', 'txout', 'pubkey', 'txin', 'block']:
            store.create_sequence(key)

        store.sql("INSERT INTO abe_lock (lock_id) VALUES (1)")

        # Insert some well-known chain metadata.
        for conf in CHAIN_CONFIG:
            for thing in "magic", "policy", "chain":
                if thing + "_id" not in conf:
                    conf[thing + "_id"] = store.new_id(thing)
            if "network" not in conf:
                conf["network"] = conf["chain"]
            for thing in "magic", "policy":
                if thing + "_name" not in conf:
                    conf[thing + "_name"] = conf["network"] + " " + thing

            store.log.info(conf["magic_id"])
            store.log.info( conf["magic_name"])
                    
            store.sql("""
                INSERT INTO magic (magic_id, magic, magic_name)
                VALUES (?, ?, ?)""",
                      (conf["magic_id"], store.binin(conf["magic"]),
                       conf["magic_name"]))
            store.sql("""
                INSERT INTO policy (policy_id, policy_name)
                VALUES (?, ?)""",
                      (conf["policy_id"], conf["policy_name"]))
            store.sql("""
                INSERT INTO chain (
                    chain_id, magic_id, policy_id, chain_name, chain_code3,
                    chain_address_version
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                      (conf["chain_id"], conf["magic_id"], conf["policy_id"],
                       conf["chain"], conf["code3"],
                       store.binin(conf["address_version"])))

        store.sql("""
            INSERT INTO pubkey (pubkey_id, pubkey_hash) VALUES (?, ?)""",
                  (NULL_PUBKEY_ID, store.binin(NULL_PUBKEY_HASH)))

        if store.args.use_firstbits:
            store.config['use_firstbits'] = "true"
            store.ddl(
                """CREATE TABLE abe_firstbits (
                    pubkey_id       NUMERIC(26) NOT NULL,
                    block_id        NUMERIC(14) NOT NULL,
                    address_version BIT VARYING(80) NOT NULL,
                    firstbits       VARCHAR(50) NOT NULL,
                    PRIMARY KEY (address_version, pubkey_id, block_id),
                    FOREIGN KEY (pubkey_id) REFERENCES pubkey (pubkey_id),
                    FOREIGN KEY (block_id) REFERENCES block (block_id)
                )""")
            store.ddl(
                """CREATE INDEX x_abe_firstbits
                    ON abe_firstbits (address_version, firstbits)""")
        else:
            store.config['use_firstbits'] = "false"

        store.config['keep_scriptsig'] = \
            "true" if store.args.keep_scriptsig else "false"

        store.save_config()
        store.commit()

    def get_lock(store):
        if store.version_below('Abe26'):
            return None
        conn = store.connect()
        cur = conn.cursor()
        cur.execute("UPDATE abe_lock SET pid = %d WHERE lock_id = 1"
                    % (os.getpid(),))
        if cur.rowcount != 1:
            raise Exception("unexpected rowcount")
        cur.close()

        # Check whether database supports concurrent updates.  Where it
        # doesn't (SQLite) we get exclusive access automatically.
        try:
            import random
            letters = "".join([chr(random.randint(65, 90)) for x in xrange(10)])
            store.sql("""
                INSERT INTO configvar (configvar_name, configvar_value)
                VALUES (?, ?)""",
                      ("upgrade-lock-" + letters, 'x'))
        except:
            store.release_lock(conn)
            conn = None

        store.rollback()

        # XXX Should reread config.

        return conn

    def release_lock(store, conn):
        if conn:
            conn.rollback()
            conn.close()

    def version_below(store, vers):
        sv = store.config['schema_version'].replace('Abe', '')
        vers = vers.replace('Abe', '')
        return float(sv) < float(vers)

    def configure(store):
        store.config = {}

        store.configure_ddl_implicit_commit()
        store.configure_create_table_epilogue()
        store.configure_max_varchar()
        store.configure_clob_type()
        store.configure_binary_type()
        store.configure_int_type()
        store.configure_sequence_type()
        store.configure_limit_style()

    def configure_binary_type(store):
        for val in (
            ['str', 'bytearray', 'buffer', 'hex', 'pg-bytea', 'binary']
            if store.args.binary_type is None else
            [ store.args.binary_type ]):

            store.config['binary_type'] = val
            store._set_sql_flavour()
            if store._test_binary_type():
                store.log.info("binary_type=%s", val)
                return
        raise Exception(
            "No known binary data representation works"
            if store.args.binary_type is None else
            "Binary type " + store.args.binary_type + " fails test")

    def configure_int_type(store):
        for val in (
            ['int', 'decimal', 'str']
            if store.args.int_type is None else
            [ store.args.int_type ]):
            store.config['int_type'] = val
            store._set_sql_flavour()
            if store._test_int_type():
                store.log.info("int_type=%s", val)
                return
        raise Exception("No known large integer representation works")

    def configure_sequence_type(store):
        for val in ['oracle', 'postgres', 'nvf', 'db2', 'mysql', 'update']:
            store.config['sequence_type'] = val
            store._set_sql_flavour()
            if store._test_sequence_type():
                store.log.info("sequence_type=%s", val)
                return
        raise Exception("No known sequence type works")

    def _drop_if_exists(store, otype, name):
        try:
            store.sql("DROP " + otype + " " + name)
            store.commit()
        except store.module.DatabaseError:
            store.rollback()

    def drop_table_if_exists(store, obj):
        store._drop_if_exists("TABLE", obj)
    def drop_view_if_exists(store, obj):
        store._drop_if_exists("VIEW", obj)

    def drop_sequence_if_exists(store, key):
        try:
            store.drop_sequence(key)
        except store.module.DatabaseError:
            store.rollback()

    def drop_column_if_exists(store, table, column):
        try:
            store.ddl("ALTER TABLE " + table + " DROP COLUMN " + column)
        except store.module.DatabaseError:
            store.rollback()

    def configure_ddl_implicit_commit(store):
        if 'create_table_epilogue' not in store.config:
            store.config['create_table_epilogue'] = ''
        for val in ['true', 'false']:
            store.config['ddl_implicit_commit'] = val
            store._set_sql_flavour()
            if store._test_ddl():
                store.log.info("ddl_implicit_commit=%s", val)
                return
        raise Exception("Can not test for DDL implicit commit.")

    def _test_ddl(store):
        """Test whether DDL performs implicit commit."""

        store.drop_table_if_exists("abe_test_1")
        store.ddl(
            "CREATE TABLE abe_test_1 ("
            " abe_test_1_id NUMERIC(12) NOT NULL PRIMARY KEY,"
            " foo VARCHAR(10))")
        store.rollback()

        try:
            store.selectall("SELECT MAX(abe_test_1_id) FROM abe_test_1")
            return True
        except store.module.DatabaseError, e:
            store.rollback()
            return False
        except Exception:
            store.rollback()
            return False
        finally:
            store.drop_table_if_exists("abe_test_1")

    def configure_create_table_epilogue(store):
        for val in ['', ' ENGINE=InnoDB']:
            store.config['create_table_epilogue'] = val
            store._set_sql_flavour()
            if store._test_transaction():
                store.log.info("create_table_epilogue='%s'", val)
                return
        raise Exception("Can not create a transactional table.")

    def _test_transaction(store):
        """Test whether CREATE TABLE needs ENGINE=InnoDB for rollback."""
        store.drop_table_if_exists("abe_test_1")
        try:
            store.ddl(
                "CREATE TABLE abe_test_1 (a NUMERIC(12))")
            store.sql("INSERT INTO abe_test_1 (a) VALUES (4)")
            store.commit()
            store.sql("INSERT INTO abe_test_1 (a) VALUES (5)")
            store.rollback()
            data = [int(row[0]) for row in store.selectall(
                    "SELECT a FROM abe_test_1")]
            return data == [4]
        except store.module.DatabaseError, e:
            store.rollback()
            return False
        except Exception, e:
            store.rollback()
            return False
        finally:
            store.drop_table_if_exists("abe_test_1")

    def configure_max_varchar(store):
        """Find the maximum VARCHAR width, up to 0xffffffff"""
        lo = 0
        hi = 1 << 32
        mid = hi - 1
        store.config['max_varchar'] = str(mid)
        store.drop_table_if_exists("abe_test_1")
        while True:
            store.drop_table_if_exists("abe_test_1")
            try:
                store.ddl("""CREATE TABLE abe_test_1
                           (a VARCHAR(%d), b VARCHAR(%d))""" % (mid, mid))
                store.sql("INSERT INTO abe_test_1 (a, b) VALUES ('x', 'y')")
                row = store.selectrow("SELECT a, b FROM abe_test_1")
                if [x for x in row] == ['x', 'y']:
                    lo = mid
                else:
                    hi = mid
            except store.module.DatabaseError, e:
                store.rollback()
                hi = mid
            except Exception, e:
                store.rollback()
                hi = mid
            if lo + 1 == hi:
                store.config['max_varchar'] = str(lo)
                store.log.info("max_varchar=%s", store.config['max_varchar'])
                break
            mid = (lo + hi) / 2
        store.drop_table_if_exists("abe_test_1")

    def configure_clob_type(store):
        """Find the name of the CLOB type, if any."""
        long_str = 'x' * 10000
        store.drop_table_if_exists("abe_test_1")
        for val in ['CLOB', 'LONGTEXT', 'TEXT', 'LONG']:
            try:
                store.ddl("CREATE TABLE abe_test_1 (a %s)" % (val,))
                store.sql("INSERT INTO abe_test_1 (a) VALUES (?)",
                          (store.binin(long_str),))
                out = store.selectrow("SELECT a FROM abe_test_1")[0]
                if store.binout(out) == long_str:
                    store.config['clob_type'] = val
                    store.log.info("clob_type=%s", val)
                    return
                else:
                    store.log.debug("out=%s", repr(out))
            except store.module.DatabaseError, e:
                store.rollback()
            except Exception, e:
                try:
                    store.rollback()
                except:
                    # Fetching a CLOB really messes up Easysoft ODBC Oracle.
                    store.reconnect()
            finally:
                store.drop_table_if_exists("abe_test_1")
        store.log.info("No native type found for CLOB.")
        store.config['clob_type'] = NO_CLOB

    def _test_binary_type(store):
        store.drop_table_if_exists("abe_test_1")
        try:
            store.ddl(
                "CREATE TABLE abe_test_1 (test_id NUMERIC(2) NOT NULL PRIMARY KEY,"
                " test_bit BIT(256), test_varbit BIT VARYING(" + str(8 * MAX_SCRIPT) + "))")
            val = str(''.join(map(chr, range(0, 256, 8))))
            store.sql("INSERT INTO abe_test_1 (test_id, test_bit, test_varbit)"
                      " VALUES (?, ?, ?)",
                      (1, store.hashin(val), store.binin(val)))
            (bit, vbit) = store.selectrow(
                "SELECT test_bit, test_varbit FROM abe_test_1")
            if store.hashout(bit) != val:
                return False
            if store.binout(vbit) != val:
                return False
            return True
        except store.module.DatabaseError, e:
            store.rollback()
            return False
        except Exception, e:
            store.rollback()
            return False
        finally:
            store.drop_table_if_exists("abe_test_1")

    def _test_int_type(store):
        store.drop_view_if_exists("abe_test_v1")
        store.drop_table_if_exists("abe_test_1")
        try:
            store.ddl(
                """CREATE TABLE abe_test_1 (test_id NUMERIC(2) NOT NULL PRIMARY KEY,
                 txout_value NUMERIC(30), i2 NUMERIC(20))""")
            store.ddl(
                """CREATE VIEW abe_test_v1 AS SELECT test_id,
                 txout_value txout_approx_value, txout_value i1, i2
                 FROM abe_test_1""")
            v1 = 2099999999999999
            v2 = 1234567890
            store.sql("INSERT INTO abe_test_1 (test_id, txout_value, i2)"
                      " VALUES (?, ?, ?)",
                      (1, store.intin(v1), v2))
            store.commit()
            prod, o1 = store.selectrow(
                "SELECT txout_approx_value * i2, i1 FROM abe_test_v1")
            prod = int(prod)
            o1 = int(o1)
            if prod < v1 * v2 * 1.0001 and prod > v1 * v2 * 0.9999 and o1 == v1:
                v3 = 9226543405000000000L
                store.sql("""INSERT INTO abe_test_1 (test_id, txout_value)
                           VALUES (2, ?)""", (store.intin(v3),))
                (v3o,) = store.selectrow("SELECT txout_value FROM abe_test_1"
                                         " WHERE test_id = 2")
                return abs(float(int(v3o)) / v3 - 1.0) < 0.0001
            return False
        except store.module.DatabaseError, e:
            store.rollback()
            return False
        except Exception, e:
            store.rollback()
            return False
        finally:
            store.drop_view_if_exists("abe_test_v1")
            store.drop_table_if_exists("abe_test_1")

    def _test_sequence_type(store):
        store.drop_table_if_exists("abe_test_1")
        store.drop_sequence_if_exists("abe_test_1")

        try:
            store.ddl(
                """CREATE TABLE abe_test_1 (
                    abe_test_1_id NUMERIC(12) NOT NULL PRIMARY KEY,
                    foo VARCHAR(10))""")
            store.create_sequence('abe_test_1')
            id1 = store.new_id('abe_test_1')
            id2 = store.new_id('abe_test_1')
            if int(id1) != int(id2):
                return True
            return False
        except store.module.DatabaseError, e:
            store.rollback()
            return False
        except Exception, e:
            store.rollback()
            return False
        finally:
            store.drop_table_if_exists("abe_test_1")
            try:
                store.drop_sequence("abe_test_1")
            except store.module.DatabaseError:
                store.rollback()

    def configure_limit_style(store):
        for val in ['native', 'emulated']:
            store.config['limit_style'] = val
            store._set_sql_flavour()
            if store._test_limit_style():
                store.log.info("limit_style=%s", val)
                return
        raise Exception("Can not emulate LIMIT.")

    def _test_limit_style(store):
        store.drop_table_if_exists("abe_test_1")
        try:
            store.ddl(
                """CREATE TABLE abe_test_1 (
                    abe_test_1_id NUMERIC(12) NOT NULL PRIMARY KEY)""")
            for id in (2, 4, 6, 8):
                store.sql("INSERT INTO abe_test_1 (abe_test_1_id) VALUES (?)",
                          (id,))
            rows = store.selectall(
                """SELECT abe_test_1_id FROM abe_test_1 ORDER BY abe_test_1_id
                    LIMIT 3""")
            return [int(row[0]) for row in rows] == [2, 4, 6]
        except store.module.DatabaseError, e:
            store.rollback()
            return False
        except Exception, e:
            store.rollback()
            return False
        finally:
            store.drop_table_if_exists("abe_test_1")

    def save_config(store):
        store.config['schema_version'] = SCHEMA_VERSION
        for name in store.config.keys():
            store.save_configvar(name)

    def save_configvar(store, name):
        store.sql("UPDATE configvar SET configvar_value = ?"
                  " WHERE configvar_name = ?", (store.config[name], name))
        if store.cursor.rowcount == 0:
            store.sql("INSERT INTO configvar (configvar_name, configvar_value)"
                      " VALUES (?, ?)", (name, store.config[name]))

    def set_configvar(store, name, value):
        store.config[name] = value
        store.save_configvar(name)

    def cache_block(store, block_id, height, prev_id, search_id):
        assert isinstance(block_id, int), block_id
        assert isinstance(height, int), height
        assert prev_id is None or isinstance(prev_id, int)
        assert search_id is None or isinstance(search_id, int)
        block = {
            'height':    height,
            'prev_id':   prev_id,
            'search_id': search_id}
        store._blocks[block_id] = block
        return block

    def _load_block(store, block_id):
        block = store._blocks.get(block_id)
        if block is None:
            row = store.selectrow("""
                SELECT block_height, prev_block_id, search_block_id
                  FROM block
                 WHERE block_id = ?""", (
