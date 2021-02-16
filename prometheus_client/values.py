from __future__ import unicode_literals

import os
from threading import Lock

import aioredis

from .mmap_dict import mmap_key, MmapedDict


class MutexValue(object):
    """A float protected by a mutex."""

    _multiprocess = False

    def __init__(self, typ, metric_name, name, labelnames, labelvalues, **kwargs):
        self._value = 0.0
        self._lock = Lock()

    def inc(self, amount):
        with self._lock:
            self._value += amount

    def set(self, value):
        with self._lock:
            self._value = value

    def get(self):
        with self._lock:
            return self._value

def RedisValueBuilder():
    global redis_connection # type: aioredis.RedisConnection
    redis = aioredis.Redis(redis_connection)

    class RedisValue(object):
        """A float stored in a Redis."""

        def __init__(self, typ, metric_name, name, labelnames, labelvalues, **kwargs):
            self._redis_key = f'{name}:{",".join(label + "=" + labelvalues[i] for i, label in enumerate(labelnames))}'

        async def inc(self, amount):
            await redis.incrby(self._redis_key, amount)

        async def set(self, value):
            await redis.set(self._redis_key, value)

        async def get(self):
            return await redis.get(self._redis_key) or 0.0

    return RedisValue

def MultiProcessValue(process_identifier=os.getpid):
    """Returns a MmapedValue class based on a process_identifier function.

    The 'process_identifier' function MUST comply with this simple rule:
    when called in simultaneously running processes it MUST return distinct values.

    Using a different function than the default 'os.getpid' is at your own risk.
    """
    files = {}
    values = []
    pid = {'value': process_identifier()}
    # Use a single global lock when in multi-processing mode
    # as we presume this means there is no threading going on.
    # This avoids the need to also have mutexes in __MmapDict.
    lock = Lock()

    class MmapedValue(object):
        """A float protected by a mutex backed by a per-process mmaped file."""

        _multiprocess = True

        def __init__(self, typ, metric_name, name, labelnames, labelvalues, multiprocess_mode='', **kwargs):
            self._params = typ, metric_name, name, labelnames, labelvalues, multiprocess_mode
            with lock:
                self.__check_for_pid_change()
                self.__reset()
                values.append(self)

        def __reset(self):
            typ, metric_name, name, labelnames, labelvalues, multiprocess_mode = self._params
            if typ == 'gauge':
                file_prefix = typ + '_' + multiprocess_mode
            else:
                file_prefix = typ
            if file_prefix not in files:
                filename = os.path.join(
                    os.environ['prometheus_multiproc_dir'],
                    '{0}_{1}.db'.format(file_prefix, pid['value']))

                files[file_prefix] = MmapedDict(filename)
            self._file = files[file_prefix]
            self._key = mmap_key(metric_name, name, labelnames, labelvalues)
            self._value = self._file.read_value(self._key)

        def __check_for_pid_change(self):
            actual_pid = process_identifier()
            if pid['value'] != actual_pid:
                pid['value'] = actual_pid
                # There has been a fork(), reset all the values.
                for f in files.values():
                    f.close()
                files.clear()
                for value in values:
                    value.__reset()

        def inc(self, amount):
            with lock:
                self.__check_for_pid_change()
                self._value += amount
                self._file.write_value(self._key, self._value)

        def set(self, value):
            with lock:
                self.__check_for_pid_change()
                self._value = value
                self._file.write_value(self._key, self._value)

        def get(self):
            with lock:
                self.__check_for_pid_change()
                return self._value

    return MmapedValue

redis_connection = None # type: aioredis.RedisConnection

def get_value_class():
    global redis_connection
    # Should we enable multi-process mode?
    # This needs to be chosen before the first metric is constructed,
    # and as that may be in some arbitrary library the user/admin has
    # no control over we use an environment variable.
    if redis_connection is not None:
        return RedisValueBuilder()
    elif 'prometheus_multiproc_dir' in os.environ:
        return MultiProcessValue()
    else:
        return MutexValue


ValueClass = get_value_class()
