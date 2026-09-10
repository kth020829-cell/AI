"""SESSIONS/TASKS/IDEMPOTENCY 처럼 프로세스 메모리에 있던 dict를 Redis로 옮기는 유틸.

목표: 호출부(ai/pipeline.py, read_task, DummySession.answer 등)는 전혀 건드리지 않는다.
    SESSIONS: dict[str, DummySession] = {}
    TASKS: dict[str, BaseModel] = {}
를
    SESSIONS = RedisDict("sessions")
    TASKS = RedisDict("tasks")
로만 바꿔도 기존 코드가 그대로 동작해야 한다.

왜 단순 get/set으로는 안 되는가
    read_task()는 이렇게 되어 있다.

        task = TASKS.get(task_id)
        if task is not None and hasattr(task, "poll"):
            return task.poll()

    task.poll()은 객체 내부 상태(self.polled)를 변형한다. 메모리 dict에서는
    꺼낸 게 원본 객체라 변형이 바로 반영되지만, Redis에서 꺼낸 건 역직렬화된
    "복사본"이라 poll() 호출 후 다시 저장해주지 않으면 다음 폴링 때 변형이
    사라진다 (polled가 매번 0으로 리셋되어 영원히 processing에 머무름).

    DummySession.answer()/advance()도 마찬가지로 내부 상태를 변형하는 메서드다.

해결: get()이 돌려주는 객체를 RedisProxy로 감싼다. 프록시의 메서드를 호출하면
    1) 실제 객체(deserialize된 사본)의 메서드를 그대로 호출하고
    2) 호출이 끝나자마자 그 객체를 Redis에 다시 저장한다
즉 "메서드를 부르면 자동으로 저장된다". 호출부는 이 사실을 몰라도 된다.

단, Pydantic BaseModel(예: TaskDoneResponse 같은 최종 응답)은 변형되는 일이 없고,
FastAPI가 직렬화할 때 실제 BaseModel 타입이어야 하므로 프록시로 감싸지 않고
그대로 돌려준다.
"""
import logging
import os
import pickle
import threading
from typing import Any, Dict, Iterator, List, Optional

from pydantic import BaseModel

logger = logging.getLogger("cue.ai.store")

# redis 패키지도 서버도 없을 수 있다. 그때는 프로세스 메모리로 떨어진다.
#
#   있으면    워커 여러 개가 같은 세션을 본다
#   없으면    워커 하나 안에서만 유효하다. 예전과 같은 동작이다
#
# 더미 서버는 백엔드가 docker run 한 줄로 띄우는 것이 약속이라, Redis를
# 띄우지 않았다고 서버가 못 뜨면 안 된다. 테스트도 마찬가지다.
try:
    import redis as _redis
except ImportError:
    _redis = None

_REDIS_URL = os.environ.get("REDIS_URL", "")
_client = None


class _MemoryClient:
    """redis_store가 쓰는 열 개 명령만 흉내내는 프로세스 안 저장소.

    Redis가 없을 때 이것으로 떨어진다. 인터페이스가 같아서 RedisDict와
    RedisCounter는 자기가 무엇을 쓰는지 몰라도 된다.
    """

    def __init__(self):
        self._kv: Dict[str, Any] = {}
        self._lists: Dict[str, List[str]] = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            return self._kv.get(key)

    def set(self, key, value):
        with self._lock:
            self._kv[key] = value

    def setnx(self, key, value):
        with self._lock:
            if key in self._kv:
                return False
            self._kv[key] = value
            return True

    def incr(self, key):
        with self._lock:
            value = int(self._kv.get(key, 0)) + 1
            self._kv[key] = value
            return value

    def delete(self, *keys):
        with self._lock:
            for key in keys:
                self._kv.pop(key, None)
                self._lists.pop(key, None)

    def exists(self, key):
        with self._lock:
            return 1 if key in self._kv else 0

    def rpush(self, key, value):
        with self._lock:
            self._lists.setdefault(key, []).append(value)

    def lrem(self, key, count, value):
        with self._lock:
            items = self._lists.get(key)
            if items and value in items:
                items.remove(value)

    def llen(self, key):
        with self._lock:
            return len(self._lists.get(key, []))

    def lrange(self, key, start, end):
        with self._lock:
            items = list(self._lists.get(key, []))
        return items if end == -1 else items[start : end + 1]


