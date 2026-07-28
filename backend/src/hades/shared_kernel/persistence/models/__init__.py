"""ORM persistence models — the full PostgreSQL schema.

Importing this package registers every table on ``Base.metadata`` so Alembic's
autogenerate (and the baseline migration's ``create_all``) sees the complete
schema. The design is intentionally comprehensive from the start — it must
support years of operation without improvised tables.

Every table carries a UUIDv7 primary key and ``created_at`` / ``updated_at``
timestamps (via the shared mixins), plus explicit indices and foreign keys for
referential integrity.
"""

from __future__ import annotations

from hades.shared_kernel.persistence.models.event_store import (
    DomainEventRecord,
    EventSnapshot,
)
from hades.shared_kernel.persistence.models.intelligence import (
    IntelClusterRecord,
    WalletHistoryRecord,
    WalletKnowledgeRecord,
    WalletProfileRecord,
    WalletRelationshipRecord,
    WalletTimelineRecord,
)
from hades.shared_kernel.persistence.models.knowledge import (
    KnowledgeDecisionRecord,
    KnowledgeLessonRecord,
    KnowledgeObservationRecord,
)
from hades.shared_kernel.persistence.models.learning import (
    CommitteeDatasetRecord,
    CommitteeDriftRecord,
    CommitteeFeatureImportanceRecord,
    CommitteeModelRecord,
    CommitteeOutcomeRecord,
    CommitteePredictionRecord,
)
from hades.shared_kernel.persistence.models.market import Feature, MarketData
from hades.shared_kernel.persistence.models.ops import (
    AuditLog,
    ConfigSnapshot,
    HealthCheck,
    Notification,
    RiskEvent,
    SystemConfiguration,
)
from hades.shared_kernel.persistence.models.portfolio import (
    EquityCurve,
    PnLHistory,
    PortfolioHistory,
    PortfolioStateRecord,
)
from hades.shared_kernel.persistence.models.research import (
    AcceptedOpportunity,
    Experiment,
    RejectedOpportunity,
    ResearchBacktestRecord,
    ResearchCandidateRecord,
    ResearchExperimentRecord,
    ResearchHypothesisRecord,
    ResearchJob,
    ResearchKnowledgeRecord,
    ResearchPromotionRecord,
    ResearchReportRecord,
    ResearchShadowRecord,
)
from hades.shared_kernel.persistence.models.risk import (
    RiskControlStateRecord,
    RiskDecisionRecord,
)
from hades.shared_kernel.persistence.models.scanner import (
    DataAnomaly,
    TokenMetadataRecord,
    TokenSnapshot,
)
from hades.shared_kernel.persistence.models.security import (
    BlacklistEntry,
    DeveloperReputation,
    SecurityAssessmentRecord,
    WalletClusterRecord,
    WhitelistEntry,
)
from hades.shared_kernel.persistence.models.signals import Signal
from hades.shared_kernel.persistence.models.strategies import Model, Strategy, StrategyVersion
from hades.shared_kernel.persistence.models.tokens import Token, Wallet
from hades.shared_kernel.persistence.models.trades import (
    LiveTrade,
    PaperTrade,
    Position,
    Trade,
)

__all__ = [
    "AcceptedOpportunity",
    "AuditLog",
    "BlacklistEntry",
    "CommitteeDatasetRecord",
    "CommitteeDriftRecord",
    "CommitteeFeatureImportanceRecord",
    "CommitteeModelRecord",
    "CommitteeOutcomeRecord",
    "CommitteePredictionRecord",
    "ConfigSnapshot",
    "DataAnomaly",
    "DeveloperReputation",
    "DomainEventRecord",
    "EquityCurve",
    "EventSnapshot",
    "Experiment",
    "Feature",
    "HealthCheck",
    "IntelClusterRecord",
    "KnowledgeDecisionRecord",
    "KnowledgeLessonRecord",
    "KnowledgeObservationRecord",
    "LiveTrade",
    "MarketData",
    "Model",
    "Notification",
    "PaperTrade",
    "PnLHistory",
    "PortfolioHistory",
    "PortfolioStateRecord",
    "Position",
    "RejectedOpportunity",
    "ResearchBacktestRecord",
    "ResearchCandidateRecord",
    "ResearchExperimentRecord",
    "ResearchHypothesisRecord",
    "ResearchJob",
    "ResearchKnowledgeRecord",
    "ResearchPromotionRecord",
    "ResearchReportRecord",
    "ResearchShadowRecord",
    "RiskControlStateRecord",
    "RiskDecisionRecord",
    "RiskEvent",
    "SecurityAssessmentRecord",
    "Signal",
    "Strategy",
    "StrategyVersion",
    "SystemConfiguration",
    "Token",
    "TokenMetadataRecord",
    "TokenSnapshot",
    "Trade",
    "Wallet",
    "WalletClusterRecord",
    "WalletHistoryRecord",
    "WalletKnowledgeRecord",
    "WalletProfileRecord",
    "WalletRelationshipRecord",
    "WalletTimelineRecord",
    "WhitelistEntry",
]
