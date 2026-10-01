# Milestone 13 Transition-to-Ledger Mapping

This table captures every money-moving FSM transition and its payout/ledger mapping.

| Transition Trigger | From -> To | Money Movement Intent | Ledger Movement (on payout success) | Outbox Purpose | Payout Destination | Payout Status Target |
| --- | --- | --- | --- | --- | --- | --- |
| Payment callback success | awaiting_payment -> locked | Record funding into escrow context | DR ESCROW_HELD / CR BUYER_CLEARING | collection:tx:<tx_id> (collection side only) | N/A | not_required |
| OTP give (non-serialized) | at_door_pending_inspection -> released | Full seller release | DR SELLER_PAYABLE / CR ESCROW_HELD | tx-release:<tx_id> | seller.mpesa_phone | pending -> succeeded/failed_definite/unknown |
| OTP withhold | at_door_pending_inspection -> refunded_buyer | Full buyer refund | DR BUYER_REFUNDABLE / CR ESCROW_HELD | tx-refund:<tx_id> | buyer.mpesa_phone | pending -> succeeded/failed_definite/unknown |
| Seller action refund_issued | return_received -> refunded_buyer | Full buyer refund | DR BUYER_REFUNDABLE / CR ESCROW_HELD | seller-refund:<tx_id> | buyer.mpesa_phone | pending -> succeeded/failed_definite/unknown |
| Buyer reconfirm accepted | awaiting_buyer_reconfirmation -> resolved_release | Full seller release | DR SELLER_PAYABLE / CR ESCROW_HELD | reconfirm-release:<tx_id> | seller.mpesa_phone | pending -> succeeded/failed_definite/unknown |
| Admin decision REFUND | escalated_admin_review -> resolved_refund | Full buyer refund | DR BUYER_REFUNDABLE / CR ESCROW_HELD | admin-refund:<dispute_id> | buyer.mpesa_phone | pending -> succeeded/failed_definite/unknown |
| Admin decision RELEASE | escalated_admin_review -> resolved_release | Full seller release | DR SELLER_PAYABLE / CR ESCROW_HELD | admin-release:<dispute_id> | seller.mpesa_phone | pending -> succeeded/failed_definite/unknown |
| Admin decision SPLIT (50/50) | escalated_admin_review -> resolved_split | Two intents: seller release + buyer refund | (1) DR SELLER_PAYABLE / CR ESCROW_HELD; (2) DR BUYER_REFUNDABLE / CR ESCROW_HELD | admin-split-release:<dispute_id>; admin-split-refund:<dispute_id> | seller.mpesa_phone + buyer.mpesa_phone | pending -> succeeded/failed_definite/unknown |
| Timeout: locked no dispatch 48h | locked -> refunded_buyer | Full buyer refund | DR BUYER_REFUNDABLE / CR ESCROW_HELD | timeout-refund:<tx_id> | buyer.mpesa_phone | pending -> succeeded/failed_definite/unknown |
| Timeout: hold_24h auto-release | hold_24h -> released | Full seller release | DR SELLER_PAYABLE / CR ESCROW_HELD | hold-release:<tx_id> | seller.mpesa_phone | pending -> succeeded/failed_definite/unknown |
| Timeout: dispute buyer-sent-back 3d | disputed_functional -> released | Full seller release | DR SELLER_PAYABLE / CR ESCROW_HELD | dispute-timeout-release:<tx_id> | seller.mpesa_phone | pending -> succeeded/failed_definite/unknown |

Notes:
- Milestone 13 does not alter the FSM shape; it adds orchestration and reliability around existing transitions.
- Payout outbox rows are expected to commit atomically with business state transitions.
- UNKNOWN outcomes should retry the same rail/provider reference before manual follow-up.