# -*- coding: utf-8 -*-
"""P45 投影器 supervisor 韧性：drain_once 抛异常不得杀死消费线程。

生产事故（2026-09-03 03:39 UTC）：mimir.service 迁移重启窗口的 sqlite 锁竞争
使 drain_once 内 runner.run_once 抛异常 → ProjectorSupervisor._run 无保护 →
daemon 线程静默死亡 → outbox 永久积压（pending=8）/ready 恒 503 →
mentor cron「Mímir v8 健康巡检」每 15 分钟飞书报错 4 天（3392 次）无人知。

本测试钉死两条行为：
1. drain_once 单轮异常 → 线程存活且继续循环（不退出）
2. 异常被记录（线程不死锁也不无声）：错误计数可观测
"""
import threading
import time

from mimir_v8.runtime import ProjectorSupervisor


class _BoomRunner:
    """前 N 次 run_once 抛异常，之后恢复正常。"""

    class _Proj:
        name = "boom"

    def __init__(self, failures_before_ok: int):
        self.projector = self._Proj()
        self.failures_before_ok = failures_before_ok
        self.calls = 0

    def run_once(self, limit: int = 100, failure_hook=None):
        self.calls += 1
        if self.calls <= self.failures_before_ok:
            raise RuntimeError("sqlite locked (simulated)")
        return {"processed": 1, "failed": 0}


class TestSupervisorResilience:
    def test_runner_exception_does_not_kill_thread(self):
        """异常后线程必须继续消费：BoomRunner 前 2 次炸、第 3 次成功。"""
        runner = _BoomRunner(failures_before_ok=2)
        sup = ProjectorSupervisor((runner,), interval_seconds=0.01)
        sup.start()
        try:
            deadline = time.time() + 5.0
            while runner.calls < 3 and time.time() < deadline:
                time.sleep(0.02)
            assert runner.calls >= 3, (
                f"线程死于异常：calls={runner.calls}（若 _run 无保护，第 1 次 "
                "RuntimeError 即杀死线程，calls 停在 1）"
            )
            assert sup._thread is not None and sup._thread.is_alive(), "supervisor 线程必须存活"
        finally:
            sup.stop()

    def test_stop_still_works_after_exception(self):
        """异常后 stop() 必须仍能正常停线程（不悬挂不 RuntimeError）。"""
        runner = _BoomRunner(failures_before_ok=10**9)  # 永远炸
        sup = ProjectorSupervisor((runner,), interval_seconds=0.01)
        sup.start()
        time.sleep(0.05)  # 让它炸几轮
        assert sup._thread.is_alive(), "持续异常下线程也应存活（每轮独立 try）"
        sup.stop(timeout=5.0)
        assert not (sup._thread and sup._thread.is_alive()), "stop 必须终止线程"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