def using_redis() -> bool:
    """지금 Redis를 쓰고 있는가. 워커를 늘려도 되는지가 이 값에 달려 있다."""
    return not isinstance(_get_client(), _MemoryClient)

# Redis에 못 넣는 값(예: ai/tasks.py의 BackgroundTask — threading.Lock을 들고 있어
# pickle 자체가 안 됨)을 저장할 때 대신 넣어두는 표식.
_LOCAL_FALLBACK_MARKER = b"__LOCAL_FALLBACK__"

# 이 타입들은 메서드 호출 후 재저장이 필요 없는 "그냥 값"이라 프록시로 감싸지 않는다.
# 감싸면 예: IDEMPOTENCY[key] (문자열)가 RedisProxy가 되어 pydantic 검증이 깨진다.
_PLAIN_TYPES = (str, int, float, bool, bytes, type(None), list, dict, tuple, set)


def _get_client():
    """Redis에 붙는다. 못 붙으면 메모리로 떨어지고 경고만 남긴다.

    REDIS_URL이 비어 있으면 아예 시도하지 않는다. 로컬 개발과 테스트에서
    없는 서버에 붙으려다 매번 몇 초씩 기다리는 일을 막기 위해서다.
    """
    global _client
    if _client is not None:
        return _client

    url = os.environ.get("REDIS_URL", "").strip()
    if not url or _redis is None:
        if url and _redis is None:
            logger.warning("REDIS_URL이 있지만 redis 패키지가 없습니다. 메모리로 진행합니다")
        _client = _MemoryClient()
        return _client

    try:
        candidate = _redis.from_url(url, socket_connect_timeout=2)
        candidate.ping()
    except Exception as e:
        # 서버가 아직 안 떴을 수 있다. 여기서 죽으면 더미 서버 자체가 못 뜬다.
        logger.warning("Redis에 붙지 못했습니다 (%s). 메모리로 진행합니다", type(e).__name__)
        _client = _MemoryClient()
        return _client

    logger.info("세션 · 작업 보관소로 Redis를 씁니다")
    _client = candidate
    return _client


def reset_client() -> None:
    """다음 호출 때 다시 붙는다. 테스트에서 REDIS_URL을 바꿀 때 쓴다."""
    global _client
    _client = None


class RedisProxy:
    """역직렬화된 객체를 감싸서, 메서드 호출 후 자동으로 Redis에 재저장한다."""

    def __init__(self, store: "RedisDict", key: str, obj: Any):
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_key", key)
        object.__setattr__(self, "_obj", obj)

    def __getattr__(self, name: str):
        attr = getattr(object.__getattribute__(self, "_obj"), name)
        if callable(attr):
            def wrapped(*args, **kwargs):
                result = attr(*args, **kwargs)
                store = object.__getattribute__(self, "_store")
                key = object.__getattribute__(self, "_key")
                obj = object.__getattribute__(self, "_obj")
                store._raw_set(key, obj)  # 변형된 객체를 다시 저장
                return result
            return wrapped
        return attr

    def __repr__(self):
        return repr(object.__getattribute__(self, "_obj"))


