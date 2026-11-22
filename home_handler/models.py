from dataclasses import dataclass, field


@dataclass
class Observation:
    watcher: str
    fingerprint: str
    severity: int  # 0 info, 1 notice, 2 warn, 3 urgent
    title: str
    detail: dict
    ts: float


@dataclass
class Incident:
    id: int
    fingerprint: str
    watcher: str
    source: str  # 'watcher' | 'loose_ends'
    ext_id: str | None
    title: str
    severity: int
    status: str  # 'open' | 'waiting' | 'done' | 'closed'
    kind: str | None
    sort_label: str | None
    sort_reason: str | None
    first_seen: float
    last_seen: float
    count: int
    detail: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
