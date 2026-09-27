"""Task-plan contracts for the D06 scheduler (pure data, no database).

The runtime turns its tables into a `GraphState`; `scheduler.py` decides
from it, deterministically and without side effects:

- whether a plan proposal is acceptable (`validate_plan`),
- which tasks are ready, which lie on the critical path, and who should do
  what next without both participants duplicating a task (`recommend`),
- whether the run is making progress or looping (`detect_loop`).

Bounds (spec D06: "unbounded delegation is rejected"):
MAX_PLAN_TASKS per proposal, MAX_TASKS_PER_RUN overall, MAX_DELEGATION_DEPTH
of parent chains, MAX_ACTIVE_CLAIMS per participant. Proposals create tasks,
never agents."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .contracts import MAX_TEXT, ValidationError, check_list, check_text

TASK_KINDS = ("code", "investigate", "test_design", "review", "code_isolated")
# Tasks that change the run's workspace need its single writer. The other
# kinds produce findings or plans and can be owned by either participant.
WRITE_KINDS = frozenset({"code"})
# D11: independent code work done in its own worktree by the participant who
# is not the writer; its accepted result is integrated into the run's
# workspace by the writer (the integration owner), under the writer's fence.
ISOLATED_KINDS = frozenset({"code_isolated"})

MAX_PLAN_TASKS = 12
MAX_TASKS_PER_RUN = 32
MAX_DELEGATION_DEPTH = 3  # a task may have at most 3 ancestors
MAX_ACTIVE_CLAIMS = 2  # CLAIMED/RUNNING tasks per participant

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TASK_ID_RE = re.compile(r"^tsk_[0-9a-f]{32}$")

# Task states (mirrors contracts.TaskState values, kept as strings here so the
# scheduler stays a pure function over plain data).
DONE = "VERIFIED"
ACTIVE = frozenset({"CLAIMED", "RUNNING", "WAITING_PEER"})
CLAIMABLE = frozenset({"READY", "CHANGES_REQUESTED"})
CLOSED = frozenset({"VERIFIED", "CANCELLED"})


@dataclass(frozen=True)
class TaskSpec:
    """One task in a plan proposal. `depends_on` and `parent` name other keys
    in the same proposal or existing task ids (`tsk_...`)."""

    key: str
    description: str
    kind: str = "code"
    depends_on: tuple[str, ...] = ()
    acceptance_ids: tuple[str, ...] = ()
    parent: str | None = None
    deliverables: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not KEY_RE.match(self.key):
            raise ValidationError(f"task key {self.key!r} must match {KEY_RE.pattern}")
        check_text(self.description, f"task {self.key} description", limit=MAX_TEXT)
        if self.kind not in TASK_KINDS:
            raise ValidationError(f"task {self.key}: kind must be one of {TASK_KINDS}")

    @classmethod
    def from_dict(cls, data: dict) -> "TaskSpec":
        if not isinstance(data, dict):
            raise ValidationError("each task must be an object")
        unknown = set(data) - {"key", "description", "kind", "depends_on", "acceptance_ids", "parent", "deliverables"}
        if unknown:
            raise ValidationError(f"unknown task fields {sorted(unknown)}")
        return cls(
            key=data.get("key", ""),
            description=data.get("description", ""),
            kind=data.get("kind", "code"),
            depends_on=tuple(check_list(data.get("depends_on"), "depends_on", limit=MAX_PLAN_TASKS + MAX_TASKS_PER_RUN, item_limit=64)),
            acceptance_ids=tuple(check_list(data.get("acceptance_ids"), "acceptance_ids", item_limit=64)),
            parent=data.get("parent"),
            deliverables=tuple(check_list(data.get("deliverables"), "deliverables")),
        )

    def to_dict(self) -> dict:
        return {
            "key": self.key, "description": self.description, "kind": self.kind, "depends_on": list(self.depends_on),
            "acceptance_ids": list(self.acceptance_ids), "parent": self.parent, "deliverables": list(self.deliverables),
        }


@dataclass(frozen=True)
class GraphTask:
    """A task as the scheduler sees it."""

    task_id: str
    kind: str
    state: str
    owner: str | None
    required: bool
    depends_on: tuple[str, ...] = ()
    parent_id: str | None = None
    acceptance_ids: tuple[str, ...] = ()
    attempts: int = 0
    proposed_by: str = "controller"
    order: int = 0  # creation order; ties are broken by it, then by task_id


@dataclass(frozen=True)
class ParticipantView:
    participant_id: str
    provider: str
    is_writer: bool
    available: bool = True  # connected or idle; False when unavailable/gone


@dataclass(frozen=True)
class GraphState:
    tasks: dict[str, GraphTask]
    participants: dict[str, ParticipantView]
    acceptance_ids: frozenset[str] = frozenset()
    # participant id -> open requests (questions, review requests) addressed to them
    pending_requests: dict[str, int] = field(default_factory=dict)
    # task id -> open requests (review requests, questions) that block that task
    waiting_on: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedTask:
    """A validated plan entry in dependency order. `depends_on` and `parent`
    hold existing task ids or keys of earlier entries in the same plan."""

    spec: TaskSpec
    depends_on: tuple[str, ...]
    parent: str | None
    depth: int  # number of ancestors


@dataclass(frozen=True)
class Recommendation:
    action: str  # answer | continue | claim | wait | replan | pause
    reason: str
    task_id: str | None = None
    priority: int = 0  # higher first


@dataclass(frozen=True)
class ProgressSample:
    """One observation of the run's state after a message, turn or submission.
    `fingerprint` hashes only task states/revisions, snapshot trees, evidence
    statuses and open findings: text similarity never counts as progress."""

    index: int
    kind: str  # message | turn | submission | check
    fingerprint: str


@dataclass(frozen=True)
class FailureRecord:
    """A failed attempt on a task: a failed required check or a blocking
    review. `signature` identifies the failure (check id + status + exit code
    + a hash of the check output with temporary paths and timings removed, or
    the normalised finding summary) so repeats of the same failed hypothesis
    are recognised and different failures are not."""

    task_id: str
    snapshot_id: str
    signature: str
    index: int


@dataclass(frozen=True)
class LoopVerdict:
    status: str  # ok | stalled | replan | pause
    reason: str
    task_id: str | None = None
    evidence: tuple[str, ...] = ()