class RedisDict:
    """SESSIONS/TASKS/IDEMPOTENCY가 기대하는 dict 인터페이스를 Redis로 구현.

    - 값은 pickle로 직렬화 (DummySession, PendingTask 같은 일반 객체도 저장 가능)
    - 삽입 순서를 별도 Redis List로 관리해 evict_oldest의 FIFO 삭제(next(iter(store)))를 지원
    - get()/[]가 돌려주는 객체는, BaseModel이 아니면 RedisProxy로 감싸서
      메서드 호출 시 자동 재저장되게 한다
    """

    def __init__(self, namespace: str, client=None):
        self._ns = namespace
        self._client = client or _get_client()
        self._order_key = f"{namespace}:_order"
        # pickle이 안 되는 값(BackgroundTask 등)을 이 워커 프로세스에만 보관.
        # 다른 워커가 같은 key를 조회하면 여기 없으니 None이 나가는데,
        # 이건 그 값이 애초에 Redis로 못 옮겨진 것이라 오늘 이미 있던 제약과 같다
        # (그 값을 만든 워커가 아니면 어차피 못 쓰던 것들 — BackgroundTask가 대표적).
        self._local_fallback: Dict[str, Any] = {}

    def _hkey(self, key: str) -> str:
        return f"{self._ns}:{key}"

    def _raw_set(self, key: str, value: Any) -> None:
        """가능하면 Redis에, pickle이 안 되면 로컬 폴백에 저장한다.

        _raw_set은 RedisProxy가 메서드 호출 후 재저장할 때도 쓰인다.
        이미 로컬 폴백에 있던 값이 다시 pickle 시도해서 실패하면 계속 로컬에 남는다.
        """
        try:
            payload = pickle.dumps(value)
        except (TypeError, pickle.PicklingError):
            self._local_fallback[key] = value
            self._client.set(self._hkey(key), _LOCAL_FALLBACK_MARKER)
            return
        self._local_fallback.pop(key, None)
        self._client.set(self._hkey(key), payload)

    def __setitem__(self, key: str, value: Any) -> None:
        is_new = self._client.exists(self._hkey(key)) == 0
        self._raw_set(key, value)
        if is_new:
            self._client.rpush(self._order_key, key)

    def get(self, key: str, default=None):
        raw = self._client.get(self._hkey(key))
        if raw is None:
            return default
        if raw == _LOCAL_FALLBACK_MARKER:
            # 이 워커가 만든 값이면 여기 있고, 다른 워커면 없어서 default가 나간다.
            # BackgroundTask처럼 원래도 그 워커 안에서만 유효했던 값이라 오늘과 동일.
            return self._local_fallback.get(key, default)
        obj = pickle.loads(raw)
        if isinstance(obj, BaseModel) or isinstance(obj, _PLAIN_TYPES):
            # 불변 응답 모델이나 str/int 같은 기본값은 감쌀 필요 없음 -
            # 그대로 돌려줘야 pydantic 검증이나 타입 체크가 깨지지 않는다
            return obj
        return RedisProxy(self, key, obj)

    def __getitem__(self, key: str):
        val = self.get(key)
        if val is None:
            raise KeyError(key)
        return val

    def __contains__(self, key: str) -> bool:
        return self._client.exists(self._hkey(key)) == 1

    def pop(self, key: str, *default):
        raw = self._client.get(self._hkey(key))
        self._client.delete(self._hkey(key))
        self._client.lrem(self._order_key, 0, key)
        local_val = self._local_fallback.pop(key, None)
        if raw is None:
            if default:
                return default[0]
            raise KeyError(key)
        if raw == _LOCAL_FALLBACK_MARKER:
            if local_val is not None:
                return local_val
            if default:
                return default[0]
            raise KeyError(key)
        return pickle.loads(raw)

    def __len__(self) -> int:
        return self._client.llen(self._order_key)

    def __iter__(self) -> Iterator[str]:
        keys = self._client.lrange(self._order_key, 0, -1)
        return iter(k.decode("utf-8") if isinstance(k, bytes) else k for k in keys)

    def clear(self) -> None:
        keys = self._client.lrange(self._order_key, 0, -1)
        if keys:
            self._client.delete(*[self._hkey(k.decode("utf-8") if isinstance(k, bytes) else k) for k in keys])
        self._client.delete(self._order_key)
        self._local_fallback.clear()


class RedisCounter:
    """itertools.count(1)를 대체. INCR로 워커 간 원자적으로 증가한다.

    _TASK_SEQ = itertools.count(1)
    next(_TASK_SEQ)
    를
    _TASK_SEQ = RedisCounter("task_seq")
    _TASK_SEQ.next()
    로 바꾸면 된다. 이것도 안 바꾸면 워커마다 카운터가 따로 놀아서
    서로 다른 워커가 같은 task_id를 만들어 충돌한다.
    """

    def __init__(self, name: str, start: int = 0, client=None):
        self._key = f"counter:{name}"
        self._client = client or _get_client()
        self._client.setnx(self._key, start)

    def next(self) -> int:
        return self._client.incr(self._key)