"""Database and cache configuration."""
import os

class MetaRedisConfig(type):
    """Redis connection settings."""
    
    @property
    def HOST(cls):
        return os.getenv('REDIS_HOST', 'localhost')
    
    @property
    def PORT(cls):
        return int(os.getenv('REDIS_PORT', 6379))
    
    @property
    def PASSWORD(cls):
        return os.getenv('REDIS_PASSWORD', None)
    
    @property
    def DB(cls):
        return int(os.getenv('REDIS_DB', 0))
    
    @property
    def CONNECT_TIMEOUT(cls):
        return int(os.getenv('REDIS_CONNECT_TIMEOUT', 5))
    
    @property
    def SOCKET_TIMEOUT(cls):
        return int(os.getenv('REDIS_SOCKET_TIMEOUT', 5))
    
    @property
    def MAX_CONNECTIONS(cls):
        return int(os.getenv('REDIS_MAX_CONNECTIONS', 10))


class RedisConfig(metaclass=MetaRedisConfig):
    """Redis cache configuration."""
    
    @classmethod
    def get_url(cls) -> str:
        """Return the Redis connection URL."""
        if cls.PASSWORD:
            return f"redis://:{cls.PASSWORD}@{cls.HOST}:{cls.PORT}/{cls.DB}"
        return f"redis://{cls.HOST}:{cls.PORT}/{cls.DB}"


class MetaCacheConfig(type):
    """Business cache settings."""
    
    @property
    def ENABLED(cls):
        return os.getenv('CACHE_ENABLED', 'False').lower() == 'true'

    @property
    def DEFAULT_EXPIRE(cls):
        return int(os.getenv('CACHE_EXPIRE', 300))

    @property
    def KLINE_CACHE_TTL(cls):
        return {
            '1m': 5,       # Cache 1m K-lines for 5 seconds
            '3m': 30,      # Cache 3m K-lines for 30 seconds
            '5m': 60,      # Cache 5m K-lines for 1 minute
            '15m': 300,    # Cache 15m K-lines for 5 minutes
            '30m': 300,    # Cache 30m K-lines for 5 minutes
            '1H': 300,     # Cache 1H K-lines for 5 minutes
            '4H': 300,     # Cache 4H K-lines for 5 minutes
            '1D': 300,     # Cache 1D K-lines for 5 minutes
            '1h': 300,
            '4h': 300,
            '1d': 300,
        }

    @property
    def ANALYSIS_CACHE_TTL(cls):
        return 3600

    @property
    def PRICE_CACHE_TTL(cls):
        return 10


class CacheConfig(metaclass=MetaCacheConfig):
    """Cache configuration."""
    pass
