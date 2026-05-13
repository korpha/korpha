"""Single point that imports every SQLModel table.

Importing this module ensures `SQLModel.metadata` knows every table — needed
before `create_all()`. Domain code can import models directly from their
own modules; this exists for the metadata side-effect.
"""
from __future__ import annotations

from korpha.approvals.model import (
    ActionClass,
    Approval,
    ApprovalStatus,
    AutonomyMode,
    TrustEnvelope,
)
from korpha.audit.model import Activity, ActorType, Cost, InferenceTier
from korpha.blockers.model import (
    Blocker,
    BlockerKind,
    BlockerStatus,
    BlockerUrgency,
)
from korpha.business.model import (
    Business,
    BusinessStatus,
    Goal,
    GoalStatus,
    Project,
    ProjectStatus,
    Task,
    TaskPriority,
    TaskStatus,
)
from korpha.business_units.model import (
    BusinessUnit,
    BusinessUnitKind,
    DeploymentMode,
    Product,
    ProductKind,
)
from korpha.credentials.model import (
    ExternalServiceAccount,
    ExternalServiceKind,
)
from korpha.shared_resources.model import (
    SharedResource,
    SharedResourceKind,
    SharedResourceUsage,
)
from korpha.cooperation.model import (
    CooperationProposal,
    CooperationStatus,
    CrossUnitQueryLog,
)
from korpha.memory.grants import CrossNamespaceRecallGrant
from korpha.cofounder.model import (
    AgentRole,
    Message,
    MessageSenderType,
    MessageSummary,
    RoleType,
    Thread,
    ThreadPlatform,
    ThreadStatus,
)
from korpha.budgets.model import (
    BudgetPolicy,
    BudgetScope,
    BudgetWindow,
)
from korpha.commerce.revenue import RevenueEvent, RevenueKind
from korpha.goals.model import Goal as AgentGoal, GoalStatus as AgentGoalStatus  # noqa: F401
from korpha.heartbeats.model import (
    Routine,
    RoutineSchedule,
    Wakeup,
    WakeupKind,
    WakeupStatus,
)
from korpha.identity.model import Founder
from korpha.kanban.artifacts import (
    ArtifactKind,
    ArtifactReviewState,
    CardArtifact,
)
from korpha.kanban.refs import (
    KanbanCardRef,
    RefRelation,
)
from korpha.kanban.model import (
    CardPriority,
    KanbanCard,
    KanbanCardEvent,
    KanbanColumn,
)
from korpha.memory.notes import FounderNote
from korpha.workspaces.model import Repo

__all__ = [
    "ActionClass",
    "Activity",
    "ActorType",
    "AgentRole",
    "Approval",
    "ApprovalStatus",
    "AutonomyMode",
    "Blocker",
    "BlockerKind",
    "BlockerStatus",
    "BlockerUrgency",
    "ArtifactKind",
    "ArtifactReviewState",
    "BudgetPolicy",
    "BudgetScope",
    "BudgetWindow",
    "Business",
    "BusinessStatus",
    "CardArtifact",
    "CardPriority",
    "Cost",
    "Founder",
    "FounderNote",
    "Goal",
    "GoalStatus",
    "InferenceTier",
    "KanbanCard",
    "KanbanCardEvent",
    "KanbanCardRef",
    "KanbanColumn",
    "RefRelation",
    "Message",
    "MessageSenderType",
    "MessageSummary",
    "Project",
    "ProjectStatus",
    "Repo",
    "RevenueEvent",
    "RevenueKind",
    "RoleType",
    "Routine",
    "RoutineSchedule",
    "Task",
    "TaskPriority",
    "TaskStatus",
    "Thread",
    "ThreadPlatform",
    "ThreadStatus",
    "TrustEnvelope",
    "Wakeup",
    "WakeupKind",
    "WakeupStatus",
]
