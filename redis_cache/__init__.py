import logging
from base64 import b64encode
from collections import defaultdict
from functools import wraps
from json import dumps, loads

logger = logging.getLogger(__package__)


def get_cache_lua_fn(client):
    if not hasattr(client, '_lua_cache_fn'):
        client._lua_cache_fn = client.register_script(
            """
local ttl = tonumber(ARGV[2])
local value
if ttl > 0 then
  value = redis.call('SETEX', KEYS[1], ttl, ARGV[1])
else
  value = redis.call('SET', KEYS[1], ARGV[1])
end
local limit = tonumber(ARGV[3])
if limit > 0 then
  local time_parts = redis.call('TIME')
  local time = tonumber(time_parts[1] .. '.' .. time_parts[2])
  redis.call('ZADD', KEYS[2], time, KEYS[1])
  local count = tonumber(redis.call('ZCOUNT', KEYS[2], '-inf', '+inf'))
  local over = count - limit
  if over > 0 then
    local stale_keys_and_scores = redis.call('ZPOPMIN', KEYS[2], over)
    -- Remove the the scores and just leave the keys
    local stale_keys = {}
    for i = 1, #stale_keys_and_scores, 2 do
      stale_keys[#stale_keys+1] = stale_keys_and_scores[i]
    end
    redis.call('ZREM', KEYS[2], unpack(stale_keys))
    redis.call('DEL', unpack(stale_keys))
  end
end
return value
"""
        )
    return client._lua_cache_fn


# Utility function to batch keys
def chunks(iterable, n):
    """Yield successive n-sized chunks from iterator."""
    _iterable = iter(iterable)
    while True:
        elements = []
        for _ in range(n):
            try:
                elements.append(next(_iterable))
            except StopIteration:
                break

        if not len(elements):
            break

        yield elements


class RedisCache:
    def __init__(
        self, redis_client, prefix='rc', serializer=dumps, deserializer=loads, key_serializer=None,
    ):
        self.client = redis_client
        self.prefix = prefix
        self.serializer = serializer
        self.deserializer = deserializer
        self.key_serializer = key_serializer

    def cache(self, ttl=0, limit=0, namespace=None, support_batch_call=False):
        return CacheDecorator(
            redis_client=self.client,
            prefix=self.prefix,
            serializer=self.serializer,
            deserializer=self.deserializer,
            key_serializer=self.key_serializer,
            ttl=ttl,
            limit=limit,
            namespace=namespace,
            support_batch_call=support_batch_call,
        )

    def mget(self, *fns_with_args):
        keys = []
        for fn_and_args in fns_with_args:
            fn = fn_and_args['fn']
            args = fn_and_args.setdefault('args', [])
            kwargs = fn_and_args.setdefault('kwargs', {})

            keys.append(fn.instance.get_key(args=args, kwargs=kwargs))

        results = self.client.mget(*keys)
        pipeline = self.client.pipeline()

        deserialized_results = {}
        cache_miss_detected = False
        args_by_fn = defaultdict(list)
        # Store cache hit results and group cache misses by fn
        for i, result in enumerate(results):
            if result is None:
                cache_miss_detected = True

                fn_and_args = fns_with_args[i]
                fn = fn_and_args['fn']

                fn_and_args['call_idx'] = i
                args_by_fn[fn].append(fn_and_args)
            else:
                result = self.deserializer(result)
                deserialized_results[i] = result

        if cache_miss_detected:
            for fn, args_batch in args_by_fn.items():
                batch_result = self._get_batch_call_result(fn, args_batch)

                for fn_and_args, result in zip(args_batch, batch_result):
                    call_idx = fn_and_args['call_idx']

                    # populate cache with freshly calculated values
                    result_serialized = self.serializer(result)
                    get_cache_lua_fn(self.client)(
                        keys=[keys[call_idx], fn.instance.keys_key],
                        args=[result_serialized, fn.instance.ttl, fn.instance.limit],
                        client=pipeline,
                    )
                    # insert them in results in order they were requested
                    deserialized_results[call_idx] = result
            pipeline.execute()

        return [deserialized_results[key] for key in sorted(deserialized_results.keys())]

    @staticmethod
    def _get_batch_call_result(fn, args_batch):
        original_fn = fn.instance.original_fn

        if fn.instance.support_batch_call:
            call_vector = [(fn_and_args['args'] or fn_and_args['kwargs']) for fn_and_args in args_batch]
            batch_result = original_fn(call_vector)
        else:
            batch_result = [original_fn(*fn_and_args['args'], **fn_and_args['kwargs']) for fn_and_args in args_batch]

        return batch_result


class CacheDecorator:
    def __init__(
        self,
        redis_client,
        prefix='rc',
        serializer=dumps,
        deserializer=loads,
        key_serializer=None,
        ttl=0,
        limit=0,
        namespace=None,
        support_batch_call=False,
    ):
        self.client = redis_client
        self.prefix = prefix
        self.serializer = serializer
        self.key_serializer = key_serializer
        self.deserializer = deserializer
        self.ttl = ttl
        self.limit = limit
        self.namespace = namespace
        self.keys_key = None
        self.support_batch_call = support_batch_call

    def get_key(self, args, kwargs):
        if self.key_serializer:
            serialized_data = self.key_serializer(args, kwargs)
        else:
            serialized_data = self.serializer([args, kwargs])

        if not isinstance(serialized_data, str):
            serialized_data = str(b64encode(serialized_data), 'utf-8')
        return f'{self.prefix}:{self.namespace}:{serialized_data}'

    def __call__(self, fn):
        self.namespace = self.namespace if self.namespace else f'{fn.__module__}.{fn.__name__}'
        self.keys_key = f'{self.prefix}:{self.namespace}:keys'
        self.original_fn = fn

        @wraps(fn)
        def inner(*args, **kwargs):
            nonlocal self
            if self.support_batch_call:
                logger.warning('To use caching for batch-mode decorated functions, use RedisCache.mget()')
                return fn(*args, **kwargs)
                # effectively disable caching, otherwise we would need to re-implement much of
                # the mget() logic covering filtering only args for which we encountered cache misses

            key = self.get_key(args, kwargs)
            result = self.client.get(key)
            if not result:
                result = fn(*args, **kwargs)
                result_serialized = self.serializer(result)
                get_cache_lua_fn(self.client)(
                    keys=[key, self.keys_key], args=[result_serialized, self.ttl, self.limit],
                )
            else:
                result = self.deserializer(result)
            return result

        inner.invalidate = self.invalidate
        inner.invalidate_all = self.invalidate_all
        inner.instance = self
        return inner

    def invalidate(self, *args, **kwargs):
        key = self.get_key(args, kwargs)
        pipe = self.client.pipeline()
        pipe.delete(key)
        pipe.zrem(self.keys_key, key)
        pipe.execute()

    def invalidate_all(self, *args, **kwargs):
        chunks_gen = chunks(self.client.scan_iter(f'{self.prefix}:{self.namespace}:*'), 500)
        for keys in chunks_gen:
            self.client.delete(*keys)
