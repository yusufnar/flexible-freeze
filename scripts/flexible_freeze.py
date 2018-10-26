'''Flexible Freeze script for PostgreSQL databases
Version 0.5
(c) 2014 PostgreSQL Experts Inc.
Licensed under The PostgreSQL License

This script is designed for doing VACUUM FREEZE or VACUUM ANALYZE runs
on your database during known slow traffic periods.  If doing both
vacuum freezes and vacuum analyzes, do the freezes first.

Takes a timeout that it won't overrun your slow traffic period.
Note that this is the time to START a vacuum, so a large table
may still overrun the vacuum period, unless you use the --enforce-time switch.
'''

import time
import sys
import signal
import argparse
import psycopg2
import datetime
from multiprocessing import Process, Lock

def timestamp():
    now = time.time()
    return time.strftime("%Y-%m-%d %H:%M:%S %Z")

if sys.version_info[:2] not in ((2,6), (2,7),):
    print >>sys.stderr, "python 2.6 or 2.7 required; you have %s" % sys.version
    exit(1)

parser = argparse.ArgumentParser()
parser.add_argument("-m", "--minutes", dest="run_min",
                    type=int, default=120,
                    help="Number of minutes to run before halting.  Defaults to 2 hours")
parser.add_argument("-d", "--databases", dest="dblist",
                    help="Comma-separated list of databases to vacuum, if not all of them")
parser.add_argument("-T", "--exclude-table", action="append", dest="tables_to_exclude",
                    help="Exclude any table with this name (in any database). You can pass this option multiple times to exclude multiple tables.")
parser.add_argument("--exclude-table-in-database", action="append", dest="exclude_table_in_database",
                    help="Argument is of form 'DATABASENAME.TABLENAME' exclude the named table, but only when processing the named database. You can pass this option multiple times.")
parser.add_argument("--vacuum", dest="vacuum", action="store_true",
                    help="Do regular vacuum instead of VACUUM FREEZE")
parser.add_argument("--pause", dest="pause_time", type=int, default=10,                    
                    help="seconds to pause between vacuums.  Default is 10.")
parser.add_argument("--freezeage", dest="freezeage",
                    type=int, default=10000000,
                    help="minimum age for freezing.  Default 10m XIDs")
parser.add_argument("--costdelay", dest="costdelay", 
                    type=int, default = 20,
                    help="vacuum_cost_delay setting in ms.  Default 20")
parser.add_argument("--costlimit", dest="costlimit",
                    type=int, default = 2000,
                    help="vacuum_cost_limit setting.  Default 2000")
parser.add_argument("-t", "--print-timestamps", action="store_true",
                    dest="print_timestamps")
parser.add_argument("--enforce-time", dest="enforcetime", action="store_true",
                    help="enforce time limit by terminating vacuum")
parser.add_argument("-l", "--log", dest="logfile")
parser.add_argument("-v", "--verbose", action="store_true",
                    dest="verbose")
parser.add_argument("--debug", action="store_true",
                    dest="debug")
parser.add_argument("-U", "--user", dest="dbuser",
                  help="database user")
parser.add_argument("-H", "--host", dest="dbhost",
                  help="database hostname")
parser.add_argument("-p", "--port", dest="dbport",
                  help="database port")
parser.add_argument("-w", "--password", dest="dbpass",
                  help="database password")
parser.add_argument("-n", "--dry-run", dest="dry_run", action="store_true",
                  help="Don't vacuum; just print what would have been vacuumed.")
parser.add_argument('-j', '--jobs', dest='jobs', default=1,
                    help='number of parallel jobs to run')

args = parser.parse_args()

output_lock=Lock()

def debug_print(some_message):
    output_lock.acquire()
    
    if args.debug:
        print >>sys.stderr, ('DEBUG (%s): ' % timestamp()) + some_message

    output_lock.release()

def verbose_print(some_message):
    output_lock.acquire()
    
    if args.verbose:
        _print(some_message, bypass_lock = True)

    output_lock.release()

