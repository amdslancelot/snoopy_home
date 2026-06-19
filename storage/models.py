from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Reminder:
    id: Optional[int]
    channel_id: int
    creator_id: int
    target_user_id: int
    message: str
    trigger_time: datetime
    is_recurring: bool = False
    cron_expression: Optional[str] = None
    job_id: Optional[str] = None
    is_active: bool = True
    voice: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ChoreTask:
    id: Optional[int]
    channel_id: int
    name: str
    description: str
    assigned_user_id: Optional[int]
    cron_expression: str
    last_completed: Optional[datetime] = None
    job_id: Optional[str] = None
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class HouseholdMember:
    discord_id: int
    username: str
    display_name: str
    timezone: str = "UTC"
    profile: dict = field(default_factory=dict)
    is_active: bool = True
