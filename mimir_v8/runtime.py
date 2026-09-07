"""Production runtime wiring and continuous projector supervision for Mímir v8."""

from __future__ import annotations

import contextlib
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .api import ServiceContext, create_app
from .auth import TokenStore
from .candidates import CandidateService
from .core_memory import CoreMemoryProjector, CoreMemoryService
from .graph_projector import GraphProjector
from .learning import LearningService
from .knowledge import LAYERS, FeedbackLoop, KnowledgeService, UnifiedSearch
from .extraction import ExtractionService
from .retention import RetentionService
from .projector import FTSProjector, ProjectorRunner
from .schema import SCHEMA_VERSION
from .query import QueryKernel
from .blackboard import BlackboardService
from .relevance import ProactiveWake
from .store import CanonicalStore
from .vector_projector import VectorProjectionError, VectorProjector, validate_vector_collection_name


class RuntimeConfigurationError(RuntimeError):
    """Raised when production runtime wiring is incomplete or unsafe."""


class LockedEmbedder:
    def __init__(self, model):
        self.model = model
        self._lock = threading.RLock()

    def __call__(self, text: str):
        with self._lock:
            return self.model.encode(text, normalize_embeddings=True).tolist()


@dataclass
class RuntimeComponents:
    store: CanonicalStore
    fts: FTSProjector
    graph: GraphProjector
    core_memory: CoreMemoryProjector
    vector: VectorProjector | None
    runners: tuple[ProjectorRunner, ...]
    supervisor: "ProjectorSupervisor"


class ProjectorSupervisor:
    def __init__(
        self,
        runners: tuple[ProjectorRunner, ...],
        *,
        interval_seconds: float = 0.25,
        error_hook: Callable[[str, BaseException], None] | None = None,
    ):
        self.runners = runners
        self.interval_seconds = interval_seconds
        # P45: 投影器单轮异常的观测钩子（不抛出即不致死；记录由接线方决定）
        self.error_hook = error_hook
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def drain_once(self, limit: int = 100) -> dict:
        results = {}
        for runner in self.runners:
            try:
                results[runner.projector.name] = runner.run_once(limit=limit)
            except Exception as exc:  # noqa: BLE001
                # P45 (2026-09-07): 生产事故修复——迁移重启窗口的 sqlite 锁竞争
                # 曾使任一 runner 抛异常即杀死整个 supervisor 线程（_run 无保护），
                # outbox 永久积压 /ready 恒 503，飞书巡检 4 天 3392 次误报无人知。
                # 单轮失败降级为该投影器本轮 0 进度 + failed 计数，循环继续。
                if self.error_hook:
                    try:
                        self.error_hook(runner.projector.name, exc)
                    except Exception:
                        pass
                results[runner.projector.name] = {"processed": 0, "failed": 1}
        return results

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mimir-v8-projectors", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        if self._thread and self._thread.is_alive():
            raise RuntimeError("projector supervisor did not stop")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = 0
                failed = 0
                for result in self.drain_once().values():
                    processed += result["processed"]
                    failed += result["failed"]
                delay = self.interval_seconds if processed == 0 or failed else 0.01
            except Exception as exc:  # noqa: BLE001
                # P45: 兜底层——drain_once 之外的任何异常（含极端）也不得杀死线程。
                # 此前一次 sqlite 锁异常即静默死亡（生产 2026-09-03 起永久积压）。
                if self.error_hook:
                    try:
                        self.error_hook("supervisor", exc)
                    except Exception:
                        pass
                delay = self.interval_seconds
            self._stop.wait(delay)


def _build_vector(root: Path, collection_name: str, model_name: str):
    try:
        projection = validate_vector_collection_name(collection_name)
    except VectorProjectionError as exc:
        raise RuntimeConfigurationError(str(exc)) from exc
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    try:
        import chromadb
        from chromadb.config import Settings
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeConfigurationError("vector runtime dependencies are unavailable") from exc
    client = chromadb.PersistentClient(
        path=str(root / "chroma"), settings=Settings(anonymized_telemetry=False)
    )
    collection = client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine", "mimir_schema": str(SCHEMA_VERSION), "projection": projection},
    )
    embedder = LockedEmbedder(
        SentenceTransformer(model_name, device="cpu", local_files_only=True)
    )
    return VectorProjector(collection, embedder, collection_name=collection_name), collection, embedder


def normalize_knowledge_layers(layers: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(layers, tuple) or not layers:
        raise RuntimeConfigurationError("at least one knowledge layer must be enabled")
    normalized: list[str] = []
    for raw in layers:
        layer = raw.strip().lower() if isinstance(raw, str) else ""
        if layer not in LAYERS:
            raise RuntimeConfigurationError(f"invalid knowledge layer: {raw!r}")
        if layer not in normalized:
            normalized.append(layer)
    return tuple(layer for layer in ("memory", "learning", "wiki") if layer in normalized)


def build_runtime(
    data_dir: str | Path,
    token_file: str | Path,
    *,
    vector_enabled: bool = True,
    collection_name: str = "mimir_v8_prod_facts",
    model_name: str = "BAAI/bge-m3",
    start_supervisor: bool = True,
    enabled_knowledge_layers: tuple[str, ...] = ("memory", "learning", "wiki"),
):
    enabled_knowledge_layers = normalize_knowledge_layers(enabled_knowledge_layers)
    from .config import load_federation_registry
    load_federation_registry(None)
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    store = CanonicalStore(root / "canonical.db")
    fts = FTSProjector(root / "fts.db")
    graph = GraphProjector(store, root / "graph.db")
    core_memory = CoreMemoryProjector(store, root / "core_memory.db")
    vector_projector = vector_collection = embedder = None
    if vector_enabled:
        vector_projector, vector_collection, embedder = _build_vector(
            root, collection_name, model_name
        )
    projectors = [fts, graph, core_memory]
    if vector_projector is not None:
        projectors.insert(0, vector_projector)
    runners = tuple(ProjectorRunner(store, projector) for projector in projectors)
    supervisor = ProjectorSupervisor(runners)
    query = QueryKernel(
        store, vector=vector_collection, fts=fts, graph=graph, embedder=embedder
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        if start_supervisor:
            supervisor.start()
        try:
            yield
        finally:
            if start_supervisor:
                supervisor.stop()

    learning = LearningService(store)
    knowledge = KnowledgeService(store)
    unified_search = UnifiedSearch(
        query, knowledge, enabled_layers=enabled_knowledge_layers
    )
    feedback_loop = FeedbackLoop(store, knowledge)
    blackboard = BlackboardService(store, root / "blackboard.db")
    # v13 wiring gap fix: /v13/wake used to 503 forever because runtime never
    # constructed ProactiveWake (tests passed wake= manually, masking this).
    wake = ProactiveWake(store)
    app = create_app(
        ServiceContext(
            store=store,
            token_store=TokenStore(token_file),
            query=query,
            core_memory=core_memory,
            candidates=learning.candidates,
            learning=learning,
            extraction=ExtractionService(store),
            retention=RetentionService(store),
            core_memory_service=CoreMemoryService(store),
            knowledge=knowledge,
            unified_search=unified_search,
            feedback_loop=feedback_loop,
            blackboard=blackboard,
            wake=wake,
            graph=graph,
        ),
        lifespan=lifespan,
    )
    components = RuntimeComponents(
        store=store, fts=fts, graph=graph, core_memory=core_memory,
        vector=vector_projector, runners=runners, supervisor=supervisor,
    )
    app.state.runtime = components
    return app, components