def _print(some_message, bypass_lock = False):
    if not bypass_lock:
        output_lock.acquire()
    
    if args.print_timestamps:
        print "{timestamp}: {some_message}".format(timestamp=timestamp(), some_message=some_message)
    else:
        print some_message
    sys.stdout.flush()

    if not bypass_lock:
        output_lock.release()


def dbconnect(dbname, dbuser, dbhost, dbport, dbpass):

    if dbname:
        connect_string ="dbname=%s application_name=flexible_freeze" % dbname
    else:
        _print("ERROR: a target database is required.")
        return None

    if dbhost:
        connect_string += " host=%s " % dbhost

    if dbuser:
        connect_string += " user=%s " % dbuser

    if dbpass:
        connect_string += " password=%s " % dbpass

    if dbport:
        connect_string += " port=%s " % dbport

    conn = psycopg2.connect( connect_string )
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)

    return conn

def signal_handler(signal, frame):
    _print('exiting due to user interrupt')
    if conn:
        try:
            conn.close()
        except:
            verbose_print('could not clean up db connections')
        
    sys.exit(0)

# </global>

# startup debugging info

debug_print("python version: %s" % sys.version)
debug_print("psycopg2 version: %s" % psycopg2.__version__)
debug_print("argparse version: %s" % argparse.__version__)
debug_print("parameters: %s" % repr(args))

# process arguments that argparse can't handle completely on its own

database_table_excludemap = {}
if args.exclude_table_in_database:
    for elem in args.exclude_table_in_database:
        parts = elem.split(".")
        if len(parts) != 2:
            print >>sys.stderr, "invalid argument '{arg}' to flag --exclude-table-in-database: argument must be of the form DATABASE.TABLE".format(arg=elem)
            exit(2)
        else:
            dat = parts[0]
            tab = parts[1]
            if dat in database_table_excludemap:
                database_table_excludemap[dat].append(tab)
            else:
                database_table_excludemap[dat] = [tab]
                exit

debug_print("database_table_excludemap: {m}".format(m=database_table_excludemap))

# get set for user interrupt
time_exit = None
signal.signal(signal.SIGINT, signal_handler)

# start logging to log file, if used
if args.logfile:
    try:
        sys.stdout = open(args.logfile, 'a')
    except Exception as ex:
        _print('could not open logfile: %s' % str(ex))
        sys.exit(1)

    _print('')
    _print('='*40)
    _print('flexible freeze started %s' % str(datetime.datetime.now()))
    verbose_print('arguments: %s' % str(args))

# do we have a database list?
# if not, connect to "postgres" database and get a list of non-system databases
dblist = None
if args.dblist is None:
    conn = None
    try:
        dbname = 'postgres'
        conn = dbconnect(dbname, args.dbuser, args.dbhost, args.dbport, args.dbpass)
    except Exception as ex:
        _print("Could not list databases: connection to database {d} failed: {e}".format(d=dbname, e=str(ex)))
        sys.exit(1)

    cur = conn.cursor()
    cur.execute("""SELECT datname FROM pg_database
        WHERE datname NOT IN ('postgres','template1','template0')
        ORDER BY age(datfrozenxid) DESC""")
    dblist = []
    for dbname in cur:
        dblist.append(dbname[0])

    conn.close()
    if not dblist:
        _print("no databases to vacuum, aborting")
        sys.exit(1)
else:
    dblist = args.dblist.split(',')

verbose_print("Flexible Freeze run starting")
n_dbs = len(dblist)
verbose_print("Processing {n} database{pl} (list of databases is {l})".format(n = n_dbs, l = ', '.join(dblist), pl = '' if n_dbs == 1 else 's'))

# </global>


