"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .inclusive import InclusiveService
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None,
          inclusive: InclusiveService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    query = parse_qs(parsed.query)
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if inclusive is not None and (
                parsed.path.startswith("/inclusive/")):
            return route_inclusive(inclusive, method, parsed, body, actor_id, query)
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def route_inclusive(service: InclusiveService, method: str, parsed, body: dict[str, Any],
                    actor_id: str, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
    """把 /inclusive/* 下的请求分派到普惠支持服务，各角色步骤彼此分离。"""

    def q(name: str, default: str = "") -> str:
        return query.get(name, [default])[0]

    path = parsed.path
    try:
        if method == "POST" and path == "/inclusive/regions":
            receipt = service.register_region(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and path == "/inclusive/providers":
            receipt = service.register_provider(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and path == "/inclusive/reviewer-interests":
            receipt = service.declare_reviewer_interest(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and path == "/inclusive/policies":
            receipt = service.freeze_policy(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and path == "/inclusive/rounds":
            receipt = service.open_round(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and path == "/inclusive/rounds/close":
            receipt = service.close_round(actor_id=actor_id, **body)
            return 200, receipt
        if method == "POST" and path == "/inclusive/applicants":
            receipt = service.register_applicant(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and path == "/inclusive/applications":
            receipt = service.submit_application(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and path == "/inclusive/reviews":
            receipt = service.submit_review(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and path == "/inclusive/ranking-runs":
            receipt = service.run_ranking(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and path == "/inclusive/materials":
            receipt = service.submit_material(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and path == "/inclusive/commitments":
            receipt = service.commit_reservation(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and path == "/inclusive/milestones/reports":
            receipt = service.report_milestone(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and path == "/inclusive/milestones/verifications":
            receipt = service.verify_milestone(actor_id=actor_id, **body)
            return 200, receipt
        if method == "POST" and path == "/inclusive/outcomes/reports":
            receipt = service.report_outcome(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and path == "/inclusive/outcomes/verifications":
            receipt = service.verify_outcome(actor_id=actor_id, **body)
            return 200, receipt
        if method == "POST" and path == "/inclusive/waivers":
            receipt = service.waive_application(actor_id=actor_id, **body)
            return 200, receipt
        if method == "POST" and path == "/inclusive/sweeps":
            receipt = service.sweep_expirations(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and path == "/inclusive/budget-reductions":
            receipt = service.reduce_budget(actor_id=actor_id, **body)
            return 200, receipt
        if method == "POST" and path == "/inclusive/specials":
            receipt = service.initiate_special(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and path == "/inclusive/specials/countersign":
            receipt = service.countersign_special(actor_id=actor_id, **body)
            return 200, receipt
        if method == "GET" and path == "/inclusive/applications":
            if not q("application_id"):
                raise ValidationError("application_id 不能为空")
            return 200, service.get_application(q("application_id"))
        if method == "GET" and path == "/inclusive/specials":
            if not q("special_id"):
                raise ValidationError("special_id 不能为空")
            return 200, service.get_special(q("special_id"))
        if method == "GET" and path == "/inclusive/ranking-runs":
            if not q("run_id"):
                raise ValidationError("run_id 不能为空")
            return 200, service.list_ranking(q("run_id"))
        if method == "GET" and path == "/inclusive/waitlist":
            if not q("round_id"):
                raise ValidationError("round_id 不能为空")
            return 200, {"items": service.list_waitlist(q("round_id"))}
        if method == "GET" and path == "/inclusive/installments":
            if not q("application_id"):
                raise ValidationError("application_id 不能为空")
            return 200, service.installment_balance(q("application_id"))
        if method == "GET" and path == "/inclusive/exits":
            return 200, {"items": service.list_exits(q("round_id") or None)}
        if method == "GET" and path == "/inclusive/reports/funds":
            if not q("round_id"):
                raise ValidationError("round_id 不能为空")
            return 200, service.fund_report(q("round_id"))
        if method == "GET" and path == "/inclusive/reports/regions":
            if not q("round_id"):
                raise ValidationError("round_id 不能为空")
            return 200, service.region_report(q("round_id"))
        if method == "GET" and path == "/inclusive/reports/outcomes":
            if not q("round_id"):
                raise ValidationError("round_id 不能为空")
            return 200, service.outcome_report(q("round_id"))
        if method == "GET" and path == "/inclusive/reports/policy-comparison":
            return 200, service.compare_policies()
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    inclusive: InclusiveService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                inclusive=self.inclusive)
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    Handler.inclusive = InclusiveService(database)
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
