# -*- coding: utf-8 -*-
import socket
from functools import partial
from itertools import izip
import logging
from collections import Iterable, defaultdict
import weakref
import traceback

from tornado.ioloop import IOLoop
from tornado.iostream import IOStream
from adisp import async, process

from datetime import datetime
from brukva.exceptions import RequestError, ConnectionError, ResponseError, InvalidResponse

log = logging.getLogger('brukva.client')

class ExecutionContext(object):
    def __init__(self, callbacks, error_wrapper=None):
        self.callbacks = callbacks
        self.error_wrapper = error_wrapper
        self.is_active = True

    def _call_callbacks(self, callbacks, value):
        if callbacks:
            if isinstance(callbacks, Iterable):
                for cb in callbacks:
                    cb(value)
            else:
                callbacks(value)

    def __enter__(self):
        return self

    def __exit__(self, type_, value, tb):
        if type_ is None:
            return True

        if self.error_wrapper:
            value = self.error_wrapper(value)
        else:
            value = value or Exception(
                'Strange exception with None value type: %s; tb: %s' %
                (type_, '\n'.join(traceback.format_tb(tb))
            ))

        if self.is_active:
            log.error(value, exc_info=(type_, value, tb))
            self.ret_call(value)
            return True
        else:
            return False

    def disable(self):
        self.is_active = False

    def enable(self):
        self.is_active = True

    def ret_call(self, value):
        self.is_active = False
        self._call_callbacks(self.callbacks, value)
        self.is_active = True

    def safe_call(self, callbacks, value):
        self.is_active = False
        self._call_callbacks(callbacks, value)
        self.is_active = True


def execution_context(callbacks, error_wrapper=None):
    """
    Syntax sugar.
    If some error occurred inside with block,
    it will be suppressed and forwarded to callbacks.

    Use contex.ret_call(value) method to call callbacks.

    @type callbacks: callable or iterator over callables
    @rtype: context
    """
    return ExecutionContext(callbacks, error_wrapper)

class Message(object):
    ''' Wrapper Message object.
        kind = command
       channel = channel from which the message was received
        pattern = subscription pattern
        body = message body
    '''
    def __init__(self, *args):
        if len(args) == 3:
            (self.kind, self.channel, self.body) = args
            self.pattern = self.channel
        elif len(args) == 4:
            (self.kind, self.channel, self.pattern, self.body) = args
        else:
            raise ValueError('Invalid number of arguments')

class CmdLine(object):
    def __init__(self, cmd, *args, **kwargs):
        self.cmd = cmd
        self.args = args
        self.kwargs = kwargs

    def __repr__(self):
        return self.cmd + '(' + str(self.args)  + ',' + str(self.kwargs) + ')'

def string_keys_to_dict(key_string, callback):
    return dict([(key, callback) for key in key_string.split()])

def dict_merge(*dicts):
    merged = {}
    [merged.update(d) for d in dicts]
    return merged

def encode(value):
    if isinstance(value, str):
        return value
    elif isinstance(value, unicode):
        return value.encode('utf-8')
    # pray and hope
    return str(value)

def format(*tokens):
    cmds = []
    for t in tokens:
        e_t = encode(t)
        cmds.append('$%s\r\n%s\r\n' % (len(e_t), e_t))
    return '*%s\r\n%s' % (len(tokens), ''.join(cmds))

def format_pipeline_request(command_stack):
    return ''.join(format(c.cmd, *c.args, **c.kwargs) for c in command_stack)

