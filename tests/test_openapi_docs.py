from app.main import app
from fastapi.testclient import TestClient


def _operation(spec: dict, path: str, method: str) -> dict:
    return spec["paths"][path][method]


def test_core_endpoints_have_summary_and_description() -> None:
    client = TestClient(app)
    spec = client.get("/openapi.json").json()

    targets = [
        ("/auth/register", "post"),
        ("/auth/login", "post"),
        ("/auth/logout", "post"),
        ("/auth/me", "get"),
        ("/listings", "post"),
        ("/listings/{listing_id}", "get"),
        ("/transactions", "post"),
        ("/transactions/payment-callback", "post"),
        ("/transactions/{transaction_id}/dispatch", "post"),
        ("/transactions/{transaction_id}/arrival", "post"),
        ("/transactions/{transaction_id}/otp-give", "post"),
        ("/transactions/{transaction_id}/report-functional-issue", "post"),
        ("/webhooks/loop/collection", "post"),
        ("/api/webhooks/pesapal/callback", "get"),
        ("/api/webhooks/pesapal/callback", "post"),
        ("/notifications", "get"),
        ("/admin/disputes/escalation-queue", "get"),
        ("/admin/disputes/{dispute_id}/timeline", "get"),
        ("/admin/disputes/{dispute_id}/force-resolve", "post"),
        ("/admin/rails/health", "get"),
        ("/admin/audit/money-events", "get"),
        ("/admin/simulated-custody/collections/force-complete", "post"),
        ("/admin/simulated-custody/collections/{provider_reference}/progress", "post"),
        ("/admin/simulated-custody/payouts/{provider_reference}/progress", "post"),
    ]

    for path, method in targets:
        operation = _operation(spec, path, method)
        assert operation.get("summary"), f"Missing summary for {method.upper()} {path}"
        assert operation.get("description"), f"Missing description for {method.upper()} {path}"


def test_key_request_schema_fields_include_descriptions() -> None:
    client = TestClient(app)
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]

    checks = [
        ("RegisterUserRequest", "phone"),
        ("CreateListingRequest", "dispute_policy"),
        ("CreateTransactionRequest", "amount"),
        ("PaymentCallbackRequest", "result_code"),
        ("ReportFunctionalIssueRequest", "category"),
        ("SellerResolutionActionRequest", "action"),
        ("AdminForceResolveRequest", "reason"),
        ("AdminForceCompleteCollectionRequest", "escrow_reference"),
    ]

    for schema_name, field_name in checks:
        schema = schemas[schema_name]
        field = schema["properties"][field_name]
        assert field.get("description"), f"Missing field description for {schema_name}.{field_name}"


def test_openapi_blocks_arbitrary_payout_trigger_paths() -> None:
    client = TestClient(app)
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]

    payout_post_paths = sorted(
        path for path, operations in paths.items() if "post" in operations and "payout" in path
    )

    assert payout_post_paths == ["/admin/simulated-custody/payouts/{provider_reference}/progress"]

    payout_progress_operation = _operation(
        spec,
        "/admin/simulated-custody/payouts/{provider_reference}/progress",
        "post",
    )
    # Guardrail: endpoint accepts only a provider reference and cannot
    # carry arbitrary payout params.
    assert "requestBody" not in payout_progress_operation

    forbidden_paths = {
        "/admin/payouts/run",
        "/admin/payouts/reconcile",
        "/transactions/{transaction_id}/payout",
    }
    assert forbidden_paths.isdisjoint(set(paths.keys()))