def get_table_list_for_db(db):
    verbose_print("finding tables in database {0}".format(db))
    conn = None
    try:
        conn = dbconnect(db, args.dbuser, args.dbhost, args.dbport, args.dbpass)
    except Exception as err:
        _print("skipping database {d} (couldn't connect: {e})".format(d=db, e=str(err)))
        return None

    cur = conn.cursor()

    exclude_clause = None
    tables = []
    
    if args.tables_to_exclude:
        tables.extend(args.tables_to_exclude)

    if db in database_table_excludemap:
        db_tables = database_table_excludemap[db]
        global_tables = args.tables_to_exclude
        tables.extend(db_tables)

    debug_print('tables: {t}'.format(t=tables))

    if tables:
        quoted_table_names = map(lambda table_name: "'" + table_name + "'",
                                 tables)
        exclude_clause = 'AND full_table_name::text NOT IN (' + ', '.join(quoted_table_names) + ')'
    else:
        exclude_clause = ''
        
    # if vacuuming, get list of top tables to vacuum
    if args.vacuum:
        tabquery = """WITH deadrow_tables AS (
                SELECT relid::regclass as full_table_name,
                    ((n_dead_tup::numeric) / ( n_live_tup + 1 )) as dead_pct,
                    pg_relation_size(relid) as table_bytes
                FROM pg_stat_user_tables
                WHERE n_dead_tup > 100
                AND ( (now() - last_autovacuum) > INTERVAL '1 hour'
                    OR last_autovacuum IS NULL )
                AND ( (now() - last_vacuum) > INTERVAL '1 hour'
                    OR last_vacuum IS NULL )
            )
            SELECT full_table_name
            FROM deadrow_tables
            WHERE dead_pct > 0.05
            AND table_bytes > 1000000
            {exclude_clause}
            ORDER BY dead_pct DESC, table_bytes DESC;""".format(exclude_clause=exclude_clause)
    else:
    # if freezing, get list of top tables to freeze
    # includes TOAST tables in case the toast table has older rows
        tabquery = """WITH tabfreeze AS (
                SELECT pg_class.oid::regclass AS full_table_name,
                greatest(age(pg_class.relfrozenxid), age(toast.relfrozenxid)) as freeze_age,
                pg_relation_size(pg_class.oid)
            FROM pg_class JOIN pg_namespace ON pg_class.relnamespace = pg_namespace.oid
                LEFT OUTER JOIN pg_class as toast
                    ON pg_class.reltoastrelid = toast.oid
            WHERE nspname not in ('pg_catalog', 'information_schema')
                AND nspname NOT LIKE 'pg_temp%'
                AND pg_class.relkind = 'r'
            )
            SELECT full_table_name
            FROM tabfreeze
            WHERE freeze_age > {freeze_age}
            {exclude_clause}
            ORDER BY freeze_age DESC
            LIMIT 1000;""".format(freeze_age=args.freezeage,
                                  exclude_clause=exclude_clause)

    debug_print('{db} tabquery: {q}'.format(db=db, q=tabquery))
    cur.execute(tabquery)
    verbose_print("getting list of tables")

    table_resultset = cur.fetchall()
    tablist = map(lambda(row): row[0], table_resultset)

    conn.close()
    return tablist
    
def get_candidates():
    database_table_vacuummap = {}
    for db in dblist:
        tables = get_table_list_for_db(db)
        debug_print('looking for eligible tables in {db}... found {t}'.format(db=db, t=tables))
        if (tables):
            database_table_vacuummap[db] = tables

    debug_print('DBs and tables to vacuum: {m}'.format(m=database_table_vacuummap))

    return database_table_vacuummap

def flatten(db_table_map):
    """
    Take a dict where each key is a DB, and each values is a list of tables in that database.
    Return a list of (db, table) pairs.

    >>>flatten({ 'drupaceous': ['peach'], 'citrus': ['orange', 'lime', 'lemon'] })
    [['drupaceous', 'peach'], ['citrus', 'orange'], ['citrus', 'lime'], ['citrus', 'lemon']]
    """
    
    pairs = []
    for db in db_table_map:
        for table in db_table_map[db]:
            pairs.append([db, table])

    return pairs