class Connection(object):
    def __init__(self, host, port, on_connect, on_disconnect, timeout=None, io_loop=None):
        self.host = host
        self.port = port
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.timeout = timeout
        self._stream = None
        self._io_loop = io_loop
        self.try_left = 2

        self.in_progress = False
        self.read_queue = []

    def connect(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
            sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(self.timeout)
            sock.connect((self.host, self.port))
            self._stream = IOStream(sock, io_loop=self._io_loop)
            self.connected()
        except socket.error, e:
            raise ConnectionError(str(e))
        self.on_connect()

    def disconnect(self):
        if self._stream:
            try:
                self._stream.close()
            except socket.error, e:
                pass
            self._stream = None

    def write(self, data, try_left=None):
        if try_left is None:
            try_left = self.try_left
        if not self._stream:
            self.connect()
            if not self._stream:
                raise ConnectionError('Tried to write to non-existent connection')

        if try_left > 0:
            try:
                self._stream.write(data)
            except IOError:
                self.disconnect()
                self.write(data, try_left - 1)
        else:
            raise ConnectionError('Tried to write to non-existent connection')

    def read(self, length, callback):
        try:
            if not self._stream:
                self.disconnect()
                raise ConnectionError('Tried to read from non-existent connection')
            self._stream.read_bytes(length, callback)
        except IOError:
            self.on_disconnect()

    def readline(self, callback):
        try:
            if not self._stream:
                self.disconnect()
                raise ConnectionError('Tried to read from non-existent connection')
            self._stream.read_until('\r\n', callback)
        except IOError:
            self.on_disconnect()

    def try_to_perform_read(self):
        if not self.in_progress and self.read_queue:
            self.in_progress = True
            self._io_loop.add_callback(partial(self.read_queue.pop(0), None) )

    @async
    def queue_wait(self, callback):
        self.read_queue.append(callback)
        self.try_to_perform_read()

    def read_done(self):
        self.in_progress = False
        self.try_to_perform_read()

    def connected(self):
        if self._stream:
            return True
        return False

def reply_to_bool(r, *args, **kwargs):
    return bool(r)

def make_reply_assert_msg(msg):
    def reply_assert_msg(r, *args, **kwargs):
        return r == msg
    return reply_assert_msg

def reply_set(r, *args, **kwargs):
    return set(r)

def reply_dict_from_pairs(r, *args, **kwargs):
    return dict(izip(r[::2], r[1::2]))

def reply_str(r, *args, **kwargs):
    return r or ''

def reply_int(r, *args, **kwargs):
    return int(r) if r is not None else None

def reply_float(r, *args, **kwargs):
    return float(r) if r is not None else None

def reply_datetime(r, *args, **kwargs):
    return datetime.fromtimestamp(int(r))

def reply_pubsub_message(r, *args, **kwargs):
    return Message(*r)

def reply_zset(r, *args, **kwargs):
    if (not r ) or (not 'WITHSCORES' in args):
        return r
    return zip(r[::2], map(float, r[1::2]))

def reply_hmget(r, key, *fields, **kwargs):
    return dict(zip(fields, r))

def reply_info(response):
    info = {}
    def get_value(value):
        if ',' not in value:
            return value
        sub_dict = {}
        for item in value.split(','):
            k, v = item.split('=')
            try:
                sub_dict[k] = int(v)
            except ValueError:
                sub_dict[k] = v
        return sub_dict
    for line in response.splitlines():
        key, value = line.split(':')
        try:
            info[key] = int(value)
        except ValueError:
            info[key] = get_value(value)
    return info

def reply_ttl(r, *args, **kwargs):
    return r != -1 and r or None


PUB_SUB_COMMANDS = set([
    'SUBSCRIBE',
    'PSUBSCRIBE',
    'UNSUBSCRIBE',
    'PUNSUBSCRIBE',
    'LISTEN',
])

class _AsyncWrapper(object):
    def __init__(self, obj):
        self.obj = obj
        self.memoized = {}

    def __getattr__(self, item):
        if item not in self.memoized:
            self.memoized[item] = async(getattr(self.obj, item), cbname='callbacks')
        return self.memoized[item]


class Client(object):
    def __init__(self, host='localhost', port=6379, password=None,
            selected_db=None, io_loop=None):
        self._io_loop = io_loop or IOLoop.instance()
        self.connection = Connection(host, port,
            self.on_connect, self.on_disconnect, io_loop=self._io_loop)
        self.async = _AsyncWrapper(weakref.proxy(self))
        self.queue = []
        self.current_cmd_line = None
        self.subscribed = False
        self.password = password
        self.selected_db = selected_db
        self.REPLY_MAP = dict_merge(
                string_keys_to_dict('AUTH BGREWRITEAOF BGSAVE DEL EXISTS EXPIRE HDEL HEXISTS '
                                    'HMSET MOVE MSET MSETNX SAVE SETNX',
                                    reply_to_bool),
                string_keys_to_dict('FLUSHALL FLUSHDB SELECT SET SETEX SHUTDOWN '
                                    'RENAME RENAMENX WATCH UNWATCH',
                                    make_reply_assert_msg('OK')),
                string_keys_to_dict('SMEMBERS SINTER SUNION SDIFF',
                                    reply_set),
                string_keys_to_dict('HGETALL BRPOP BLPOP',
                                    reply_dict_from_pairs),
                string_keys_to_dict('HGET',
                                    reply_str),
                string_keys_to_dict('SUBSCRIBE UNSUBSCRIBE LISTEN '
                                    'PSUBSCRIBE UNSUBSCRIBE',
                                    reply_pubsub_message),
                string_keys_to_dict('ZRANK ZREVRANK',
                                    reply_int),
                string_keys_to_dict('ZSCORE ZINCRBY ZCOUNT ZCARD',
                                    reply_int),
                string_keys_to_dict('ZRANGE ZRANGEBYSCORE ZREVRANGE',
                                    reply_zset),
                {'HMGET': reply_hmget},
                {'PING': make_reply_assert_msg('PONG')},
                {'LASTSAVE': reply_datetime },
                {'TTL': reply_ttl } ,
                {'INFO': reply_info},
                {'MULTI_PART': make_reply_assert_msg('QUEUED')},
            )

        self._waiting_callbacks = defaultdict(list)
        self._pipeline = None

    def __repr__(self):
        return 'Brukva client (host=%s, port=%s)' % (self.connection.host, self.connection.port)

    def pipeline(self, transactional=False):
        if not self._pipeline:
            self._pipeline = Pipeline(
                selected_db=self.selected_db,
                io_loop = self._io_loop,
                transactional=transactional
            )
            self._pipeline.connection = self.connection
        return self._pipeline

    #### connection

    def connect(self):
        self.connection.connect()

    def disconnect(self):
        self.connection.disconnect()

    def on_connect(self):
        if self.password:
            self.auth(self.password)
        if self.selected_db:
            self.select(self.selected_db)

    def on_disconnect(self):
        if self.subscribed:
            self.subscribed = False
        raise ConnectionError("Socket closed on remote end")
    ####

    #### formatting
    def encode(self, value):
        if isinstance(value, str):
            return value
        elif isinstance(value, unicode):
            return value.encode('utf-8')
        # pray and hope
        return str(value)

    def format(self, *tokens):
        cmds = []
        for t in tokens:
            e_t = self.encode(t)
            cmds.append('$%s\r\n%s\r\n' % (len(e_t), e_t))
        return '*%s\r\n%s' % (len(tokens), ''.join(cmds))

    def format_reply(self, cmd_line, data):
        if cmd_line.cmd not in self.REPLY_MAP:
            return data
        try:
            res =  self.REPLY_MAP[cmd_line.cmd](data, *cmd_line.args, **cmd_line.kwargs)
        except Exception, e:
            raise ResponseError(
                'failed to format reply to %s, raw data: %s; err message: %s' %
                (cmd_line, data, e), cmd_line
            )
        return res
    ####

    #### new AsIO
    def call_callbacks(self, callbacks, *args, **kwargs):
        for cb in callbacks:
            cb(*args, **kwargs)

    @process
    def execute_command(self, cmd, callback, *args, **kwargs):
        cmd_line = CmdLine(cmd, *args, **kwargs)
        with execution_context(callback) as ctx:
            if callback is None:
                callback = []
            elif not hasattr(callback, '__iter__'):
                callback = [callback]

            if self.subscribed and cmd not in PUB_SUB_COMMANDS:
                ctx.ret_call(RequestError('Calling not pub/sub command during subscribed state', cmd_line))
                return

            try:
                self.connection.write(self.format(cmd, *args, **kwargs))
            except Exception, e:
                self.connection.disconnect()
                raise e

            if self.subscribed and cmd in ('SUBSCRIBE', 'UNSUBSCRIBE'):
                self._waiting_callbacks[cmd].append(callback)
                return

            yield self.connection.queue_wait()
            data = yield async(self.connection.readline)()
            if not data:
                result = None
                self.connection.read_done()
                raise Exception('TODO: [no data from connection->readline')
            else:
                response = yield self.process_data(data, cmd_line)
                result = self.format_reply(cmd_line, response)

                self.connection.read_done()
            ctx.ret_call(result)

    @async
    @process
    def process_data(self, data, cmd_line, callback):
        with execution_context(callback) as ctx:
            data = data[:-2] # strip \r\n

            if data == '$-1':
                response =  None
            elif data == '*0' or data == '*-1':
                response = []
            else:
                if len(data) == 0:
                    raise IOError('Disconnected')
                head, tail = data[0], data[1:]

                if head == '*':
                    response = yield self.consume_multibulk(int(tail), cmd_line)
                elif head == '$':
                    response = yield self.consume_bulk(int(tail)+2)
                elif head == '+':
                    response = tail
                elif head == ':':
                    response = int(tail)
                elif head == '-':
                    if tail.startswith('ERR'):
                        tail = tail[4:]
                    response = ResponseError(tail, cmd_line)
                else:
                    raise ResponseError('Unknown response type %s' % head, cmd_line)
            ctx.ret_call(response)

    @async
    @process
    def consume_multibulk(self, length, cmd_line, callback):
        with execution_context(callback) as ctx:
            tokens = []
            while len(tokens) < length:
                data = yield async(self.connection.readline)()
                if not data:
                    raise ResponseError(
                        'Not enough data in response to %s, accumulated tokens: %s'%
                        (cmd_line, tokens), cmd_line
                    )
                token = yield self.process_data(data, cmd_line) #FIXME error
                tokens.append( token )

            ctx.ret_call(tokens)

    @async
    @process
    def consume_bulk(self, length, callback):
        with execution_context(callback) as ctx:
            data = yield async(self.connection.read)(length)
            if isinstance(data, Exception):
                raise data
            if not data:
                raise ResponseError('EmptyResponse')
            else:
                data = data[:-2]
            ctx.ret_call(data)
        ####

    ### MAINTENANCE
    def bgrewriteaof(self, callback=None):
        self.execute_command('BGREWRITEAOF', callback)

    def dbsize(self, callback=None):
        self.execute_command('DBSIZE', callback)

    def flushall(self, callback=None):
        self.execute_command('FLUSHALL', callback)

    def flushdb(self, callback=None):
        self.execute_command('FLUSHDB', callback)

    def ping(self, callback=None):
        self.execute_command('PING', callback)

    def info(self, callback=None):
        self.execute_command('INFO', callback)

    def select(self, db, callback=None):
        self.selected_db = db
        self.execute_command('SELECT', callback, db)

    def shutdown(self, callback=None):
        self.execute_command('SHUTDOWN', callback)

    def save(self, callback=None):
        self.execute_command('SAVE', callback)

    def bgsave(self, callback=None):
        self.execute_command('BGSAVE', callback)

    def lastsave(self, callback=None):
        self.execute_command('LASTSAVE', callback)

    def keys(self, pattern, callback=None):
        self.execute_command('KEYS', callback, pattern)

    def auth(self, password, callback=None):
        self.execute_command('AUTH', callback, password)

    ### BASIC KEY COMMANDS
    def append(self, key, value, callback=None):
        self.execute_command('APPEND', callback, key, value)

    def expire(self, key, ttl, callback=None):
        self.execute_command('EXPIRE', callback, key, ttl)

    def ttl(self, key, callback=None):
        self.execute_command('TTL', callback, key)

    def type(self, key, callback=None):
        self.execute_command('TYPE', callback, key)

    def randomkey(self, callback=None):
        self.execute_command('RANDOMKEY', callback)

    def rename(self, src, dst, callback=None):
        self.execute_command('RENAME', callback, src, dst)

    def renamenx(self, src, dst, callback=None):
        self.execute_command('RENAMENX', callback, src, dst)

    def move(self, key, db, callback=None):
        self.execute_command('MOVE', callback, key, db)

    def substr(self, key, start, end, callback=None):
        self.execute_command('SUBSTR', callback, key, start, end)

    def delete(self, key, callback=None):
        self.execute_command('DEL', callback, key)

    def set(self, key, value, callback=None):
        self.execute_command('SET', callback, key, value)

    def setex(self, key, ttl, value, callback=None):
        self.execute_command('SETEX', callback, key, ttl, value)

    def setnx(self, key, value, callback=None):
        self.execute_command('SETNX', callback, key, value)

    def mset(self, mapping, callback=None):
        items = []
        [ items.extend(pair) for pair in mapping.iteritems() ]
        self.execute_command('MSET', callback, *items)

    def msetnx(self, mapping, callback=None):
        items = []
        [ items.extend(pair) for pair in mapping.iteritems() ]
        self.execute_command('MSETNX', callback, *items)

    def get(self, key, callback=None):
        self.execute_command('GET', callback, key)

    def mget(self, keys, callback=None):
        self.execute_command('MGET', callback, *keys)

    def getset(self, key, value, callback=None):
        self.execute_command('GETSET', callback, key, value)

    def exists(self, key, callback=None):
        self.execute_command('EXISTS', callback, key)

    def sort(self, key, start=None, num=None, by=None, get=None, desc=False, alpha=False, store=None, callback=None):
        if (start is not None and num is None) or (num is not None and start is None):
            raise ValueError("``start`` and ``num`` must both be specified")

        tokens = [key]
        if by is not None:
            tokens.append('BY')
            tokens.append(by)
        if start is not None and num is not None:
            tokens.append('LIMIT')
            tokens.append(start)
            tokens.append(num)
        if get is not None:
            tokens.append('GET')
            tokens.append(get)
        if desc:
            tokens.append('DESC')
        if alpha:
            tokens.append('ALPHA')
        if store is not None:
            tokens.append('STORE')
            tokens.append(store)
        return self.execute_command('SORT', callback, *tokens)

    ### COUNTERS COMMANDS
    def incr(self, key, callback=None):
        self.execute_command('INCR', callback, key)

    def decr(self, key, callback=None):
        self.execute_command('DECR', callback, key)

    def incrby(self, key, amount, callback=None):
        self.execute_command('INCRBY', callback, key, amount)

    def decrby(self, key, amount, callback=None):
        self.execute_command('DECRBY', callback, key, amount)

    ### LIST COMMANDS
    def blpop(self, keys, timeout=0, callback=None):
        tokens = list(keys)
        tokens.append(timeout)
        self.execute_command('BLPOP', callback, *tokens)

    def brpop(self, keys, timeout=0, callback=None):
        tokens = list(keys)
        tokens.append(timeout)
        self.execute_command('BRPOP', callback, *tokens)

    def brpoplpush(self, src, dst, timeout=1, callback=None):
        tokens = [src, dst, timeout]
        self.execute_command('BRPOPLPUSH', callback, *tokens)

    def lindex(self, key, index, callback=None):
        self.execute_command('LINDEX', callback, key, index)

    def llen(self, key, callback=None):
        self.execute_command('LLEN', callback, key)

    def lrange(self, key, start, end, callback=None):
        self.execute_command('LRANGE', callback, key, start, end)

    def lrem(self, key, value, num=0, callback=None):
        self.execute_command('LREM', callback, key, num, value)

    def lset(self, key, index, value, callback=None):
        self.execute_command('LSET', callback, key, index, value)

    def ltrim(self, key, start, end, callback=None):
        self.execute_command('LTRIM', callback, key, start, end)

    def lpush(self, key, value, callback=None):
        self.execute_command('LPUSH', callback, key, value)

    def rpush(self, key, value, callback=None):
        self.execute_command('RPUSH', callback, key, value)

    def lpop(self, key, callback=None):
        self.execute_command('LPOP', callback, key)

    def rpop(self, key, callback=None):
        self.execute_command('RPOP', callback, key)

    def rpoplpush(self, src, dst, callback=None):
        self.execute_command('RPOPLPUSH', callback, src, dst)

    ### SET COMMANDS
    def sadd(self, key, value, callback=None):
        self.execute_command('SADD', callback, key, value)

    def srem(self, key, value, callback=None):
        self.execute_command('SREM', callback, key, value)

    def scard(self, key, callback=None):
        self.execute_command('SCARD', callback, key)

    def spop(self, key, callback=None):
        self.execute_command('SPOP', callback, key)

    def smove(self, src, dst, value, callback=None):
        self.execute_command('SMOVE', callback, src, dst, value)

    def sismember(self, key, value, callback=None):
        self.execute_command('SISMEMBER', callback, key, value)

    def smembers(self, key, callback=None):
        self.execute_command('SMEMBERS', callback, key)

    def srandmember(self, key, callback=None):
        self.execute_command('SRANDMEMBER', callback, key)

    def sinter(self, keys, callback=None):
        self.execute_command('SINTER', callback, *keys)

    def sdiff(self, keys, callback=None):
        self.execute_command('SDIFF', callback, *keys)

    def sunion(self, keys, callback=None):
        self.execute_command('SUNION', callback, *keys)

    def sinterstore(self, keys, dst, callback=None):
        self.execute_command('SINTERSTORE', callback, dst, *keys)

    def sunionstore(self, keys, dst, callback=None):
        self.execute_command('SUNIONSTORE', callback, dst, *keys)

    def sdiffstore(self, keys, dst, callback=None):
        self.execute_command('SDIFFSTORE', callback, dst, *keys)

    ### SORTED SET COMMANDS
    def zadd(self, key, score, value, callback=None):
        self.execute_command('ZADD', callback, key, score, value)

    def zcard(self, key, callback=None):
        self.execute_command('ZCARD', callback, key)

    def zincrby(self, key, value, amount, callback=None):
        self.execute_command('ZINCRBY', callback, key, amount, value)

    def zrank(self, key, value, callback=None):
        self.execute_command('ZRANK', callback, key, value)

    def zrevrank(self, key, value, callback=None):
        self.execute_command('ZREVRANK', callback, key, value)

    def zrem(self, key, value, callback=None):
        self.execute_command('ZREM', callback, key, value)

    def zcount(self, key, start, end, offset=None, limit=None, with_scores=None, callback=None):
        tokens = [key, start, end]
        if offset is not None:
            tokens.append('LIMIT')
            tokens.append(offset)
            tokens.append(limit)
        if with_scores:
            tokens.append('WITHSCORES')
        self.execute_command('ZCOUNT', callback, *tokens)

    def zcard(self, key, callback=None):
        self.execute_command('ZCARD', callback, key)

    def zscore(self, key, value, callback=None):
        self.execute_command('ZSCORE', callback, key, value)

    def zrange(self, key, start, num, with_scores, callback=None):
        tokens = [key, start, num]
        if with_scores:
            tokens.append('WITHSCORES')
        self.execute_command('ZRANGE', callback, *tokens)

    def zrevrange(self, key, start, num, with_scores, callback=None):
        tokens = [key, start, num]
        if with_scores:
            tokens.append('WITHSCORES')
        self.execute_command('ZREVRANGE', callback, *tokens)

    def zrangebyscore(self, key, start, end, offset=None, limit=None, with_scores=False, callback=None):
        tokens = [key, start, end]
        if offset is not None:
            tokens.append('LIMIT')
            tokens.append(offset)
            tokens.append(limit)
        if with_scores:
            tokens.append('WITHSCORES')
        self.execute_command('ZRANGEBYSCORE', callback, *tokens)

    def zremrangebyrank(self, key, start, end, callback=None):
        self.execute_command('ZREMRANGEBYRANK', callback, key, start, end)

    def zremrangebyscore(self, key, start, end, callback=None):
        self.execute_command('ZREMRANGEBYSCORE', callback, key, start, end)

    def zinterstore(self, dest, keys, aggregate=None, callback=None):
        return self._zaggregate('ZINTERSTORE', dest, keys, aggregate, callback)

    def zunionstore(self, dest, keys, aggregate=None, callback=None):
        return self._zaggregate('ZUNIONSTORE', dest, keys, aggregate, callback)

    def _zaggregate(self, command, dest, keys, aggregate, callback):
        tokens = [dest, len(keys)]
        if isinstance(keys, dict):
            items = keys.items()
            keys = [i[0] for i in items]
            weights = [i[1] for i in items]
        else:
            weights = None
        tokens.extend(keys)
        if weights:
            tokens.append('WEIGHTS')
            tokens.extend(weights)
        if aggregate:
            tokens.append('AGGREGATE')
            tokens.append(aggregate)
        return self.execute_command(command, callback, *tokens)

    ### HASH COMMANDS
    def hgetall(self, key, callback=None):
        self.execute_command('HGETALL', callback, key)

    def hmset(self, key, mapping, callback=None):
        items = []
        [ items.extend(pair) for pair in mapping.iteritems() ]
        self.execute_command('HMSET', callback, key, *items)

    def hset(self, key, field, value, callback=None):
        self.execute_command('HSET', callback, key, field, value)

    def hget(self, key, field, callback=None):
        self.execute_command('HGET', callback, key, field)

    def hdel(self, key, field, callback=None):
        self.execute_command('HDEL', callback, key, field)

    def hlen(self, key, callback=None):
        self.execute_command('HLEN', callback, key)

    def hexists(self, key, field, callback=None):
        self.execute_command('HEXISTS', callback, key, field)

    def hincrby(self, key, field, amount=1, callback=None):
        self.execute_command('HINCRBY', callback, key, field, amount)

    def hkeys(self, key, callback=None):
        self.execute_command('HKEYS', callback, key)

    def hmget(self, key, fields, callback=None):
        self.execute_command('HMGET', callback, key, *fields)

    def hvals(self, key, callback=None):
        self.execute_command('HVALS', callback, key)

    ### PUBSUB
    def subscribe(self, channels, callback=None):
        self._subscribe('SUBSCRIBE', channels, callback)

    def psubscribe(self, channels, callback=None):
        self._subscribe('PSUBSCRIBE', channels, callback)

    def _subscribe(self, cmd, channels, callback=None):
        callback = callback or []
        if not isinstance(callback, Iterable):
            callback = [callback]
        if isinstance(channels, basestring):
            channels = [channels]
        if not self.subscribed:
            callback = list(callback) + [self.on_subscribed]
        self.execute_command(cmd, callback, *channels)

    def on_subscribed(self, result):
        self.subscribed = True

    def unsubscribe(self, channels, callback=None):
        self._unsubscribe('UNSUBSCRIBE', channels, callback)

    def punsubscribe(self, channels, callback=None):
        self._unsubscribe('UNSUBSCRIBE', channels, callback)

    def _unsubscribe(self, cmd, channels, callback=None):
        callback = callback or []
        if not isinstance(callback, Iterable):
            callback = [callback]
        if isinstance(channels, basestring):
            channels = [channels]
        callback = list(callback)
        self.execute_command(cmd, callback, *channels)

    def on_unsubscribed(self, *args, **kwargs):
        self.subscribed = False

    def publish(self, channel, message, callback=None):
        self.execute_command('PUBLISH', callback, channel, message)

    @process
    def listen(self, callback=None):
        # 'LISTEN' is just for receiving information, it is not actually sent anywhere
        def error_wrapper(e):
            if isinstance(e, GeneratorExit):
                return ConnectionError('Connection lost')
            else:
                return e

        with execution_context(callback, error_wrapper) as ctx:
            callback = callback or []
            if not hasattr(callback, '__iter__'):
                callback = [callback]
            yield self.connection.queue_wait()

            cmd_listen = CmdLine('LISTEN')
            while self.subscribed:
                data = yield async(self.connection.readline)()
                if isinstance(data, Exception):
                    raise data

                response = yield self.process_data(data, cmd_listen)
                if isinstance(response, Exception):
                    raise response

                result = self.format_reply(cmd_listen, response)

                if result.kind not in ('message', 'pmessage'):
                    waiting_stack = self._waiting_callbacks[result.kind.upper()]
                    if len(waiting_stack) > 0:
                        ctx.safe_call(waiting_stack.pop(0), result)

                    if result.kind == 'unsubscribe' and result.body == 0:
                        self.on_unsubscribed()
                        self.connection.read_done()
                        break
                else:
                    ctx.ret_call(result)

    ### CAS
    def watch(self, key, callback=None):
        self.execute_command('WATCH', callback, key)

    def unwatch(self, callback=None):
        self.execute_command('UNWATCH', callback)

class Pipeline(Client):
    def __init__(self, transactional, *args, **kwargs):
        super(Pipeline, self).__init__(*args, **kwargs)
        self.transactional = transactional
        self.command_stack = []

    def execute_command(self, cmd, callback, *args, **kwargs):
        if cmd in ('AUTH', 'SELECT'):
            super(Pipeline, self).execute_command(cmd, callback, *args, **kwargs)
        elif cmd in PUB_SUB_COMMANDS:
            raise RequestError(
                'Client is not supposed to issue command %s in pipeline' % cmd)
        self.command_stack.append(CmdLine(cmd, *args, **kwargs))

    def discard(self): # actually do nothing with redis-server, just flush command_stack
        self.command_stack = []

    @process
    def execute(self, callback):
        with execution_context(callback) as ctx:
            command_stack = self.command_stack
            self.command_stack = []

            if callback is None:
                callback = []
            elif not hasattr(callback, '__iter__'):
                callback = [callback]

            if self.transactional:
                command_stack = [CmdLine('MULTI')] + command_stack + [CmdLine('EXEC')]

            request =  format_pipeline_request(command_stack)

            try:
                self.connection.write(request)
            except IOError:
                self.command_stack = []
                self.connection.disconnect()
                raise ConnectionError("Socket closed on remote end")
            except Exception, e:
                self.command_stack = []
                self.connection.disconnect()
                raise e

            yield self.connection.queue_wait()
            responses = []
            total = len(command_stack)
            cmds = iter(command_stack)

            while len(responses) < total:
                data = yield async(self.connection.readline)()
                if not data:
                    raise ResponseError('Not enough data after EXEC')
                try:
                    cmd_line = cmds.next()
                    if self.transactional and cmd_line.cmd != 'EXEC':
                        response = yield self.process_data(data, CmdLine('MULTI_PART'))
                    else:
                        response = yield self.process_data(data, cmd_line)
                    responses.append(response)
                except Exception,e :
                    responses.append(e)
            self.connection.read_done()

            def format_replies(cmd_lines, responses):
                results = []
                for cmd_line, response in zip(cmd_lines, responses):
                    try:
                        results.append(self.format_reply(cmd_line, response))
                    except Exception, e:
                        results.append(e)
                return results

            if self.transactional:
                command_stack = command_stack[:-1]
                responses = responses[-1] # actual data only from EXEC command
                            #FIXME:  assert all other responses to be 'QUEUED'
                log.info('responses %s', responses)
                results = format_replies(command_stack[1:], responses)
                log.info('results %s', results)
            else:
                results = format_replies(command_stack, responses)

            ctx.ret_call(results)
