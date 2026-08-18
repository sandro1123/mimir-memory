"""Mímir v8.2 canonical memory package with LLM evaluation and content collection."""

from .auth import AuthError, Principal, TokenStore
from .candidates import CandidatePolicyError, CandidateService, CreateCandidate, ReviewCandidate
from .core_memory import (
    CoreMemoryPolicyError,
    CoreMemoryProjector,
    CoreMemoryService,
    PromoteCoreMemory,
    RetireCoreMemory,
)
from .graph_projector import GraphProjector
from .connectors import ConnectorError, HermesStateCDC
from .extraction import EvidenceInput, ExtractionService
from .retention import RetentionSchedule, RetentionService
from .trust import TrustManager, TrustScore, SIGNAL_WEIGHTS
from .learning import (
    ConversationEnvelope,
    ConversationMessage,
    LearningService,
    RedactionResult,
    redact_text,
)
from .migration import MigrationError, MigrationReport, V7Importer
from .projector import FTSProjector, ProjectionError, ProjectorRunner
from .query import QueryKernel, QueryRequest
from .publication import (
    PublicationPolicyError,
    PublicationService,
    RegisterDocument,
    RequestPublication,
    human_content_hash,
    replace_managed_sections,
)
from .schema import (
    CreateFact,
    GrantFactAccess,
    MIMIR_VERSION,
    SCHEMA_VERSION,
    TombstoneFact,
    UpdateFact,
    ValidationError,
)
from .store import CanonicalStore, ConflictError, NotFoundError
from .vector_projector import VectorProjectionError, VectorProjector

# v8.2 modules
from .evaluator import Evaluator, EvaluationResult, PolicyDecision, EVALUATOR_VERSION
from .review import ReviewQueue, ReviewItem, ReviewQueueSummary
from .reporting import DailyReport, ReportGenerator, DeepReader, ReadingResult
from .collectors import BaseCollector, RSSCollector, WebCollector

__all__ = [
    "AuthError",
    "BaseCollector",
    "CandidatePolicyError",
    "CandidateService",
    "CanonicalStore",
    "ConflictError",
    "ConnectorError",
    "ConversationEnvelope",
    "ConversationMessage",
    "CoreMemoryPolicyError",
    "CoreMemoryProjector",
    "CoreMemoryService",
    "CreateCandidate",
    "CreateFact",
    "DailyReport",
    "DeepReader",
    "EVALUATOR_VERSION",
    "EvaluationResult",
    "Evaluator",
    "EvidenceInput",
    "ExtractionService",
    "FTSProjector",
    "GraphProjector",
    "GrantFactAccess",
    "HermesStateCDC",
    "LearningService",
    "MIMIR_VERSION",
    "MigrationError",
    "MigrationReport",
    "NotFoundError",
    "PolicyDecision",
    "Principal",
    "ProjectionError",
    "ProjectorRunner",
    "PromoteCoreMemory",
    "PublicationPolicyError",
    "PublicationService",
    "QueryKernel",
    "QueryRequest",
    "RSSCollector",
    "ReadingResult",
    "RedactionResult",
    "RegisterDocument",
    "ReportGenerator",
    "RequestPublication",
    "RetentionSchedule",
    "RetentionService",
    "TrustManager",
    "TrustScore",
    "SIGNAL_WEIGHTS",
    "RetireCoreMemory",
    "ReviewCandidate",
    "ReviewItem",
    "ReviewQueue",
    "ReviewQueueSummary",
    "SCHEMA_VERSION",
    "TokenStore",
    "TombstoneFact",
    "UpdateFact",
    "V7Importer",
    "ValidationError",
    "VectorProjectionError",
    "VectorProjector",
    "WebCollector",
    "human_content_hash",
    "replace_managed_sections",
    "redact_text",
]
