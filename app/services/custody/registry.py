from collections.abc import Mapping
from dataclasses import dataclass

from app.services.custody.ports import CollectionRail, CustodyProvider, PayoutRail


@dataclass(frozen=True)
class CustodyRegistry:
    provider: CustodyProvider
    collection_rails: Mapping[str, CollectionRail]
    payout_rails: Mapping[str, PayoutRail]

    def get_collection_rail(self, rail_name: str) -> CollectionRail:
        return self.collection_rails[rail_name]

    def get_payout_rail(self, rail_name: str) -> PayoutRail:
        return self.payout_rails[rail_name]