def split_into_lists(items, n):
    """
    Take a list, split it into n lists, and return those n lists as a list.
    Example: split_into_lists(['a', 'b', 'c', 'd'], 2) returns [ [1, 3], [2, 4] ]
    """

    # Start out with a list of n empty lists, e.g. [ [], [] ]
    n = int(n)
    lists = []
    for x in range(n):
        lists.append([])
    debug_print('lists before filling: {l}'.format(l=lists))

    # Then distribute the items into those lists:
    # first item goes into lists[0], second item goes into lists[1], and so on.
    i = 0
    while True:
        # If we got to the end of the list of lists,
        # but we still have items,
        # start again from the beginning of the list of lists.
        if i == n:
            i = 0

        # Keep going as long as we have items.
        if items:
            lists[i].append(items.pop(0))
            i += 1
        else:
            # When we run out of items to distribute among the lists, stop.
            break

    debug_print('lists after filling: {l}'.format(l=lists))
    #debug_print('lists[0]: {l}'.format(l=lists[0]))
    #debug_print('lists[1]: {l}'.format(l=lists[1]))
    return lists


def worker(id, db_table_pairs, global_args, output_lock):
    tables_processed = []
    halt_time = time.time() + ( args.run_min * 60 )

    for pair in db_table_pairs:
        db = pair[0]
        table = pair[1]

        conn = None
        try:
            conn = dbconnect(db, global_args.dbuser, global_args.dbhost, global_args.dbport, global_args.dbpass)
        except Exception as ex:
            _print("Worker {i} could not connect to database {db}: '{err}'. Failed process was supposed to process [db, table] pairs '{p}'".format(i=id, db=db, err=ex, p=db_table_pairs))
            break
        cur = conn.cursor()
        cur.execute("SET vacuum_cost_delay = {0}".format(global_args.costdelay))
        cur.execute("SET vacuum_cost_limit = {0}".format(global_args.costlimit))
        
        if time.time() >= halt_time:
            _verbose_print('Worker {i} reached time limit; terminating subprocess.'.format(i=id))
            break
        else:
            # figure out statement_timeout
            if args.enforcetime:
                timeout_secs = int(halt_time - time.time()) + 30
                timeout_query = """SET statement_timeout = '%ss'""" % timeout_secs

            # Do the actual vacuuming.
            verbose_print("Worker {i} processing table {t} in database {d}".format(i=id, t=table, d=db))
            if args.vacuum:
                exquery = """VACUUM ANALYZE %s""" % table
            else:
                exquery = """VACUUM FREEZE ANALYZE %s""" % table
            excur = conn.cursor()

            if not args.dry_run:
                try:
                    if args.enforcetime:
                        excur.execute(timeout_query)
                    
                    excur.execute(exquery)
                except Exception as ex:
                    _print('Worker {i} failed to VACUUM table {t} in DB {d}: {ex}'.format(i=id, t=table, d=db, ex=ex))
                    if time.time() >= halt_time:
                        verbose_print('Worker {i} halted flexible_freeze due to enforced time limit'.format(i=id))

            tables_processed.append([db, table])
            time.sleep(args.pause_time)

    verbose_print('Worker {i} terminating after processing {n} table{pl} of {m}: {tables}'.format(i = id, n =len(tables_processed), pl = '' if len(tables_processed) == 1 else 's', tables=tables_processed, m=len(db_table_pairs)))


def create_and_start_processes(items):
    processes = []

    i = 0
    for pairs in items:
        debug_print('Process {number} will process [db, table] pairs {pairs}'.format(number=i, pairs=pairs))
        process = Process(target=worker, args=[i, pairs, args, output_lock])
        processes.append(process)
        i += 1

    debug_print('Processes: {p}'.format(p=processes))
    return processes
    

def main():    
    database_table_map = get_candidates()
    debug_print('database_table_map: {d}'.format(d=database_table_map))
    
    database_table_pairs = flatten(database_table_map)
    debug_print('database_table_pairs: {d}'.format(d=database_table_pairs))

    lists_of_pairs = split_into_lists(items = database_table_pairs, n = args.jobs)

    processes = create_and_start_processes(lists_of_pairs)

    for process in processes:
        process.start()

    for process in processes:
        process.join()

    sys.exit(0)
    
main()



# did we get through all tables?
# exit, report results
# if not time_exit:
#     _print("All tables vacuumed.")
#     # verbose_print("%d tables in %d databases" % (tabcount, dbcount))
# else:
#     _print("Vacuuming halted due to timeout")
#     # verbose_print("after vacuuming %d tables in %d databases" % (tabcount, dbcount,))

# verbose_print("Flexible Freeze run complete")
# sys.exit(0)
