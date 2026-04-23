from enum import Enum


class SourceType(str, Enum):
    GMAIL = "gmail"
    SLACK = "slack"
    NOTION = "notion"
    JIRA = "jira"


class ItemClassification(str, Enum):
    TASK = "task"
    FOLLOW_UP = "follow_up"
    INFO = "info"
    BLOCKER = "blocker"
    DECISION = "decision"


class DeliveryChannel(str, Enum):
    DB = "db"
    EMAIL = "email"
    SLACK = "slack"
