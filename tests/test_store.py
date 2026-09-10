"""세션 · 작업 보관소.

Redis가 있으면 워커 여러 개가 같은 세션을 본다. 없으면 프로세스 메모리로
떨어져 예전과 같이 동작한다.

**없다고 서버가 못 뜨면 안 된다.** 더미 서버는 백엔드가 docker run 한 줄로
띄우는 것이 약속이고, 테스트도 Redis 서버 없이 돌아야 한다.
"""
import pytest

from ai import redis_store
from ai.redis_store import RedisCounter, RedisDict, _MemoryClient


@pytest.fixture(autouse=True)
def _fresh_client(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    redis_store.reset_client()
    yield
    redis_store.reset_client()


# ---------------------------------------------------------------------------
# Redis가 없을 때
# ---------------------------------------------------------------------------


def test_REDIS_URL이_없으면_메모리로_떨어진다():
    assert redis_store.using_redis() is False


def test_붙지_못하면_예외를_올리지_않는다(monkeypatch):
    """서버가 아직 안 떴을 수 있다. 여기서 죽으면 더미 서버 자체가 못 뜬다."""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:65530/0")
    redis_store.reset_client()

    assert redis_store.using_redis() is False   # 조용히 메모리로


def test_서버가_없어도_기동한다(monkeypatch):
    import importlib
    import sys

    monkeypatch.setenv("AI_MODE", "dummy")
    monkeypatch.delenv("REDIS_URL", raising=False)
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    assert main.app is not None


# ---------------------------------------------------------------------------
# dict처럼 동작하는가 — 호출부가 이걸 기대한다
# ---------------------------------------------------------------------------


def test_dict처럼_읽고_쓴다():
    store = RedisDict("t_basic")
    store.clear()

    store["a"] = "1"
    assert store["a"] == "1"
    assert store.get("a") == "1"
    assert store.get("없음") is None
    assert "a" in store
    assert "없음" not in store
    assert len(store) == 1


def test_삽입_순서를_지킨다():
    """evict_oldest가 next(iter(store))로 가장 오래된 것을 지운다."""
    store = RedisDict("t_order")
    store.clear()

    for key in ("first", "second", "third"):
        store[key] = key
    assert list(store) == ["first", "second", "third"]

    store.pop("first")
    assert list(store) == ["second", "third"]


def test_같은_키를_다시_넣어도_순서가_안_늘어난다():
    store = RedisDict("t_dup")
    store.clear()

    store["a"] = "1"
    store["a"] = "2"
    assert len(store) == 1
    assert store["a"] == "2"


def test_없는_키를_pop하면_기본값():
    store = RedisDict("t_pop")
    store.clear()
    assert store.pop("없음", "기본") == "기본"
    with pytest.raises(KeyError):
        store.pop("없음")


# ---------------------------------------------------------------------------
# 메서드를 부르면 저장된다 — 폴링이 여기 걸려 있다
# ---------------------------------------------------------------------------


class _Counter:
    def __init__(self):
        self.n = 0

    def poll(self):
        self.n += 1
        return self.n


def test_메서드를_부르면_변형이_남는다():
    """PendingTask.poll()이 내부 상태를 바꾼다. 안 남으면 영원히 processing이다."""
    store = RedisDict("t_mutate")
    store.clear()
    store["k"] = _Counter()

    assert store["k"].poll() == 1
    assert store["k"].poll() == 2
    assert store["k"].poll() == 3


def test_pickle이_안_되는_값도_받는다():
    """BackgroundTask는 threading.Lock을 들고 있어 직렬화가 안 된다."""
    import threading

    store = RedisDict("t_lock")
    store.clear()

    value = threading.Lock()
    store["k"] = value
    assert store["k"] is value
    assert "k" in store


def test_카운터는_1부터_올라간다():
    counter = RedisCounter("t_seq")
    first = counter.next()
    assert counter.next() == first + 1
