from enum import StrEnum


class CustodyMode(StrEnum):
    TIER_1 = "tier_1"
    TIER_2 = "tier_2"


class CollectionOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED_DEFINITE = "FAILED_DEFINITE"
    UNKNOWN = "UNKNOWN"


class PayoutOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED_DEFINITE = "FAILED_DEFINITE"
    UNKNOWN = "UNKNOWN"
