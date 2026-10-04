"""把普惠支持领域服务暴露为不依赖第三方框架的 HTTP/JSON 接口。"""

from __future__ import annotations

import argparse
import json
from typing import Any
from urllib.parse import parse_qs, urlparse

from digital_trade_foundation.api import Handler as FoundationHandler
from digital_trade_foundation.api import route as foundation_route
from digital_trade_foundation.errors import DomainError, ValidationError
from digital_trade_foundation.storage import Database
from http.server import ThreadingHTTPServer

from .service import SupportService


POST_ROUTES = {
    "/support/policies": "create_policy",
    "/support/policies/freeze": "freeze_policy",
    "/support/policies/budget-cut": "cut_budget",
    "/support/policies/rank": "run_ranking",
    "/support/applicants": "register_applicant",
    "/support/providers": "register_provider",
    "/support/providers/conflicts": "declare_conflict",
    "/support/applications": "submit_application",
    "/support/applications/materials": "submit_material",
    "/support/applications/review": "review_application",
    "/support/applications/commit": "commit_reservation",
    "/support/applications/withdraw": "withdraw_application",
    "/support/applications/exit": "exit_application",
    "/support/applications/milestones/report": "report_milestone",
    "/support/applications/milestones/verify": "verify_milestone",
    "/support/applications/outcomes/report": "report_outcome",
    "/support/applications/outcomes/verify": "verify_outcome",
    "/support/special-approvals": "propose_special_approval",
    "/support/special-approvals/cosign": "cosign_special_approval",
}


def _created(receipt, result: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), {
        "request_id": receipt.request_id,
        "resource_type": receipt.resource_type,
        "resource_id": receipt.resource_id,
        "replayed": receipt.replayed,
        "result": result,
    }


def _query(parsed, key: str) -> str:
    value = parse_qs(parsed.query).get(key, [""])[0]
    if not value:
        raise ValidationError(f"{key} 不能为空")
    return value


def _dispatch_support(service: SupportService, method: str, path: str,
                      body: dict[str, Any], actor_id: str) -> tuple[int, dict[str, Any]] | None:
    parsed = urlparse(path)
    if method == "POST" and parsed.path in POST_ROUTES:
        receipt, result = getattr(service, POST_ROUTES[parsed.path])(actor_id=actor_id, **body)
        return _created(receipt, result)
    if method == "GET" and parsed.path == "/support/policy":
        return 200, service.get_policy(_query(parsed, "policy_id"))
    if method == "GET" and parsed.path == "/support/policy/ranking":
        return 200, {"items": service.policy_ranking(_query(parsed, "policy_id"))}
    if method == "GET" and parsed.path == "/support/policy/decisions":
        return 200, {"items": service.policy_decisions(_query(parsed, "policy_id"))}
    if method == "GET" and parsed.path == "/support/policy/report":
        return 200, service.policy_report(_query(parsed, "policy_id"))
    if method == "GET" and parsed.path == "/support/application":
        return 200, service.get_application(_query(parsed, "application_id"))
    if method == "GET" and parsed.path == "/support/application/explain":
        return 200, service.explain_application(_query(parsed, "application_id"))
    if method == "GET" and parsed.path == "/support/compare":
        policy_ids = [item for item in _query(parsed, "policy_ids").split(",") if item]
        return 200, service.compare_policies(policy_ids)
    return None


def route(service: SupportService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """先分派普惠支持接口，未命中时回落到基础服务路由。"""

    headers = headers or {}
    body = body or {}
    actor_id = headers.get("X-Actor-Id", "")
    try:
        handled = _dispatch_support(service, method, path, body, actor_id)
        if handled is not None:
            return handled
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}
    return foundation_route(service, method, path, body, headers)


class Handler(FoundationHandler):
    """复用基础服务的请求解析，仅替换路由函数。"""

    service: SupportService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)


def main() -> int:
    """启动普惠支持 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动普惠支持资格、配额与成效跟踪服务")
    parser.add_argument("--database", default="support.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = SupportService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
