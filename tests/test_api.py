"""端到端测试：真实 uvicorn 起服务 + stdlib urllib 调用，覆盖
创建 / 查询 / 分页 / 健康检查 / 422 / 复算标志 / 收款与冲销 / SQLite 持久化。

运行：先 pip install -r requirements.txt，再
    python -m unittest tests.test_api -v
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import uvicorn


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(base: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # 启动期间连接被拒是正常的
            last = exc
            time.sleep(0.2)
    raise RuntimeError(f"服务未在 {timeout}s 内就绪: {last}")


def _request(base: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{base}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


class ServerThread(threading.Thread):
    def __init__(self, db_file: str, port: int) -> None:
        super().__init__(daemon=True)
        self.db_file = db_file
        self.port = port

    def run(self) -> None:
        uvicorn.run(
            "app.main:app",
            host="127.0.0.1",
            port=self.port,
            log_level="error",
        )


class ApiTests(unittest.TestCase):
    server: ServerThread
    tmpdir: tempfile.TemporaryDirectory
    base: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.TemporaryDirectory()
        db_file = str(Path(cls.tmpdir.name) / "test.db")
        os.environ["DB_PATH"] = db_file
        port = _free_port()
        cls.base = f"http://127.0.0.1:{port}"
        cls.server = ServerThread(db_file, port)
        cls.server.start()
        _wait_ready(cls.base)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmpdir.cleanup()
        os.environ.pop("DB_PATH", None)

    def _request(self, method: str, path: str, body: dict | None = None):
        return _request(self.base, method, path, body)

    def test_01_health(self) -> None:
        status, body = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok", "database": "up"})

    def test_02_create_and_get(self) -> None:
        payload = {
            "currency": "USD",
            "lines": [
                {"description": "books", "net": "10.05", "tax_rate": "0.10"},
                {"description": "pens", "net": "20.05", "tax_rate": "0.10"},
            ],
        }
        status, body = self._request("POST", "/api/v1/invoices", payload)
        self.assertEqual(status, 201, body)
        self.assertIn("id", body)
        self.assertEqual(body["totals"]["total_tax"], "3.01")
        self.assertEqual(body["rounding"]["residual_units"], -1)
        self.assertTrue(body["recompute_matched"])

        invoice_id = body["id"]
        status2, fetched = self._request("GET", f"/api/v1/invoices/{invoice_id}")
        self.assertEqual(status2, 200)
        self.assertEqual(fetched["totals"], body["totals"])
        self.assertTrue(fetched["recompute_matched"])
        self.assertEqual(len(fetched["lines"]), 2)

    def test_03_create_fx(self) -> None:
        payload = {
            "currency": "USD",
            "target_currency": "JPY",
            "exchange_rate": "150.25",
            "lines": [{"net": "1.00", "tax_rate": "0.10"}],
        }
        status, body = self._request("POST", "/api/v1/invoices", payload)
        self.assertEqual(status, 201, body)
        self.assertEqual(body["fx"]["target_currency"], "JPY")
        # 150.25 -> 150 JPY；税 15.025 -> 15 JPY
        self.assertEqual(body["lines"][0]["converted"]["net"], "150")
        self.assertEqual(body["lines"][0]["converted"]["tax"], "15")
        # 原币保留
        self.assertEqual(body["lines"][0]["net"], "1.00")
        self.assertEqual(body["lines"][0]["tax"], "0.10")

    def test_04_validation_error(self) -> None:
        # 净额不是最小单位整数倍
        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "1.005", "tax_rate": "0.1"}]},
        )
        self.assertEqual(status, 422)
        self.assertIn("detail", body)

        # 未知币种
        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "ZZZ", "lines": [{"net": "1.00", "tax_rate": "0.1"}]},
        )
        self.assertEqual(status, 422)

        # pydantic 校验：金额必须是字符串
        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": 1.0, "tax_rate": "0.1"}]},
        )
        self.assertEqual(status, 422)

    def test_05_not_found(self) -> None:
        status, body = self._request("GET", "/api/v1/invoices/999999")
        self.assertEqual(status, 404)

    def test_06_pagination_and_persistence(self) -> None:
        # 再建 3 张，配合前两张共 5 张
        for i in range(3):
            status, body = self._request(
                "POST",
                "/api/v1/invoices",
                {"currency": "CNY", "lines": [{"net": "100.00", "tax_rate": "0.13"}]},
            )
            self.assertEqual(status, 201)

        status, body = self._request("GET", "/api/v1/invoices?page=1&page_size=2")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 5)
        self.assertEqual(body["pages"], 3)
        self.assertEqual(body["page"], 1)
        self.assertEqual(body["page_size"], 2)
        self.assertEqual(len(body["items"]), 2)
        # 按 id 倒序
        self.assertGreater(body["items"][0]["id"], body["items"][1]["id"])
        self.assertEqual(body["items"][0]["line_count"], 1)
        self.assertEqual(body["items"][0]["currency"], "CNY")

        status, body = self._request("GET", "/api/v1/invoices?page=3&page_size=2")
        self.assertEqual(len(body["items"]), 1)

        # 分页参数越界 -> 422
        status, _ = self._request("GET", "/api/v1/invoices?page=0")
        self.assertEqual(status, 422)

    def test_07_persisted_db_file(self) -> None:
        # SQLite 文件确实落盘
        self.assertTrue(os.path.exists(os.environ["DB_PATH"]))
        self.assertGreater(os.path.getsize(os.environ["DB_PATH"]), 0)

    def test_08_payments(self) -> None:
        # 建一张 USD 发票：total_gross = 33.11
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {
                "currency": "USD",
                "lines": [
                    {"net": "10.05", "tax_rate": "0.10"},
                    {"net": "20.05", "tax_rate": "0.10"},
                ],
            },
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        self.assertEqual(inv["totals"]["total_gross"], "33.11")

        # 初始：无收款，未收余额 = 含税总额
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(status, 200)
        self.assertEqual(ov["total_gross"], "33.11")
        self.assertEqual(ov["paid_total"], "0.00")
        self.assertEqual(ov["outstanding"], "33.11")
        self.assertEqual(ov["payment_count"], 0)
        self.assertEqual(ov["payments"], [])

        # 登记第一笔收款
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "20"}
        )
        self.assertEqual(status, 201, pay)
        self.assertEqual(pay["invoice_id"], invoice_id)
        self.assertEqual(pay["amount"], "20.00")  # 按币种最小单位规范化
        self.assertEqual(pay["currency"], "USD")
        self.assertIn("created_at", pay)
        self.assertEqual(pay["paid_total"], "20.00")
        self.assertEqual(pay["outstanding"], "13.11")

        # 第二笔：恰好收满
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "13.11"}
        )
        self.assertEqual(status, 201, pay)
        self.assertEqual(pay["paid_total"], "33.11")
        self.assertEqual(pay["outstanding"], "0.00")

        # 查询：两笔记录，按登记顺序返回，均带登记时间
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(status, 200)
        self.assertEqual(ov["payment_count"], 2)
        self.assertEqual([p["amount"] for p in ov["payments"]], ["20.00", "13.11"])
        for p in ov["payments"]:
            self.assertIn("created_at", p)
        self.assertEqual(ov["paid_total"], "33.11")
        self.assertEqual(ov["outstanding"], "0.00")

        # 已收满，再收 0.01 -> 422，且不留下记录
        status, body = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "0.01"}
        )
        self.assertEqual(status, 422)
        self.assertIn("detail", body)
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(ov["payment_count"], 2)
        self.assertEqual(ov["paid_total"], "33.11")

    def test_09_payment_validation(self) -> None:
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "10.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        path = f"/api/v1/invoices/{invoice_id}/payments"

        # 超额（累计 > 含税总额 10.00）
        status, _ = self._request("POST", path, {"amount": "10.01"})
        self.assertEqual(status, 422)
        # 零 / 负数
        status, _ = self._request("POST", path, {"amount": "0"})
        self.assertEqual(status, 422)
        status, _ = self._request("POST", path, {"amount": "-5.00"})
        self.assertEqual(status, 422)
        # 非十进制字符串 / 非字符串
        status, _ = self._request("POST", path, {"amount": "abc"})
        self.assertEqual(status, 422)
        status, _ = self._request("POST", path, {"amount": 1.5})
        self.assertEqual(status, 422)
        # 小于币种最小单位
        status, _ = self._request("POST", path, {"amount": "0.001"})
        self.assertEqual(status, 422)

        # 以上全部失败，不得留下任何记录
        status, ov = self._request("GET", path)
        self.assertEqual(ov["payment_count"], 0)
        self.assertEqual(ov["paid_total"], "0.00")
        self.assertEqual(ov["outstanding"], "10.00")

        # 不存在的发票：登记 404、查询 404
        status, _ = self._request(
            "POST", "/api/v1/invoices/999999/payments", {"amount": "1.00"}
        )
        self.assertEqual(status, 404)
        status, _ = self._request("GET", "/api/v1/invoices/999999/payments")
        self.assertEqual(status, 404)

    def test_10_concurrent_payments_no_overpay(self) -> None:
        # total_gross = 100.00，10 个并发请求各付 20.00：
        # 同事务校验+写入，恰好 5 笔成功、5 笔 422，最终不超收
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "100.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]

        codes: list[int] = []
        lock = threading.Lock()

        def pay() -> None:
            code, _ = self._request(
                "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "20.00"}
            )
            with lock:
                codes.append(code)

        threads = [threading.Thread(target=pay) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(codes), [201] * 5 + [422] * 5)
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(ov["payment_count"], 5)
        self.assertEqual(ov["paid_total"], "100.00")
        self.assertEqual(ov["outstanding"], "0.00")

    def test_11_due_date_persisted(self) -> None:
        # 带 due_date 创建并持久化，GET 单张时原样返回，且复算仍然一致
        payload = {
            "currency": "USD",
            "due_date": "2026-10-01",
            "lines": [{"net": "10.00", "tax_rate": "0.10"}],
        }
        status, body = self._request("POST", "/api/v1/invoices", payload)
        self.assertEqual(status, 201, body)
        self.assertEqual(body["due_date"], "2026-10-01")
        self.assertTrue(body["recompute_matched"])
        invoice_id = body["id"]

        status, fetched = self._request("GET", f"/api/v1/invoices/{invoice_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["due_date"], "2026-10-01")
        self.assertTrue(fetched["recompute_matched"])

        # 不传 due_date：响应中为 null，不影响其他字段
        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "10.00", "tax_rate": "0.10"}]},
        )
        self.assertEqual(status, 201, body)
        self.assertIsNone(body["due_date"])

        # 非法日期一律 422：格式不符 / 不存在的日期 / 带时间成分
        for bad in ("2026-10", "2026-10-01T00:00:00Z", "2026/10/01",
                    "2026-13-01", "2026-02-30", "not-a-date"):
            status, resp = self._request(
                "POST",
                "/api/v1/invoices",
                {
                    "currency": "USD",
                    "due_date": bad,
                    "lines": [{"net": "10.00", "tax_rate": "0.10"}],
                },
            )
            self.assertEqual(status, 422, bad)
            self.assertIn("detail", resp)

    def test_12_overdue_query(self) -> None:
        # 构造发票集合，固定 as_of=2026-09-23
        def make(due_date, currency="USD", lines=None, pay=None):
            payload = {"currency": currency, "lines": lines or [{"net": "10.00", "tax_rate": "0"}]}
            if due_date is not None:
                payload["due_date"] = due_date
            status, inv = self._request("POST", "/api/v1/invoices", payload)
            self.assertEqual(status, 201, inv)
            if pay:
                status, pay_resp = self._request(
                    "POST", f"/api/v1/invoices/{inv['id']}/payments", {"amount": pay}
                )
                self.assertEqual(status, 201, pay_resp)
            return inv["id"]

        id_a = make("2026-09-22", lines=[
            {"net": "10.05", "tax_rate": "0.10"},
            {"net": "20.05", "tax_rate": "0.10"},
        ])  # gross 33.11，逾期未付
        make("2026-09-23")  # due_date == as_of，不逾期
        make("2026-09-10", pay="10.00")  # 已收满，排除
        id_d = make("2026-09-01", currency="JPY",
                    lines=[{"net": "1000", "tax_rate": "0.10"}])  # gross 1100，0 位小数
        id_e = make("2026-09-20",
                    lines=[{"net": "100.00", "tax_rate": "0"}],
                    pay="40.00")  # gross 100.00，部分收款
        make(None)  # 无到期日，排除
        make("2026-12-31")  # 未来到期，排除
        id_h = make("2000-01-01")  # 远古到期，缺省 as_of（UTC 今日）下也必逾期

        # 固定 as_of：共 4 张逾期（早前测试创建的发票均无 due_date，不计入）
        status, body = self._request(
            "GET", "/api/v1/invoices/overdue?as_of=2026-09-23&page=1&page_size=2"
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["as_of"], "2026-09-23")
        self.assertEqual(body["total"], 4)
        self.assertEqual(body["pages"], 2)
        self.assertEqual(body["page"], 1)
        self.assertEqual(body["page_size"], 2)
        # 按 id 倒序：H(最晚建) 在前，A 最后
        self.assertEqual([it["id"] for it in body["items"]], [id_h, id_e])

        status, page2 = self._request(
            "GET", "/api/v1/invoices/overdue?as_of=2026-09-23&page=2&page_size=2"
        )
        self.assertEqual(status, 200)
        self.assertEqual([it["id"] for it in page2["items"]], [id_d, id_a])
        self.assertEqual(page2["total"], 4)

        by_id = {it["id"]: it for it in body["items"] + page2["items"]}

        # 部分收款：精确未收余额 = 100.00 - 40.00 = 60.00
        e = by_id[id_e]
        self.assertEqual(e["currency"], "USD")
        self.assertEqual(e["due_date"], "2026-09-20")
        self.assertEqual(e["total_gross"], "100.00")
        self.assertEqual(e["paid_total"], "40.00")
        self.assertEqual(e["outstanding"], "60.00")
        self.assertIn("created_at", e)

        # JPY：0 位小数，金额不带小数点
        d = by_id[id_d]
        self.assertEqual(d["currency"], "JPY")
        self.assertEqual(d["total_gross"], "1100")
        self.assertEqual(d["paid_total"], "0")
        self.assertEqual(d["outstanding"], "1100")

        # 未付 USD 发票
        a = by_id[id_a]
        self.assertEqual(a["total_gross"], "33.11")
        self.assertEqual(a["paid_total"], "0.00")
        self.assertEqual(a["outstanding"], "33.11")

        # 缺省 as_of：取 UTC 当前日期；远古到期的 H 必在结果中
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        status, body = self._request(
            "GET", "/api/v1/invoices/overdue?page_size=100"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["as_of"], today)
        self.assertIn(id_h, [it["id"] for it in body["items"]])
        # 回显的每个条目都必须满足：due_date < as_of、outstanding > 0
        from decimal import Decimal
        for it in body["items"]:
            self.assertLess(it["due_date"], today)
            self.assertGreater(Decimal(it["outstanding"]), 0)

        # 非法 as_of -> 422
        for bad in ("2026-09", "2026-09-23T00:00:00Z", "2026/09/23",
                    "2026-13-01", "not-a-date"):
            status, resp = self._request(
                "GET", f"/api/v1/invoices/overdue?as_of={bad}"
            )
            self.assertEqual(status, 422, bad)
            self.assertIn("detail", resp)

        # 分页参数越界 -> 422
        status, _ = self._request("GET", "/api/v1/invoices/overdue?page=0")
        self.assertEqual(status, 422)
        status, _ = self._request("GET", "/api/v1/invoices/overdue?page_size=101")
        self.assertEqual(status, 422)

    def test_13_payment_allocation(self) -> None:
        # 待结发票：
        #   A: USD 11.00 due 2026-09-10
        #   B: USD 22.00 due 2026-09-05（最早到期）
        #   C: USD 33.00 无到期日（排最后）
        #   D: USD 44.00 due 2026-09-10（与 A 同日，id 大于 A）
        #   J: JPY 1000   due 2026-08-01
        def make(currency, gross, due_date):
            payload = {"currency": currency, "lines": [{"net": gross, "tax_rate": "0"}]}
            if due_date is not None:
                payload["due_date"] = due_date
            status, inv = self._request("POST", "/api/v1/invoices", payload)
            self.assertEqual(status, 201, inv)
            return inv["id"]

        id_a = make("USD", "11.00", "2026-09-10")
        id_b = make("USD", "22.00", "2026-09-05")
        id_c = make("USD", "33.00", None)
        id_d = make("USD", "44.00", "2026-09-10")
        id_j = make("JPY", "1000", "2026-08-01")
        self.assertLess(id_a, id_d)  # 同日按 id 升序

        # 传入顺序故意打乱；分配顺序仍为 B -> A -> D -> C
        status, body = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "usd", "amount": "50", "invoice_ids": [id_c, id_d, id_b, id_a]},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["currency"], "USD")
        self.assertEqual(body["amount"], "50.00")
        self.assertEqual(body["allocated_total"], "50.00")
        self.assertIn("created_at", body)
        self.assertEqual(
            [(it["invoice_id"], it["allocated"], it["outstanding"])
             for it in body["allocations"]],
            [
                (id_b, "22.00", "0.00"),
                (id_a, "11.00", "0.00"),
                (id_d, "17.00", "27.00"),
                (id_c, "0.00", "33.00"),
            ],
        )
        # 实际抵扣的三张各带 payment_id；未抵扣的 C 不产生流水
        self.assertIsNone(body["allocations"][3]["payment_id"])
        for it in body["allocations"][:3]:
            self.assertIsInstance(it["payment_id"], int)
        # 到期日按顺序回显
        self.assertEqual(
            [it["due_date"] for it in body["allocations"]],
            ["2026-09-05", "2026-09-10", "2026-09-10", None],
        )

        # 单张收款查询必须看得到分配结果（沿用 payments 流水）
        status, ov = self._request("GET", f"/api/v1/invoices/{id_b}/payments")
        self.assertEqual(status, 200)
        self.assertEqual(ov["payment_count"], 1)
        self.assertEqual(ov["payments"][0]["amount"], "22.00")
        self.assertEqual(ov["payments"][0]["currency"], "USD")
        self.assertEqual(ov["paid_total"], "22.00")
        self.assertEqual(ov["outstanding"], "0.00")

        status, ov = self._request("GET", f"/api/v1/invoices/{id_d}/payments")
        self.assertEqual(ov["payment_count"], 1)
        self.assertEqual(ov["paid_total"], "17.00")
        self.assertEqual(ov["outstanding"], "27.00")

        # 未抵扣：没有流水
        status, ov = self._request("GET", f"/api/v1/invoices/{id_c}/payments")
        self.assertEqual(ov["payment_count"], 0)
        self.assertEqual(ov["paid_total"], "0.00")
        self.assertEqual(ov["outstanding"], "33.00")

        # 逾期查询反映分配后的余额：A/B 已收满被排除，D 仍在
        status, overdue = self._request(
            "GET", "/api/v1/invoices/overdue?as_of=2026-09-23&page_size=100"
        )
        self.assertEqual(status, 200)
        overdue_ids = {it["id"]: it for it in overdue["items"]}
        self.assertNotIn(id_a, overdue_ids)
        self.assertNotIn(id_b, overdue_ids)
        self.assertNotIn(id_c, overdue_ids)  # 无到期日
        d = overdue_ids[id_d]
        self.assertEqual(d["paid_total"], "17.00")
        self.assertEqual(d["outstanding"], "27.00")

        # ---- 整笔拒绝场景：任何失败都不得留下部分收款 ----

        # 总未收不足：D(27) + C(33) = 60，请求 60.01
        status, resp = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "60.01", "invoice_ids": [id_d, id_c]},
        )
        self.assertEqual(status, 422)
        self.assertIn("detail", resp)
        status, ov = self._request("GET", f"/api/v1/invoices/{id_d}/payments")
        self.assertEqual(ov["payment_count"], 1)  # 仍是原来的 17.00
        self.assertEqual(ov["paid_total"], "17.00")
        status, ov = self._request("GET", f"/api/v1/invoices/{id_c}/payments")
        self.assertEqual(ov["payment_count"], 0)

        # 币种不符（JPY 列表里混了 USD 发票）
        status, resp = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "JPY", "amount": "1000", "invoice_ids": [id_j, id_c]},
        )
        self.assertEqual(status, 422)
        self.assertIn("USD", resp["detail"])
        status, ov = self._request("GET", f"/api/v1/invoices/{id_j}/payments")
        self.assertEqual(ov["payment_count"], 0)

        # 发票不存在 -> 404，且不写入列表里其他发票
        status, resp = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "1.00",
             "invoice_ids": [id_c, 999999]},
        )
        self.assertEqual(status, 404)
        self.assertIn("999999", resp["detail"])
        status, ov = self._request("GET", f"/api/v1/invoices/{id_c}/payments")
        self.assertEqual(ov["payment_count"], 0)

        # 列表重复 / 为空
        status, _ = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "1.00",
             "invoice_ids": [id_c, id_c]},
        )
        self.assertEqual(status, 422)
        status, _ = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "1.00", "invoice_ids": []},
        )
        self.assertEqual(status, 422)

        # 金额：≤0 / 小于最小单位 / 非十进制
        for bad in ("0", "-1.00", "0.001", "abc"):
            status, _ = self._request(
                "POST",
                "/api/v1/payment-allocations",
                {"currency": "USD", "amount": bad, "invoice_ids": [id_c]},
            )
            self.assertEqual(status, 422, bad)

        # 0 位小数币种：JPY 600 -> 分配 600，余 400，不带小数位
        status, body = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "JPY", "amount": "600", "invoice_ids": [id_j]},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["amount"], "600")
        self.assertEqual(body["allocations"][0]["allocated"], "600")
        self.assertEqual(body["allocations"][0]["outstanding"], "400")
        # JPY 最小单位是 1，0.5 必须拒绝
        status, _ = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "JPY", "amount": "0.5", "invoice_ids": [id_j]},
        )
        self.assertEqual(status, 422)
        # 剩余 400 恰好收满
        status, body = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "JPY", "amount": "400", "invoice_ids": [id_j]},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["allocations"][0]["outstanding"], "0")

        # 恰好 60 收满 D+C：两张都清零
        status, body = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "60.00",
             "invoice_ids": [id_c, id_d]},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(
            [(it["invoice_id"], it["allocated"], it["outstanding"])
             for it in body["allocations"]],
            [(id_d, "27.00", "0.00"), (id_c, "33.00", "0.00")],
        )

    def test_14_allocation_concurrent_no_overpay(self) -> None:
        # gross 100.00：7 个分配请求（列表各只含该发票）+ 3 个单张收款，
        # 每个 20.00。BEGIN IMMEDIATE 串行化 -> 恰好 5 笔成功、5 笔 422，不超收
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD",
             "due_date": "2026-08-01",
             "lines": [{"net": "100.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]

        codes: list[int] = []
        lock = threading.Lock()

        def allocate() -> None:
            code, _ = self._request(
                "POST",
                "/api/v1/payment-allocations",
                {"currency": "USD", "amount": "20.00", "invoice_ids": [invoice_id]},
            )
            with lock:
                codes.append(code)

        def single() -> None:
            code, _ = self._request(
                "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "20.00"}
            )
            with lock:
                codes.append(code)

        threads = [threading.Thread(target=allocate) for _ in range(7)]
        threads += [threading.Thread(target=single) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(codes), [201] * 5 + [422] * 5)
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(ov["paid_total"], "100.00")
        self.assertEqual(ov["outstanding"], "0.00")
        self.assertEqual(ov["payment_count"], 5)

    def test_15_payment_reversals(self) -> None:
        # 建一张 USD 发票：total_gross = 33.11，先收款 20.00
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {
                "currency": "USD",
                "lines": [
                    {"net": "10.05", "tax_rate": "0.10"},
                    {"net": "20.05", "tax_rate": "0.10"},
                ],
            },
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]

        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "20.00"}
        )
        self.assertEqual(status, 201, pay)
        payment_id = pay["id"]
        # 新收款：未冲销，净入账 = 原金额
        self.assertEqual(pay["reversed_total"], "0.00")
        self.assertEqual(pay["net_amount"], "20.00")

        # 第一次部分冲销 5.00（"5" 规范化为 "5.00"）
        status, rev = self._request(
            "POST", f"/api/v1/payments/{payment_id}/reversals", {"amount": "5"}
        )
        self.assertEqual(status, 201, rev)
        self.assertEqual(rev["payment_id"], payment_id)
        self.assertEqual(rev["invoice_id"], invoice_id)
        self.assertEqual(rev["amount"], "5.00")
        self.assertEqual(rev["currency"], "USD")
        self.assertIn("created_at", rev)
        self.assertEqual(rev["reversed_total"], "5.00")
        self.assertEqual(rev["net_amount"], "15.00")

        # 单张收款查询：流水显示已冲销与净入账，余额按净入账额回升
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(status, 200)
        self.assertEqual(ov["paid_total"], "15.00")
        self.assertEqual(ov["reversed_total"], "5.00")
        self.assertEqual(ov["outstanding"], "18.11")
        self.assertEqual(ov["payment_count"], 1)
        p0 = ov["payments"][0]
        self.assertEqual(p0["amount"], "20.00")
        self.assertEqual(p0["reversed_total"], "5.00")
        self.assertEqual(p0["net_amount"], "15.00")

        # 第二次冲销 15.00：同一笔分次冲销，恰好冲完
        status, rev = self._request(
            "POST", f"/api/v1/payments/{payment_id}/reversals", {"amount": "15.00"}
        )
        self.assertEqual(status, 201, rev)
        self.assertEqual(rev["reversed_total"], "20.00")
        self.assertEqual(rev["net_amount"], "0.00")

        # 已冲完，再冲 0.01 -> 422 超额，且不写入
        status, body = self._request(
            "POST", f"/api/v1/payments/{payment_id}/reversals", {"amount": "0.01"}
        )
        self.assertEqual(status, 422)
        self.assertIn("detail", body)
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(ov["reversed_total"], "20.00")
        self.assertEqual(ov["paid_total"], "0.00")
        self.assertEqual(ov["outstanding"], "33.11")
        # 原收款流水仍在（冲销不删除原收款）
        self.assertEqual(ov["payment_count"], 1)
        self.assertEqual(ov["payments"][0]["amount"], "20.00")
        self.assertEqual(ov["payments"][0]["net_amount"], "0.00")

        # 冲销后余额回升，可按净入账额再次收满
        status, pay2 = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "33.11"}
        )
        self.assertEqual(status, 201, pay2)
        self.assertEqual(pay2["paid_total"], "33.11")
        self.assertEqual(pay2["outstanding"], "0.00")

        # 0 位小数币种：JPY 收款 1000，冲销 600 -> 净入账 400，金额不带小数位
        status, inv_j = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "JPY", "lines": [{"net": "1000", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv_j)
        status, pay_j = self._request(
            "POST", f"/api/v1/invoices/{inv_j['id']}/payments", {"amount": "1000"}
        )
        self.assertEqual(status, 201, pay_j)
        status, rev_j = self._request(
            "POST", f"/api/v1/payments/{pay_j['id']}/reversals", {"amount": "600"}
        )
        self.assertEqual(status, 201, rev_j)
        self.assertEqual(rev_j["amount"], "600")
        self.assertEqual(rev_j["reversed_total"], "600")
        self.assertEqual(rev_j["net_amount"], "400")
        # JPY 最小单位是 1，冲销 0.5 必须拒绝
        status, _ = self._request(
            "POST", f"/api/v1/payments/{pay_j['id']}/reversals", {"amount": "0.5"}
        )
        self.assertEqual(status, 422)

    def test_16_reversal_validation(self) -> None:
        # 未知流水 -> 404
        status, body = self._request(
            "POST", "/api/v1/payments/999999/reversals", {"amount": "1.00"}
        )
        self.assertEqual(status, 404)
        self.assertIn("detail", body)

        # 建发票并收款 10.00
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "10.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "10.00"}
        )
        self.assertEqual(status, 201, pay)
        path = f"/api/v1/payments/{pay['id']}/reversals"

        # 超额（累计冲销 > 原收款 10.00）
        status, _ = self._request("POST", path, {"amount": "10.01"})
        self.assertEqual(status, 422)
        # 零 / 负数
        status, _ = self._request("POST", path, {"amount": "0"})
        self.assertEqual(status, 422)
        status, _ = self._request("POST", path, {"amount": "-5.00"})
        self.assertEqual(status, 422)
        # 非十进制字符串 / 非字符串
        status, _ = self._request("POST", path, {"amount": "abc"})
        self.assertEqual(status, 422)
        status, _ = self._request("POST", path, {"amount": 1.5})
        self.assertEqual(status, 422)
        # 小于币种最小单位
        status, _ = self._request("POST", path, {"amount": "0.001"})
        self.assertEqual(status, 422)

        # 以上全部失败，不得留下任何冲销记录
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(ov["reversed_total"], "0.00")
        self.assertEqual(ov["paid_total"], "10.00")
        self.assertEqual(ov["outstanding"], "0.00")
        self.assertEqual(ov["payments"][0]["reversed_total"], "0.00")
        self.assertEqual(ov["payments"][0]["net_amount"], "10.00")

    def test_17_reversal_affects_overdue_and_allocation(self) -> None:
        # 发票 100.00、已到期：收满后不逾期；冲销 40.00 后重新逾期；
        # 后续来款分配按净入账额再收满
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {
                "currency": "USD",
                "due_date": "2026-09-01",
                "lines": [{"net": "100.00", "tax_rate": "0"}],
            },
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "100.00"}
        )
        self.assertEqual(status, 201, pay)
        payment_id = pay["id"]

        def overdue_by_id() -> dict:
            status, body = self._request(
                "GET", "/api/v1/invoices/overdue?as_of=2026-09-23&page_size=100"
            )
            self.assertEqual(status, 200)
            return {it["id"]: it for it in body["items"]}

        # 已收满：不逾期
        self.assertNotIn(invoice_id, overdue_by_id())

        # 冲销 40.00 -> 净入账 60.00，未收余额 40.00，重新进入逾期列表
        status, rev = self._request(
            "POST", f"/api/v1/payments/{payment_id}/reversals", {"amount": "40.00"}
        )
        self.assertEqual(status, 201, rev)
        item = overdue_by_id()[invoice_id]
        self.assertEqual(item["paid_total"], "60.00")
        self.assertEqual(item["outstanding"], "40.00")

        # 后续来款分配按净入账额计算：恰好再分配 40.00 收满
        status, body = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "40.00", "invoice_ids": [invoice_id]},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["allocations"][0]["allocated"], "40.00")
        self.assertEqual(body["allocations"][0]["outstanding"], "0.00")
        self.assertNotIn(invoice_id, overdue_by_id())

        # 余额已按净额算清，多分配 0.01 也整笔拒绝
        status, _ = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "0.01", "invoice_ids": [invoice_id]},
        )
        self.assertEqual(status, 422)

    def test_18_concurrent_reversals_no_overreverse(self) -> None:
        # 原收款 20.00：10 个并发冲销各 3.00，
        # 同事务校验+写入，恰好 6 笔成功（累计 18.00）、4 笔 422，不超原收款
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "20.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "20.00"}
        )
        self.assertEqual(status, 201, pay)
        payment_id = pay["id"]

        codes: list[int] = []
        lock = threading.Lock()

        def reverse() -> None:
            code, _ = self._request(
                "POST", f"/api/v1/payments/{payment_id}/reversals", {"amount": "3.00"}
            )
            with lock:
                codes.append(code)

        threads = [threading.Thread(target=reverse) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(codes), [201] * 6 + [422] * 4)
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(ov["reversed_total"], "18.00")
        self.assertEqual(ov["paid_total"], "2.00")
        self.assertEqual(ov["outstanding"], "18.00")

    def test_19_payment_snapshot(self) -> None:
        import sqlite3
        from decimal import Decimal

        # total_gross = 33.11
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {
                "currency": "USD",
                "lines": [
                    {"net": "10.05", "tax_rate": "0.10"},
                    {"net": "20.05", "tax_rate": "0.10"},
                ],
            },
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]

        # 直接改 SQLite 中流水的秒级时间戳，构造确定性的历史序列
        db_file = os.environ["DB_PATH"]

        def set_time(table: str, row_id: int, ts: str) -> None:
            # 两张流水表均以 id 为主键；row_id 一律传该行自己的 id
            c = sqlite3.connect(db_file, timeout=30)
            try:
                c.execute(
                    f"UPDATE {table} SET created_at = ? WHERE id = ?",
                    (ts, row_id),
                )
                c.commit()
            finally:
                c.close()

        def set_invoice_created(ts: str) -> None:
            c = sqlite3.connect(db_file, timeout=30)
            try:
                c.execute(
                    "UPDATE invoices SET created_at = ? WHERE id = ?",
                    (ts, invoice_id),
                )
                c.commit()
            finally:
                c.close()

        created = "2026-01-01T00:00:00Z"
        set_invoice_created(created)

        t1 = "2026-02-01T10:00:00Z"
        t2 = "2026-02-02T10:00:00Z"
        t3 = "2026-02-03T10:00:00Z"
        t4 = "2026-02-04T10:00:00Z"

        # 收款 20.00（t1）
        status, pay1 = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "20.00"}
        )
        self.assertEqual(status, 201, pay1)
        pid1 = pay1["id"]
        set_time("payments", pid1, t1)
        # 冲销 5.00（t2）
        status, rev1 = self._request(
            "POST", f"/api/v1/payments/{pid1}/reversals", {"amount": "5.00"}
        )
        self.assertEqual(status, 201, rev1)
        rid1 = rev1["id"]
        set_time("payment_reversals", rid1, t2)
        # 再收款 13.11（t3），净额 20-5+13.11 = 28.11，收满在冲销后
        status, pay2 = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "13.11"}
        )
        self.assertEqual(status, 201, pay2)
        pid2 = pay2["id"]
        set_time("payments", pid2, t3)

        def snap(at: str):
            return self._request(
                "GET",
                f"/api/v1/invoices/{invoice_id}/payment-snapshot"
                f"?at={urllib.parse.quote(at, safe='')}",
            )

        # 时点 == 发票创建：无任何流水
        status, s = snap(created)
        self.assertEqual(status, 200, s)
        self.assertEqual(s["at"], created)
        self.assertEqual(s["invoice_created_at"], created)
        self.assertEqual(s["total_gross"], "33.11")
        self.assertEqual(s["received_total"], "0.00")
        self.assertEqual(s["reversed_total"], "0.00")
        self.assertEqual(s["net_paid"], "0.00")
        self.assertEqual(s["outstanding"], "33.11")
        self.assertEqual(s["payment_count"], 0)
        self.assertEqual(s["reversal_count"], 0)
        self.assertEqual(s["payments"], [])
        self.assertEqual(s["reversals"], [])

        # 早于发票创建 -> 422
        status, body = snap("2025-12-31T23:59:59Z")
        self.assertEqual(status, 422)
        self.assertIn("detail", body)

        # t1 之前一秒：无流水
        status, s = snap("2026-02-01T09:59:59Z")
        self.assertEqual(status, 200)
        self.assertEqual(s["received_total"], "0.00")
        self.assertEqual(s["outstanding"], "33.11")

        # t1 当秒（闭区间）：收款 20 计入，冲销不计入
        status, s = snap(t1)
        self.assertEqual(status, 200, s)
        self.assertEqual(s["received_total"], "20.00")
        self.assertEqual(s["reversed_total"], "0.00")
        self.assertEqual(s["net_paid"], "20.00")
        self.assertEqual(s["outstanding"], "13.11")
        self.assertEqual(s["payment_count"], 1)
        self.assertEqual(s["reversal_count"], 0)
        self.assertEqual(
            [(p["id"], p["reversed_total"], p["net_amount"]) for p in s["payments"]],
            [(pid1, "0.00", "20.00")],
        )

        # t2 当秒：冲销 5 计入（同秒闭区间）
        status, s = snap(t2)
        self.assertEqual(status, 200, s)
        self.assertEqual(s["received_total"], "20.00")
        self.assertEqual(s["reversed_total"], "5.00")
        self.assertEqual(s["net_paid"], "15.00")
        self.assertEqual(s["outstanding"], "18.11")
        self.assertEqual(s["payment_count"], 1)
        self.assertEqual(s["reversal_count"], 1)
        self.assertEqual(
            [(r["id"], r["payment_id"], r["amount"]) for r in s["reversals"]],
            [(rid1, pid1, "5.00")],
        )
        p0 = s["payments"][0]
        self.assertEqual(p0["reversed_total"], "5.00")
        self.assertEqual(p0["net_amount"], "15.00")

        # t3 当秒：第二笔收款计入
        status, s = snap(t3)
        self.assertEqual(status, 200, s)
        self.assertEqual(s["received_total"], "33.11")
        self.assertEqual(s["reversed_total"], "5.00")
        self.assertEqual(s["net_paid"], "28.11")
        self.assertEqual(s["outstanding"], "5.00")
        self.assertEqual(s["payment_count"], 2)
        self.assertEqual([p["id"] for p in s["payments"]], [pid1, pid2])

        # ---- 截止后的流水不得改变历史结果 ----
        # 对 pid2 冲销 13.11（t4），随后再查 t3 的快照必须与之前完全一致
        status, rev2 = self._request(
            "POST", f"/api/v1/payments/{pid2}/reversals", {"amount": "13.11"}
        )
        self.assertEqual(status, 201, rev2)
        set_time("payment_reversals", rev2["id"], t4)

        status, s = snap(t3)
        self.assertEqual(status, 200, s)
        self.assertEqual(s["received_total"], "33.11")
        self.assertEqual(s["reversed_total"], "5.00")
        self.assertEqual(s["net_paid"], "28.11")
        self.assertEqual(s["outstanding"], "5.00")
        self.assertEqual(s["reversal_count"], 1)
        self.assertEqual(
            [p["net_amount"] for p in s["payments"]], ["15.00", "13.11"]
        )

        # t4 当秒：第二笔冲销计入，净已收 15.00，未收回到 18.11
        status, s = snap(t4)
        self.assertEqual(status, 200, s)
        self.assertEqual(s["received_total"], "33.11")
        self.assertEqual(s["reversed_total"], "18.11")
        self.assertEqual(s["net_paid"], "15.00")
        self.assertEqual(s["outstanding"], "18.11")
        self.assertEqual(s["reversal_count"], 2)

        # 未来时点：与当前全量一致
        status, s_now = snap("2030-01-01T00:00:00Z")
        self.assertEqual(status, 200, s_now)
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(status, 200)
        self.assertEqual(s_now["received_total"], "33.11")
        self.assertEqual(s_now["reversed_total"], ov["reversed_total"])
        self.assertEqual(s_now["net_paid"], ov["paid_total"])
        self.assertEqual(s_now["outstanding"], ov["outstanding"])
        self.assertEqual(s_now["payment_count"], ov["payment_count"])

        # 必填参数缺失 -> 422
        status, _ = self._request(
            "GET", f"/api/v1/invoices/{invoice_id}/payment-snapshot"
        )
        self.assertEqual(status, 422)

        # 时间格式错误 -> 422
        for bad in (
            "2026-02-01T10:00:00+00:00",  # 偏移而非 Z
            "2026-02-01 10:00:00",        # 空格分隔
            "2026-02-01T10:00:00.000Z",   # 毫秒
            "2026-02-01T10:00Z",          # 缺秒
            "2026-02-01",                 # 仅日期
            "2026-13-01T10:00:00Z",       # 月份不存在
            "2026-02-30T10:00:00Z",       # 日期不存在
            "not-a-time",
        ):
            status, resp = snap(bad)
            self.assertEqual(status, 422, bad)
            self.assertIn("detail", resp)

        # 发票不存在 -> 404（即使时间合法/非法均按 404 处理）
        status, body = self._request(
            "GET", "/api/v1/invoices/999999/payment-snapshot?at=2030-01-01T00:00:00Z"
        )
        self.assertEqual(status, 404)
        self.assertIn("detail", body)

    def test_20_payment_snapshot_jpy(self) -> None:
        # 0 位小数币种：快照金额不带小数位
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "JPY", "lines": [{"net": "1000", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        created = inv["created_at"]

        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "1000"}
        )
        self.assertEqual(status, 201, pay)
        status, rev = self._request(
            "POST", f"/api/v1/payments/{pay['id']}/reversals", {"amount": "600"}
        )
        self.assertEqual(status, 201, rev)

        status, s = self._request(
            "GET",
            f"/api/v1/invoices/{invoice_id}/payment-snapshot?at=2030-01-01T00:00:00Z",
        )
        self.assertEqual(status, 200, s)
        self.assertEqual(s["currency"], "JPY")
        self.assertEqual(s["total_gross"], "1000")
        self.assertEqual(s["received_total"], "1000")
        self.assertEqual(s["reversed_total"], "600")
        self.assertEqual(s["net_paid"], "400")
        self.assertEqual(s["outstanding"], "600")
        # 快照时点不得早于发票创建
        self.assertEqual(s["invoice_created_at"], created)

    def test_21_installment_plan_create_and_get(self) -> None:
        # total_gross = 33.11 USD，3 期：10.00 / 13.11 / 10.00
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {
                "currency": "USD",
                "lines": [
                    {"net": "10.05", "tax_rate": "0.10"},
                    {"net": "20.05", "tax_rate": "0.10"},
                ],
            },
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        path = f"/api/v1/invoices/{invoice_id}/installment-plan"
        payload = {
            "installments": [
                {"due_date": "2026-08-01", "amount": "10.00"},
                {"due_date": "2026-09-01", "amount": "13.11"},
                {"due_date": "2026-10-15", "amount": "10.00"},
            ]
        }

        status, plan = self._request("POST", path, payload)
        self.assertEqual(status, 201, plan)
        self.assertEqual(plan["invoice_id"], invoice_id)
        self.assertIn("plan_id", plan)
        self.assertIn("created_at", plan)
        # 创建响应的 as_of 取 UTC 当前日期（不写死具体日期，避免随时间失效）
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        self.assertEqual(plan["as_of"], today)
        self.assertEqual(plan["currency"], "USD")
        self.assertEqual(plan["total_gross"], "33.11")
        self.assertEqual(plan["net_paid"], "0.00")
        self.assertEqual(plan["covered_total"], "0.00")
        self.assertEqual(plan["uncovered_total"], "33.11")
        # 创建时 as_of 为 UTC 当日（2026-09-01 之后、2026-10-15 之前）：前两期已到期
        self.assertEqual(plan["overdue_total"], "23.11")
        self.assertEqual(
            [(it["installment"], it["due_date"], it["amount"],
              it["covered"], it["uncovered"], it["overdue"])
             for it in plan["installments"]],
            [
                (1, "2026-08-01", "10.00", "0.00", "10.00", "10.00"),
                (2, "2026-09-01", "13.11", "0.00", "13.11", "13.11"),
                (3, "2026-10-15", "10.00", "0.00", "10.00", "0.00"),
            ],
        )

        # GET 与 POST 返回同构视图（用同一 as_of 显式查询）
        status, fetched = self._request("GET", f"{path}?as_of={today}")
        self.assertEqual(status, 200, fetched)
        self.assertEqual(fetched, plan)

        # 到期日当天不算逾期：as_of=2026-08-01 时第 1 期 overdue=0
        status, at_due = self._request("GET", f"{path}?as_of=2026-08-01")
        self.assertEqual(status, 200)
        self.assertEqual(at_due["overdue_total"], "0.00")
        self.assertEqual(at_due["installments"][0]["overdue"], "0.00")

        # 一天之后：第 1 期全额逾期
        status, after_due = self._request("GET", f"{path}?as_of=2026-08-02")
        self.assertEqual(status, 200)
        self.assertEqual(after_due["overdue_total"], "10.00")

        # 全部到期后：所有未覆盖额都逾期
        status, late = self._request("GET", f"{path}?as_of=2030-01-01")
        self.assertEqual(late["overdue_total"], "33.11")

        # 期次合计 + 覆盖汇总恒等
        from decimal import Decimal
        for body in (at_due, after_due, late):
            self.assertEqual(
                sum(Decimal(it["amount"]) for it in body["installments"]),
                Decimal("33.11"),
            )
            self.assertEqual(
                Decimal(body["covered_total"]) + Decimal(body["uncovered_total"]),
                Decimal(body["total_gross"]),
            )

        # 缺省 as_of：回显 UTC 当日
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        status, default_view = self._request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(default_view["as_of"], today)

    def test_22_installment_plan_fifo_coverage(self) -> None:
        # 3 期 10.00 / 13.11 / 10.00 = 33.11；收款逐期 FIFO 抵扣
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {
                "currency": "USD",
                "lines": [
                    {"net": "10.05", "tax_rate": "0.10"},
                    {"net": "20.05", "tax_rate": "0.10"},
                ],
            },
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        path = f"/api/v1/invoices/{invoice_id}/installment-plan"
        status, _ = self._request(
            "POST",
            path,
            {
                "installments": [
                    {"due_date": "2026-08-01", "amount": "10.00"},
                    {"due_date": "2026-09-01", "amount": "13.11"},
                    {"due_date": "2026-10-15", "amount": "10.00"},
                ]
            },
        )
        self.assertEqual(status, 201)

        def view(as_of: str = "2026-09-24"):
            status, body = self._request("GET", f"{path}?as_of={as_of}")
            self.assertEqual(status, 200, body)
            return body

        # 收款 20.00：第 1 期覆盖满，第 2 期覆盖 10.00/未覆盖 3.11
        status, _ = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "20.00"}
        )
        self.assertEqual(status, 201)
        v = view()
        self.assertEqual(v["net_paid"], "20.00")
        self.assertEqual(v["covered_total"], "20.00")
        self.assertEqual(v["uncovered_total"], "13.11")
        # 仅第 2 期未覆盖的 3.11 逾期（第 1 期已覆盖，第 3 期未到期）
        self.assertEqual(v["overdue_total"], "3.11")
        self.assertEqual(
            [(it["covered"], it["uncovered"], it["overdue"])
             for it in v["installments"]],
            [("10.00", "0.00", "0.00"),
             ("10.00", "3.11", "3.11"),
             ("0.00", "10.00", "0.00")],
        )

        # 再收 13.11 恰好收满：全部覆盖，逾期归零
        status, _ = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "13.11"}
        )
        self.assertEqual(status, 201)
        v = view("2030-01-01")
        self.assertEqual(v["net_paid"], "33.11")
        self.assertEqual(v["covered_total"], "33.11")
        self.assertEqual(v["uncovered_total"], "0.00")
        self.assertEqual(v["overdue_total"], "0.00")
        self.assertTrue(all(it["covered"] == it["amount"] for it in v["installments"]))

    def test_23_installment_plan_reversal_reopens(self) -> None:
        # 收满后冲销：净已收回降，后期次重新打开；原计划期次/金额不变
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "100.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        path = f"/api/v1/invoices/{invoice_id}/installment-plan"
        status, created = self._request(
            "POST",
            path,
            {
                "installments": [
                    {"due_date": "2026-08-01", "amount": "40.00"},
                    {"due_date": "2026-09-01", "amount": "60.00"},
                ]
            },
        )
        self.assertEqual(status, 201, created)

        # 来款分配也走 payments 表，计划抵扣必须可见
        status, alloc = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "100.00", "invoice_ids": [invoice_id]},
        )
        self.assertEqual(status, 201, alloc)
        payment_id = alloc["allocations"][0]["payment_id"]

        status, full = self._request("GET", f"{path}?as_of=2026-09-24")
        self.assertEqual(status, 200)
        self.assertEqual(full["covered_total"], "100.00")
        self.assertEqual(full["uncovered_total"], "0.00")
        self.assertEqual(full["overdue_total"], "0.00")

        # 冲销 70.00：净已收 30.00 -> 第 1 期覆盖 30/未覆盖 10，第 2 期完全重新打开
        status, rev = self._request(
            "POST",
            f"/api/v1/payments/{payment_id}/reversals",
            {"amount": "70.00"},
        )
        self.assertEqual(status, 201, rev)
        status, reopened = self._request("GET", f"{path}?as_of=2026-09-24")
        self.assertEqual(status, 200, reopened)
        self.assertEqual(reopened["net_paid"], "30.00")
        self.assertEqual(reopened["covered_total"], "30.00")
        self.assertEqual(reopened["uncovered_total"], "70.00")
        self.assertEqual(reopened["overdue_total"], "70.00")
        self.assertEqual(
            [(it["installment"], it["amount"], it["covered"], it["uncovered"])
             for it in reopened["installments"]],
            [(1, "40.00", "30.00", "10.00"), (2, "60.00", "0.00", "60.00")],
        )
        # 原计划行未被改写：期次号、到期日、金额与创建时一致
        self.assertEqual(
            [(it["installment"], it["due_date"], it["amount"])
             for it in reopened["installments"]],
            [(it["installment"], it["due_date"], it["amount"])
             for it in created["installments"]],
        )

    def test_24_installment_plan_validation(self) -> None:
        # gross 10.00 USD
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "10.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        path = f"/api/v1/invoices/{invoice_id}/installment-plan"

        def post(installments):
            return self._request("POST", path, {"installments": installments})

        # 期次合计不等于含税总额 -> 422
        status, body = post([{"due_date": "2026-08-01", "amount": "9.00"}])
        self.assertEqual(status, 422, body)
        self.assertIn("含税总额", body["detail"])

        # 合计相等但存在 0 / 负数金额 -> 422
        for bad in ("0", "-1.00"):
            status, _ = post(
                [{"due_date": "2026-08-01", "amount": bad},
                 {"due_date": "2026-09-01", "amount": "10.00"}]
            )
            self.assertEqual(status, 422, bad)

        # 小于币种最小单位 -> 422
        status, _ = post(
            [{"due_date": "2026-08-01", "amount": "9.999"},
             {"due_date": "2026-09-01", "amount": "0.001"}]
        )
        self.assertEqual(status, 422)

        # 到期日未按升序排列 -> 422（pydantic 校验，detail 为错误列表）
        status, body = post(
            [{"due_date": "2026-09-01", "amount": "5.00"},
             {"due_date": "2026-08-01", "amount": "5.00"}]
        )
        self.assertEqual(status, 422)
        self.assertIn("升序", json.dumps(body, ensure_ascii=False))

        # 期次为空 -> 422
        status, _ = post([])
        self.assertEqual(status, 422)

        # 到期日格式非法（带时间 / 不存在的日期）-> 422
        for bad in ("2026-08", "2026-08-01T00:00:00Z", "2026/08/01", "not-a-date"):
            status, _ = post([{"due_date": bad, "amount": "10.00"}])
            self.assertEqual(status, 422, bad)

        # 所有失败都不得创建计划
        status, _ = self._request("GET", path)
        self.assertEqual(status, 404)

        # 成功创建一份
        status, plan = post(
            [{"due_date": "2026-08-01", "amount": "4.00"},
             {"due_date": "2026-08-01", "amount": "6.00"}]  # 同日多期允许
        )
        self.assertEqual(status, 201, plan)

        # 每张发票只允许一份：再次创建 -> 409
        status, body = post([{"due_date": "2026-12-01", "amount": "10.00"}])
        self.assertEqual(status, 409)
        self.assertIn("只允许一份", body["detail"])

        # 发票不存在：创建 404、查询 404
        status, _ = self._request(
            "POST",
            "/api/v1/invoices/999999/installment-plan",
            {"installments": [{"due_date": "2026-08-01", "amount": "1.00"}]},
        )
        self.assertEqual(status, 404)
        status, body = self._request(
            "GET", "/api/v1/invoices/999999/installment-plan"
        )
        self.assertEqual(status, 404)
        self.assertIn("不存在", body["detail"])

        # 查询时 as_of 非法 -> 422
        status, _ = self._request("GET", f"{path}?as_of=2026-13-40")
        self.assertEqual(status, 422)

    def test_25_installment_plan_jpy_and_concurrent(self) -> None:
        # 0 位小数币种：金额不带小数位，非整数最小单位（0.5）被拒绝
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "JPY", "lines": [{"net": "1000", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv)
        jpy_id = inv["id"]
        path = f"/api/v1/invoices/{jpy_id}/installment-plan"
        status, body = self._request(
            "POST",
            path,
            {
                "installments": [
                    {"due_date": "2026-08-01", "amount": "400"},
                    {"due_date": "2026-09-01", "amount": "600"},
                ]
            },
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["total_gross"], "1000")
        self.assertEqual(body["uncovered_total"], "1000")
        self.assertEqual(body["installments"][0]["amount"], "400")

        status, _ = self._request(
            "POST", f"/api/v1/invoices/{jpy_id}/payments", {"amount": "500"}
        )
        self.assertEqual(status, 201)
        status, v = self._request("GET", f"{path}?as_of=2026-09-24")
        self.assertEqual(status, 200)
        self.assertEqual(v["net_paid"], "500")
        self.assertEqual(
            [(it["covered"], it["uncovered"], it["overdue"])
             for it in v["installments"]],
            [("400", "0", "0"), ("100", "500", "500")],
        )

        # 并发为同一张发票创建计划：唯一约束 + BEGIN IMMEDIATE，只允许一份
        status, inv2 = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "10.00", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, inv2)
        target = inv2["id"]
        target_path = f"/api/v1/invoices/{target}/installment-plan"
        payload = {"installments": [{"due_date": "2026-08-01", "amount": "10.00"}]}

        codes: list[int] = []
        lock = threading.Lock()

        def create() -> None:
            code, _ = self._request("POST", target_path, payload)
            with lock:
                codes.append(code)

        threads = [threading.Thread(target=create) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(codes), [201] + [409] * 7)

    def test_26_invoice_number_check_duplicates(self) -> None:
        payload = {
            "items": [
                {"supplier_code": "SUP-A", "invoice_number": "INV-001"},
                {"supplier_code": "sup-a", "invoice_number": " inv-001 "},
                {"supplier_code": "SUP-A", "invoice_number": "INV-002"},
                {"supplier_code": "SUP-B", "invoice_number": "INV-001"},
            ]
        }
        status, body = self._request("POST", "/api/v1/invoice-number-check", payload)
        self.assertEqual(status, 200, body)
        self.assertEqual(
            body,
            {
                "items": [
                    {
                        "supplier_code": "SUP-A",
                        "normalized_invoice_number": "inv-001",
                        "positions": [1, 2],
                        "original_invoice_numbers": ["INV-001", " inv-001 "],
                    }
                ]
            },
        )

    def test_27_invoice_number_check_normalization(self) -> None:
        # NFKC（全角）+ 大小写折叠（Kelvin 符号 K → k；ß casefold → ss）
        payload = {
            "items": [
                {"supplier_code": "ＳＵＰ－１", "invoice_number": "ＡＢＣ１２３"},
                {"supplier_code": "SUP-1", "invoice_number": "abc123"},
                {"supplier_code": "x", "invoice_number": "STRAßE"},
                {"supplier_code": "x", "invoice_number": "strasse"},
            ]
        }
        status, body = self._request("POST", "/api/v1/invoice-number-check", payload)
        self.assertEqual(status, 200, body)
        self.assertEqual(
            [(g["normalized_invoice_number"], g["positions"]) for g in body["items"]],
            [("abc123", [1, 2]), ("strasse", [3, 4])],
        )

    def test_28_invoice_number_check_group_ordering(self) -> None:
        # 重复组按组内最早输入位置升序；同号码跨供应商不算重复
        payload = {
            "items": [
                {"supplier_code": "a", "invoice_number": "z"},
                {"supplier_code": "b", "invoice_number": "m"},
                {"supplier_code": "a", "invoice_number": "z"},
                {"supplier_code": "b", "invoice_number": "n"},
                {"supplier_code": "b", "invoice_number": "m"},
                {"supplier_code": "a", "invoice_number": "x"},
                {"supplier_code": "a", "invoice_number": "x"},
            ]
        }
        status, body = self._request("POST", "/api/v1/invoice-number-check", payload)
        self.assertEqual(status, 200, body)
        self.assertEqual(
            [(g["supplier_code"], g["positions"]) for g in body["items"]],
            [("a", [1, 3]), ("b", [2, 5]), ("a", [6, 7])],
        )
        # supplier_code 取首次出现位置的原始值
        self.assertEqual(
            [g["original_invoice_numbers"] for g in body["items"]],
            [["z", "z"], ["m", "m"], ["x", "x"]],
        )

    def test_29_invoice_number_check_no_duplicates(self) -> None:
        payload = {
            "items": [
                {"supplier_code": "a", "invoice_number": "1"},
                {"supplier_code": "b", "invoice_number": "1"},
                {"supplier_code": "a", "invoice_number": "2"},
            ]
        }
        status, body = self._request("POST", "/api/v1/invoice-number-check", payload)
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"items": []})

    def test_30_invoice_number_check_validation(self) -> None:
        path = "/api/v1/invoice-number-check"

        # 空批次
        status, body = self._request("POST", path, {"items": []})
        self.assertEqual(status, 422, body)

        # 超过 500 条
        big = {
            "items": [
                {"supplier_code": "s", "invoice_number": f"n{i}"} for i in range(501)
            ]
        }
        status, body = self._request("POST", path, big)
        self.assertEqual(status, 422, body)

        # 原始发票号码为空 / 纯空白（NFKC 后为空）
        for bad_number in ("", "   ", "\t\n", "\u3000"):  # 全角空格 NFKC 后为空
            status, body = self._request(
                "POST",
                path,
                {"items": [{"supplier_code": "s", "invoice_number": bad_number}]},
            )
            self.assertEqual(status, 422, (bad_number, body))

        # 供应商编号为空
        status, body = self._request(
            "POST",
            path,
            {"items": [{"supplier_code": "  ", "invoice_number": "n"}]},
        )
        self.assertEqual(status, 422, body)

        # 原始号码超过 100 个 Unicode 字符（每个 emoji 算 1 个字符）
        status, body = self._request(
            "POST",
            path,
            {"items": [{"supplier_code": "s", "invoice_number": "😀" * 101}]},
        )
        self.assertEqual(status, 422, body)

        # 恰好 100 个字符合法
        status, body = self._request(
            "POST",
            path,
            {"items": [{"supplier_code": "s", "invoice_number": "a" * 100}]},
        )
        self.assertEqual(status, 200, body)

        # 缺字段
        status, body = self._request(
            "POST", path, {"items": [{"supplier_code": "s"}]}
        )
        self.assertEqual(status, 422, body)

    def test_31_credit_memo_create_and_list(self) -> None:
        # USD 发票两行 13%：l1 net 10.00 -> tax 1.30；l2 net 3.34 -> tax 0.43
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [
                {"net": "10.00", "tax_rate": "0.13"},
                {"net": "3.34", "tax_rate": "0.13"},
            ]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        self.assertEqual(
            [(l["line_no"], l["net"], l["tax"]) for l in inv["lines"]],
            [(1, "10.00", "1.30"), (2, "3.34", "0.43")],
        )
        path = f"/api/v1/invoices/{invoice_id}/credit-memos"

        # 初始查询：无凭证
        status, ov = self._request("GET", path)
        self.assertEqual(status, 200, ov)
        self.assertEqual(ov["invoice_id"], invoice_id)
        self.assertEqual(ov["currency"], "USD")
        self.assertEqual(ov["total_net"], "13.34")
        self.assertEqual(ov["total_tax"], "1.73")
        self.assertEqual(ov["total_gross"], "15.07")
        self.assertEqual(ov["credit_count"], 0)
        self.assertEqual(ov["credited_net"], "0.00")
        self.assertEqual(ov["credited_tax"], "0.00")
        self.assertEqual(ov["credited_gross"], "0.00")
        self.assertEqual(ov["line_credits"], [])
        self.assertEqual(ov["memos"], [])

        # 一张凭证含两行（故意把行号倒序提交，凭证行保持提交顺序）
        status, memo = self._request(
            "POST", path, {"lines": [
                {"line_no": 2, "net": "1.00"},
                {"line_no": 1, "net": "3.34"},
            ]},
        )
        self.assertEqual(status, 201, memo)
        self.assertEqual(memo["invoice_id"], invoice_id)
        self.assertEqual(memo["currency"], "USD")
        self.assertIn("id", memo)
        self.assertIn("created_at", memo)
        # l2: round(0.43*1/3.34)=round(0.1287...)=0.13；l1: round(1.30*3.34/10)=0.43
        self.assertEqual(
            memo["lines"],
            [
                {"line_no": 2, "net": "1.00", "tax": "0.13", "gross": "1.13",
                 "cumulative_net": "1.00", "cumulative_tax": "0.13"},
                {"line_no": 1, "net": "3.34", "tax": "0.43", "gross": "3.77",
                 "cumulative_net": "3.34", "cumulative_tax": "0.43"},
            ],
        )
        self.assertEqual(memo["net_total"], "4.34")
        self.assertEqual(memo["tax_total"], "0.56")
        self.assertEqual(memo["gross_total"], "4.90")

        # 查询接口：凭证按 id 升序，凭证行按行号升序；含逐原行累计
        status, ov = self._request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(ov["credit_count"], 1)
        self.assertEqual(ov["credited_net"], "4.34")
        self.assertEqual(ov["credited_tax"], "0.56")
        self.assertEqual(ov["credited_gross"], "4.90")
        self.assertEqual(
            ov["line_credits"],
            [
                {"line_no": 1, "credited_net": "3.34", "credited_tax": "0.43",
                 "credited_gross": "3.77", "remaining_net": "6.66"},
                {"line_no": 2, "credited_net": "1.00", "credited_tax": "0.13",
                 "credited_gross": "1.13", "remaining_net": "2.34"},
            ],
        )
        self.assertEqual([l["line_no"] for l in ov["memos"][0]["lines"]], [1, 2])
        self.assertEqual(ov["memos"][0]["id"], memo["id"])

        # 原发票保持原样：逐行净额/税额、汇总与创建时完全一致
        status, fetched = self._request("GET", f"/api/v1/invoices/{invoice_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["totals"], inv["totals"])
        self.assertEqual(
            [(l["line_no"], l["net"], l["tax"], l["gross"]) for l in fetched["lines"]],
            [(l["line_no"], l["net"], l["tax"], l["gross"]) for l in inv["lines"]],
        )
        self.assertTrue(fetched["recompute_matched"])

        # 收款流水保持原样：未收余额仍等于含税总额，收款查询看不到任何凭证影响
        status, payov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(status, 200)
        self.assertEqual(payov["total_gross"], "15.07")
        self.assertEqual(payov["paid_total"], "0.00")
        self.assertEqual(payov["outstanding"], "15.07")
        self.assertEqual(payov["payment_count"], 0)

        # 登记一笔收款后再抵扣：凭证不改变未收余额
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "5.00"}
        )
        self.assertEqual(status, 201, pay)
        self.assertEqual(pay["outstanding"], "10.07")
        status, ov = self._request("GET", path)
        self.assertEqual(ov["credit_count"], 1)
        status, payov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(payov["outstanding"], "10.07")

    def test_32_credit_memo_cumulative_and_full_offset(self) -> None:
        # l1 net 10.00 @13% -> tax 1.30，分三次抵：3.34 / 3.33 / 3.33
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "10.00", "tax_rate": "0.13"}]},
        )
        self.assertEqual(status, 201, inv)
        path = f"/api/v1/invoices/{inv['id']}/credit-memos"
        from decimal import Decimal

        status, m1 = self._request("POST", path, {"lines": [{"line_no": 1, "net": "3.34"}]})
        self.assertEqual(status, 201, m1)
        # round(1.30 * 3.34 / 10) = round(0.4342) = 0.43
        self.assertEqual(m1["lines"][0]["tax"], "0.43")
        self.assertEqual(m1["lines"][0]["cumulative_net"], "3.34")
        self.assertEqual(m1["lines"][0]["cumulative_tax"], "0.43")

        status, m2 = self._request("POST", path, {"lines": [{"line_no": 1, "net": "3.33"}]})
        self.assertEqual(status, 201, m2)
        # 累计 6.67：round(1.30*0.667)=round(0.8671)=0.87；本次 = 0.87-0.43 = 0.44
        self.assertEqual(m2["lines"][0]["tax"], "0.44")
        self.assertEqual(m2["lines"][0]["cumulative_net"], "6.67")
        self.assertEqual(m2["lines"][0]["cumulative_tax"], "0.87")

        # 最后一笔恰好抵完净额 10.00：累计税额必须恰好等于原行税额 1.30
        status, m3 = self._request("POST", path, {"lines": [{"line_no": 1, "net": "3.33"}]})
        self.assertEqual(status, 201, m3)
        self.assertEqual(m3["lines"][0]["net"], "3.33")
        self.assertEqual(m3["lines"][0]["cumulative_net"], "10.00")
        self.assertEqual(m3["lines"][0]["cumulative_tax"], "1.30")
        self.assertEqual(m3["lines"][0]["tax"], "0.43")
        self.assertEqual(m3["lines"][0]["gross"], "3.76")
        # 三行税额合计恰好抵完原行已确认税额
        self.assertEqual(
            sum(Decimal(m["lines"][0]["tax"]) for m in (m1, m2, m3)),
            Decimal("1.30"),
        )

        # 抵完后逐原行剩余净额为 0
        status, ov = self._request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(ov["credit_count"], 3)
        self.assertEqual(ov["line_credits"][0]["remaining_net"], "0.00")
        self.assertEqual(ov["line_credits"][0]["credited_tax"], "1.30")

        # 再多抵 0.01 -> 422，且不新增凭证
        status, body = self._request(
            "POST", path, {"lines": [{"line_no": 1, "net": "0.01"}]}
        )
        self.assertEqual(status, 422)
        self.assertIn("超过原行净额", body["detail"])
        status, ov = self._request("GET", path)
        self.assertEqual(ov["credit_count"], 3)

    def test_33_credit_memo_validation(self) -> None:
        # 两行发票：l1 正净额 10.00；l2 负净额 -5.00（冲红行，不可抵扣）
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [
                {"net": "10.00", "tax_rate": "0.13"},
                {"net": "-5.00", "tax_rate": "0.13"},
            ]},
        )
        self.assertEqual(status, 201, inv)
        invoice_id = inv["id"]
        path = f"/api/v1/invoices/{invoice_id}/credit-memos"

        def post(lines):
            return self._request("POST", path, {"lines": lines})

        # 引用不存在的原行
        status, body = post([{"line_no": 9, "net": "1.00"}])
        self.assertEqual(status, 422, body)
        self.assertIn("不存在行号 9", body["detail"])

        # 原行净额不为正（负净额冲红行）
        status, body = post([{"line_no": 2, "net": "0.01"}])
        self.assertEqual(status, 422, body)
        self.assertIn("不为正", body["detail"])

        # 本次抵扣净额 0 / 负数 / 非最小单位
        for bad in ("0", "0.000", "-1.00", "0.001"):
            status, _ = post([{"line_no": 1, "net": bad}])
            self.assertEqual(status, 422, bad)

        # 行号非正、非整数 -> 422（pydantic）
        for bad_no in (0, -1, 1.5):
            status, _ = post([{"line_no": bad_no, "net": "1.00"}])
            self.assertEqual(status, 422, bad_no)

        # 同一凭证内重复行号 -> 422
        status, body = post([
            {"line_no": 1, "net": "1.00"},
            {"line_no": 1, "net": "2.00"},
        ])
        self.assertEqual(status, 422, body)
        self.assertIn("重复", json.dumps(body, ensure_ascii=False))

        # 空明细 -> 422
        status, _ = post([])
        self.assertEqual(status, 422)

        # 累计超额：先抵 9.00，再抵 1.01 -> 422 且不写入第二张
        status, m = post([{"line_no": 1, "net": "9.00"}])
        self.assertEqual(status, 201, m)
        status, body = post([{"line_no": 1, "net": "1.01"}])
        self.assertEqual(status, 422)
        self.assertIn("超过原行净额", body["detail"])
        status, ov = self._request("GET", path)
        self.assertEqual(ov["credit_count"], 1)
        self.assertEqual(ov["credited_net"], "9.00")

        # 全部失败后原发票与分页列表不受影响（发票仍在、逐行金额不变）
        status, fetched = self._request("GET", f"/api/v1/invoices/{invoice_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["totals"], inv["totals"])

        # 发票不存在：创建 404、查询 404
        status, body = self._request(
            "POST",
            "/api/v1/invoices/999999/credit-memos",
            {"lines": [{"line_no": 1, "net": "1.00"}]},
        )
        self.assertEqual(status, 404)
        self.assertIn("不存在", body["detail"])
        status, body = self._request("GET", "/api/v1/invoices/999999/credit-memos")
        self.assertEqual(status, 404)

    def test_34_credit_memo_jpy_rounding(self) -> None:
        # JPY 0 位小数：net 1000 @10% -> tax 100；多次抵扣验证 HALF_UP 与完全抵完
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "JPY", "lines": [{"net": "1000", "tax_rate": "0.10"}]},
        )
        self.assertEqual(status, 201, inv)
        self.assertEqual(inv["lines"][0]["tax"], "100")
        path = f"/api/v1/invoices/{inv['id']}/credit-memos"

        status, m1 = self._request("POST", path, {"lines": [{"line_no": 1, "net": "333"}]})
        self.assertEqual(status, 201, m1)
        # round(100*333/1000)=round(33.3)=33
        self.assertEqual(m1["lines"][0]["tax"], "33")
        self.assertEqual(m1["lines"][0]["gross"], "366")
        self.assertEqual(m1["lines"][0]["cumulative_tax"], "33")

        status, m2 = self._request("POST", path, {"lines": [{"line_no": 1, "net": "333"}]})
        self.assertEqual(status, 201, m2)
        # 累计 666：round(66.6)=67；本次 = 67-33 = 34
        self.assertEqual(m2["lines"][0]["tax"], "34")
        self.assertEqual(m2["lines"][0]["cumulative_tax"], "67")

        # 完全抵扣：累计税额恰好 100，本次 33（67+33=100）
        status, m3 = self._request("POST", path, {"lines": [{"line_no": 1, "net": "334"}]})
        self.assertEqual(status, 201, m3)
        self.assertEqual(m3["lines"][0]["cumulative_net"], "1000")
        self.assertEqual(m3["lines"][0]["cumulative_tax"], "100")
        self.assertEqual(m3["lines"][0]["tax"], "33")

        # 0.5 不是 JPY 最小单位整数倍 -> 422
        status, body = self._request(
            "POST", path, {"lines": [{"line_no": 1, "net": "0.5"}]}
        )
        self.assertEqual(status, 422)
        self.assertIn("最小单位", body["detail"])

        # 汇总金额不带小数位
        status, ov = self._request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(ov["credited_net"], "1000")
        self.assertEqual(ov["credited_tax"], "100")
        self.assertEqual(ov["credited_gross"], "1100")
        self.assertEqual(ov["line_credits"][0]["remaining_net"], "0")

    def test_35_credit_memo_concurrent(self) -> None:
        # 原行净额 10.00：15 个并发抵扣各 1.00，BEGIN IMMEDIATE 串行化：
        # 恰好 10 笔成功、5 笔 422，累计抵扣净额 10.00、累计税额恰好 1.30
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "10.00", "tax_rate": "0.13"}]},
        )
        self.assertEqual(status, 201, inv)
        path = f"/api/v1/invoices/{inv['id']}/credit-memos"

        codes: list[int] = []
        taxes: list[str] = []
        lock = threading.Lock()

        def credit() -> None:
            code, body = self._request(
                "POST", path, {"lines": [{"line_no": 1, "net": "1.00"}]}
            )
            with lock:
                codes.append(code)
                if code == 201:
                    taxes.append(body["lines"][0]["tax"])

        from decimal import Decimal

        threads = [threading.Thread(target=credit) for _ in range(15)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(codes), [201] * 10 + [422] * 5)
        status, ov = self._request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(ov["credit_count"], 10)
        self.assertEqual(ov["credited_net"], "10.00")
        self.assertEqual(ov["credited_tax"], "1.30")
        self.assertEqual(ov["credited_gross"], "11.30")
        self.assertEqual(ov["line_credits"][0]["remaining_net"], "0.00")
        # 逐次税额之和恰好抵完原行已确认税额
        self.assertEqual(sum(Decimal(t) for t in taxes), Decimal("1.30"))

    def test_36_receivables_aging_buckets(self) -> None:
        # 用与历史用例隔离的币种（EUR 2 位、KRW 0 位），全部时间戳回拨到 2029，
        # 使 as_of=2030-01-15 的结果完全确定、不受真实当前时间影响
        import sqlite3
        from decimal import Decimal

        db_file = os.environ["DB_PATH"]

        def set_time(table: str, row_id: int, ts: str) -> None:
            c = sqlite3.connect(db_file, timeout=30)
            try:
                c.execute(
                    f"UPDATE {table} SET created_at = ? WHERE id = ?", (ts, row_id)
                )
                c.commit()
            finally:
                c.close()

        def eur_invoice(due: str | None, net: str = "100.00") -> int:
            status, body = self._request(
                "POST",
                "/api/v1/invoices",
                {"currency": "EUR", "due_date": due,
                 "lines": [{"net": net, "tax_rate": "0"}]},
            )
            self.assertEqual(status, 201, body)
            iid = body["id"]
            # 0% 税率：含税总额 == 净额
            self.assertEqual(body["totals"]["total_gross"], net)
            set_time("invoices", iid, "2029-09-01T00:00:00Z")
            return iid

        # as_of = 2030-01-15：逐桶边界（差 0/1/30/31/60/61/90/91/106 天）
        i_current = eur_invoice("2030-01-15")   # 到期当日 -> current
        i_d1 = eur_invoice("2030-01-14")        # 逾期 1 天（部分收款 40 -> 未收 60）
        i_d30 = eur_invoice("2029-12-16")       # 逾期 30 天
        i_d31 = eur_invoice("2029-12-15")       # 逾期 31 天
        i_d60 = eur_invoice("2029-11-16")       # 逾期 60 天
        i_d61 = eur_invoice("2029-11-15")       # 逾期 61 天
        i_d90 = eur_invoice("2029-10-17")       # 逾期 90 天
        i_d91 = eur_invoice("2029-10-16")       # 逾期 91 天
        i_d106 = eur_invoice("2029-10-01")      # 逾期 106 天
        i_nodue = eur_invoice(None)             # 无到期日单列
        i_paid = eur_invoice("2030-01-10")      # 已收满 -> 排除
        i_neg = eur_invoice("2030-01-10", "-10.00")  # 含税总额为负 -> 排除
        i_future = eur_invoice("2030-01-10")    # 查询日结束后才创建 -> 排除
        set_time("invoices", i_future, "2030-01-16T00:00:00Z")

        # 逾期 1 天的发票部分收款 40（截止时点之前）-> 未收 60
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{i_d1}/payments", {"amount": "40.00"}
        )
        self.assertEqual(status, 201, pay)
        set_time("payments", pay["id"], "2030-01-10T00:00:00Z")

        # 已收满的发票
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{i_paid}/payments", {"amount": "100.00"}
        )
        self.assertEqual(status, 201, pay)

        # KRW：0 位小数，金额字符串不带小数点
        status, krw = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "KRW", "lines": [{"net": "5000", "tax_rate": "0"}]},
        )
        self.assertEqual(status, 201, krw)
        self.assertEqual(krw["totals"]["total_gross"], "5000")
        set_time("invoices", krw["id"], "2029-09-01T00:00:00Z")

        def aging(as_of: str):
            return self._request(
                "GET", f"/api/v1/receivables/aging?as_of={as_of}"
            )

        status, body = aging("2030-01-15")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["as_of"], "2030-01-15")
        self.assertEqual(body["cutoff_at"], "2030-01-15T23:59:59Z")

        by_ccy = {c["currency"]: c for c in body["currencies"]}
        self.assertIn("EUR", by_ccy)
        self.assertIn("KRW", by_ccy)
        # 币种按代码升序：EUR 在 KRW 之前
        names = [c["currency"] for c in body["currencies"]]
        self.assertLess(names.index("EUR"), names.index("KRW"))

        eur = by_ccy["EUR"]
        self.assertEqual(eur["currency_digits"], 2)
        self.assertEqual(eur["invoice_count"], 10)
        self.assertEqual(eur["total_outstanding"], "960.00")
        bk = {b["bucket"]: b for b in eur["buckets"]}
        # 六个桶固定全部返回（空桶也为 0）
        self.assertEqual(
            [b["bucket"] for b in eur["buckets"]],
            [
                "no_due_date",
                "current",
                "overdue_1_30",
                "overdue_31_60",
                "overdue_61_90",
                "overdue_91_plus",
            ],
        )
        self.assertEqual(bk["no_due_date"],
                         {"bucket": "no_due_date", "invoice_count": 1, "outstanding": "100.00"})
        self.assertEqual(bk["current"],
                         {"bucket": "current", "invoice_count": 1, "outstanding": "100.00"})
        self.assertEqual(bk["overdue_1_30"],
                         {"bucket": "overdue_1_30", "invoice_count": 2, "outstanding": "160.00"})
        self.assertEqual(bk["overdue_31_60"],
                         {"bucket": "overdue_31_60", "invoice_count": 2, "outstanding": "200.00"})
        self.assertEqual(bk["overdue_61_90"],
                         {"bucket": "overdue_61_90", "invoice_count": 2, "outstanding": "200.00"})
        self.assertEqual(bk["overdue_91_plus"],
                         {"bucket": "overdue_91_plus", "invoice_count": 2, "outstanding": "200.00"})
        # 桶发票数之和 == 币种发票数；桶未收之和 == 币种总未收
        self.assertEqual(sum(b["invoice_count"] for b in eur["buckets"]), 10)
        self.assertEqual(
            str(sum(Decimal(b["outstanding"]) for b in eur["buckets"])), "960.00"
        )

        # KRW：0 位精度，无到期日
        krw_block = by_ccy["KRW"]
        self.assertEqual(krw_block["currency_digits"], 0)
        self.assertEqual(krw_block["invoice_count"], 1)
        self.assertEqual(krw_block["total_outstanding"], "5000")
        kb = {b["bucket"]: b for b in krw_block["buckets"]}
        self.assertEqual(kb["no_due_date"]["outstanding"], "5000")
        self.assertEqual(kb["no_due_date"]["invoice_count"], 1)
        self.assertEqual(kb["current"]["outstanding"], "0")

    def test_37_receivables_aging_cutoff_and_validation(self) -> None:
        # GBP：验证「查询日 UTC 结束时」闭区间口径（发票创建/收款/冲销）
        import sqlite3
        from datetime import datetime, timezone

        db_file = os.environ["DB_PATH"]

        def set_time(table: str, row_id: int, ts: str) -> None:
            c = sqlite3.connect(db_file, timeout=30)
            try:
                c.execute(
                    f"UPDATE {table} SET created_at = ? WHERE id = ?", (ts, row_id)
                )
                c.commit()
            finally:
                c.close()

        def gbp_invoice(due: str | None) -> int:
            status, body = self._request(
                "POST",
                "/api/v1/invoices",
                # net 100 + 10% 税 -> total_gross 110.00
                {"currency": "GBP", "due_date": due,
                 "lines": [{"net": "100.00", "tax_rate": "0.10"}]},
            )
            self.assertEqual(status, 201, body)
            self.assertEqual(body["totals"]["total_gross"], "110.00")
            return body["id"]

        a = gbp_invoice("2030-01-10")
        set_time("invoices", a, "2030-01-10T00:00:00Z")
        b = gbp_invoice("2030-01-16")
        set_time("invoices", b, "2030-01-16T12:00:00Z")  # 15 日结束之后才创建
        c = gbp_invoice(None)
        set_time("invoices", c, "2030-01-15T23:59:59Z")  # 恰在截止秒创建 -> 计入

        # A：截止秒收 30（计入）、截止秒冲销 10（计入）、次日 00:00:00 再收 50
        status, pay1 = self._request(
            "POST", f"/api/v1/invoices/{a}/payments", {"amount": "30.00"}
        )
        self.assertEqual(status, 201, pay1)
        set_time("payments", pay1["id"], "2030-01-15T23:59:59Z")
        status, rev = self._request(
            "POST", f"/api/v1/payments/{pay1['id']}/reversals", {"amount": "10.00"}
        )
        self.assertEqual(status, 201, rev)
        set_time("payment_reversals", rev["id"], "2030-01-15T23:59:59Z")
        status, pay2 = self._request(
            "POST", f"/api/v1/invoices/{a}/payments", {"amount": "50.00"}
        )
        self.assertEqual(status, 201, pay2)
        set_time("payments", pay2["id"], "2030-01-16T00:00:00Z")

        def aging(as_of: str | None):
            q = "" if as_of is None else f"?as_of={as_of}"
            return self._request("GET", f"/api/v1/receivables/aging{q}")

        gbp = lambda body: next(x for x in body["currencies"] if x["currency"] == "GBP")

        # 14 日：只有 A 已创建且尚无流水，未收 110，逾期 4 天
        status, body = aging("2030-01-14")
        self.assertEqual(status, 200, body)
        block = gbp(body)
        self.assertEqual(block["invoice_count"], 1)
        self.assertEqual(block["total_outstanding"], "110.00")
        bk = {x["bucket"]: x for x in block["buckets"]}
        self.assertEqual(bk["overdue_1_30"]["invoice_count"], 1)
        self.assertEqual(bk["overdue_1_30"]["outstanding"], "110.00")
        self.assertEqual(bk["no_due_date"]["invoice_count"], 0)

        # 15 日截止秒：A 净收 20（30−10，当秒均计入）-> 未收 90；
        # C 恰在截止秒创建 -> 计入；B 次日创建 -> 排除
        status, body = aging("2030-01-15")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["cutoff_at"], "2030-01-15T23:59:59Z")
        block = gbp(body)
        self.assertEqual(block["invoice_count"], 2, block)
        self.assertEqual(block["total_outstanding"], "200.00")
        bk = {x["bucket"]: x for x in block["buckets"]}
        self.assertEqual(bk["overdue_1_30"],
                         {"bucket": "overdue_1_30", "invoice_count": 1, "outstanding": "90.00"})
        self.assertEqual(bk["no_due_date"],
                         {"bucket": "no_due_date", "invoice_count": 1, "outstanding": "110.00"})

        # 16 日：A 净收 70 -> 未收 40；B 当日 12:00 已创建、到期当日 -> current
        status, body = aging("2030-01-16")
        self.assertEqual(status, 200, body)
        block = gbp(body)
        self.assertEqual(block["invoice_count"], 3)
        self.assertEqual(block["total_outstanding"], "260.00")
        bk = {x["bucket"]: x for x in block["buckets"]}
        self.assertEqual(bk["overdue_1_30"]["outstanding"], "40.00")
        self.assertEqual(bk["current"]["invoice_count"], 1)
        self.assertEqual(bk["current"]["outstanding"], "110.00")
        self.assertEqual(bk["no_due_date"]["invoice_count"], 1)

        # 非法日期 -> 422
        for bad in ("2030-13-01", "2030-02-30", "2030-01-15T00:00:00Z", "abc", "30-01-2030"):
            status, body = aging(bad)
            self.assertEqual(status, 422, bad)
            self.assertIn("detail", body)

        # 缺省 as_of：取 UTC 当前日期，cutoff 为当日 23:59:59Z
        status, body = aging(None)
        self.assertEqual(status, 200, body)
        today = datetime.now(timezone.utc).date().isoformat()
        self.assertEqual(body["as_of"], today)
        self.assertEqual(body["cutoff_at"], f"{today}T23:59:59Z")

    def test_38_allocation_batch_reversal(self) -> None:
        # 跨发票来款分配 -> 保存批次并回显 batch_id；按批次整笔撤销；
        # 重复请求幂等不新增冲销；手工部分冲销后只撤销净余额；
        # 不存在批次 404；旧的无批次收款仍按原接口冲销。
        def make_invoice(gross: str, due_date: str | None) -> int:
            payload = {"currency": "USD", "lines": [{"net": gross, "tax_rate": "0"}]}
            if due_date is not None:
                payload["due_date"] = due_date
            status, inv = self._request("POST", "/api/v1/invoices", payload)
            self.assertEqual(status, 201, inv)
            return inv["id"]

        # 三张发票：到期日决定分配顺序 x(09-01) -> y(09-02) -> z(09-03)
        id_x = make_invoice("10.00", "2026-09-01")
        id_y = make_invoice("20.00", "2026-09-02")
        id_z = make_invoice("30.00", "2026-09-03")

        # 旧的无批次收款（先于批次存在）：稍后验证其仍可按原接口冲销
        status, old_pay = self._request(
            "POST", f"/api/v1/invoices/{id_x}/payments", {"amount": "5.00"}
        )
        self.assertEqual(status, 201, old_pay)
        old_payment_id = old_pay["id"]

        # 来款 50.00 分配到三张发票（剩余未收：x 5、y 20、z 30，合计 55）
        status, body = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "50.00",
             "invoice_ids": [id_z, id_y, id_x]},
        )
        self.assertEqual(status, 201, body)
        self.assertIsInstance(body["batch_id"], int)
        batch_id = body["batch_id"]
        self.assertEqual(body["allocated_total"], "50.00")
        allocs = body["allocations"]
        self.assertEqual(
            [(it["invoice_id"], it["allocated"], it["payment_id"] is not None)
             for it in allocs],
            [(id_x, "5.00", True), (id_y, "20.00", True), (id_z, "25.00", True)],
        )
        pay_x, pay_y, pay_z = (it["payment_id"] for it in allocs)

        # 撤销前各发票余额
        status, ov = self._request("GET", f"/api/v1/invoices/{id_x}/payments")
        self.assertEqual(ov["paid_total"], "10.00")
        self.assertEqual(ov["outstanding"], "0.00")

        # 先手工部分冲销批次内 y 的收款 8.00：整批撤销时 y 只应再冲净余额 12.00
        status, rev = self._request(
            "POST", f"/api/v1/payments/{pay_y}/reversals", {"amount": "8.00"}
        )
        self.assertEqual(status, 201, rev)

        # 不存在的批次 -> 404，明确错误
        status, resp = self._request(
            "POST", "/api/v1/payment-allocation-batches/999999/reversals"
        )
        self.assertEqual(status, 404)
        self.assertIn("999999", resp["detail"])

        # 首次整批撤销 -> 201
        status, body = self._request(
            "POST", f"/api/v1/payment-allocation-batches/{batch_id}/reversals"
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["batch_id"], batch_id)
        self.assertEqual(body["currency"], "USD")
        self.assertEqual(body["allocated_total"], "50.00")
        # x 冲 5（净 5）、y 冲 12（净 20−8 手工）、z 冲 25 -> 合计 42
        self.assertEqual(body["reversed_total"], "42.00")
        self.assertEqual(body["reversal_count"], 3)
        items = body["items"]
        self.assertEqual(
            [(it["seq"], it["invoice_id"], it["payment_id"],
              it["allocated"], it["reversed_amount"], it["net_amount"])
             for it in items],
            [
                (1, id_x, pay_x, "5.00", "5.00", "0.00"),
                (2, id_y, pay_y, "20.00", "12.00", "0.00"),
                (3, id_z, pay_z, "25.00", "25.00", "0.00"),
            ],
        )
        for it in items:
            self.assertIsInstance(it["reversal_id"], int)
            self.assertTrue(it["reversal_created_at"].endswith("Z"))
            self.assertEqual(it["currency"], "USD")
        rev_x_id = items[0]["reversal_id"]
        rev_y_id = items[1]["reversal_id"]
        rev_z_id = items[2]["reversal_id"]

        # 撤销后发票未收余额按净入账额回升（x 上还有无批次的旧收款 5.00 净入账）
        status, ov = self._request("GET", f"/api/v1/invoices/{id_x}/payments")
        self.assertEqual(ov["paid_total"], "5.00")
        self.assertEqual(ov["reversed_total"], "5.00")
        self.assertEqual(ov["outstanding"], "5.00")
        by_pid = {p["id"]: p for p in ov["payments"]}
        self.assertEqual(by_pid[pay_x]["reversed_total"], "5.00")
        self.assertEqual(by_pid[pay_x]["net_amount"], "0.00")
        self.assertEqual(by_pid[old_payment_id]["reversed_total"], "0.00")

        status, ov = self._request("GET", f"/api/v1/invoices/{id_y}/payments")
        self.assertEqual(ov["paid_total"], "0.00")
        self.assertEqual(ov["reversed_total"], "20.00")
        self.assertEqual(ov["outstanding"], "20.00")

        status, ov = self._request("GET", f"/api/v1/invoices/{id_z}/payments")
        self.assertEqual(ov["paid_total"], "0.00")
        self.assertEqual(ov["outstanding"], "30.00")

        # 重复请求 -> 200，内容恒定，不新增冲销（reversal_id 与首次一致）
        status, again = self._request(
            "POST", f"/api/v1/payment-allocation-batches/{batch_id}/reversals"
        )
        self.assertEqual(status, 200, again)
        self.assertEqual(again["reversed_total"], "42.00")
        self.assertEqual(again["reversal_count"], 3)
        self.assertEqual(
            [it["reversed_amount"] for it in again["items"]],
            ["5.00", "12.00", "25.00"],
        )
        self.assertEqual(
            [it["reversal_id"] for it in again["items"]],
            [rev_x_id, rev_y_id, rev_z_id],
        )
        for it in again["items"]:
            self.assertEqual(it["net_amount"], "0.00")

        # 第三次请求同样幂等
        status, third = self._request(
            "POST", f"/api/v1/payment-allocation-batches/{batch_id}/reversals"
        )
        self.assertEqual(status, 200)
        self.assertEqual(third, again)

        # 旧的无批次收款仍可按原接口查询与冲销
        status, ov = self._request("GET", f"/api/v1/invoices/{id_x}/payments")
        old = next(p for p in ov["payments"] if p["id"] == old_payment_id)
        self.assertEqual(old["net_amount"], "5.00")
        status, rev = self._request(
            "POST", f"/api/v1/payments/{old_payment_id}/reversals",
            {"amount": "5.00"},
        )
        self.assertEqual(status, 201, rev)
        self.assertEqual(rev["reversed_total"], "5.00")
        self.assertEqual(rev["net_amount"], "0.00")

        # 批次撤销释放的余额可被新的来款分配再次使用
        # 此刻未收：x 10（旧收款已手工冲完）、y 20、z 30 -> 合计 60
        status, body = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "60.00",
             "invoice_ids": [id_x, id_y, id_z]},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["allocated_total"], "60.00")
        self.assertIsInstance(body["batch_id"], int)
        self.assertNotEqual(body["batch_id"], batch_id)
        for inv_id in (id_x, id_y, id_z):
            status, ov = self._request("GET", f"/api/v1/invoices/{inv_id}/payments")
            self.assertEqual(ov["outstanding"], "0.00", inv_id)

    def test_39_allocation_batch_reversal_already_manual_reversed(self) -> None:
        # 批次内收款在整批撤销前已被手工全额冲销：撤销返回 200、
        # 该笔 reversed_amount 为 0.00、reversal_id 为 null，且不新增冲销
        def make_invoice(gross: str) -> int:
            status, inv = self._request(
                "POST", "/api/v1/invoices",
                {"currency": "JPY", "lines": [{"net": gross, "tax_rate": "0"}]},
            )
            self.assertEqual(status, 201, inv)
            return inv["id"]

        id_a = make_invoice("100")
        id_b = make_invoice("200")
        status, body = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "JPY", "amount": "300", "invoice_ids": [id_a, id_b]},
        )
        self.assertEqual(status, 201, body)
        batch_id = body["batch_id"]
        pay_a, pay_b = (it["payment_id"] for it in body["allocations"])

        # 两笔批次收款都在撤销前手工冲完
        amount_by_pid = {it["payment_id"]: it["allocated"] for it in body["allocations"]}
        for pid in (pay_a, pay_b):
            status, rev = self._request(
                "POST", f"/api/v1/payments/{pid}/reversals",
                {"amount": amount_by_pid[pid]},
            )
            self.assertEqual(status, 201, rev)

        status, result = self._request(
            "POST", f"/api/v1/payment-allocation-batches/{batch_id}/reversals"
        )
        self.assertEqual(status, 200, result)  # 无新增 -> 200
        self.assertEqual(result["reversed_total"], "0")
        self.assertEqual(result["reversal_count"], 0)
        for it in result["items"]:
            self.assertEqual(it["reversed_amount"], "0")
            self.assertIsNone(it["reversal_id"])
            self.assertIsNone(it["reversal_created_at"])
            self.assertEqual(it["net_amount"], "0")

        # 重复请求仍幂等
        status, again = self._request(
            "POST", f"/api/v1/payment-allocation-batches/{batch_id}/reversals"
        )
        self.assertEqual(status, 200)
        self.assertEqual(again, result)

    def test_40_tax_ledger_grouping(self) -> None:
        # 期间税额归集：UTC 自然日闭区间；原币种 + 规范化税率分组；
        # 发票按保存时间、凭证按创建时间分别计入；不使用换算列；排序固定。
        import sqlite3
        from decimal import Decimal

        db_file = os.environ["DB_PATH"]

        def set_time(table: str, row_id: int, ts: str) -> None:
            c = sqlite3.connect(db_file, timeout=30)
            try:
                c.execute(
                    f"UPDATE {table} SET created_at = ? WHERE id = ?", (ts, row_id)
                )
                c.commit()
            finally:
                c.close()

        def invoice(currency: str, lines, ts: str, fx: bool = False) -> int:
            payload = {"currency": currency, "lines": lines}
            if fx:
                payload.update({"target_currency": "CNY", "exchange_rate": "5.5"})
            status, body = self._request("POST", "/api/v1/invoices", payload)
            self.assertEqual(status, 201, body)
            set_time("invoices", body["id"], ts)
            return body["id"]

        def memo(invoice_id: int, items, ts: str) -> dict:
            status, body = self._request(
                "POST", f"/api/v1/invoices/{invoice_id}/credit-memos",
                {"lines": [{"line_no": ln, "net": net} for ln, net in items]},
            )
            self.assertEqual(status, 201, body)
            set_time("credit_memos", body["id"], ts)
            return body

        def ledger(start: str, end: str):
            return self._request(
                "GET", f"/api/v1/tax-ledger?start={start}&end={end}"
            )

        # SGD（2 位）2031-02 期间发票：
        # a: 100.00 @0.10 + 50.00 @0.05；b: 10.00 @0.130；c: 20.00 @0.13；
        # neg: -40.00 @0.2；bound_lo/bound_hi 验证起止日闭区间
        a = invoice(
            "SGD",
            [{"net": "100.00", "tax_rate": "0.10"},
             {"net": "50.00", "tax_rate": "0.05"}],
            "2031-02-10T10:00:00Z",
        )
        invoice("SGD", [{"net": "10.00", "tax_rate": "0.130"}], "2031-02-11T08:00:00Z")
        invoice("SGD", [{"net": "20.00", "tax_rate": "0.13"}], "2031-02-12T08:00:00Z")
        invoice("JPY", [{"net": "1000", "tax_rate": "0.1"}], "2031-02-15T00:00:00Z")
        invoice("SGD", [{"net": "-40.00", "tax_rate": "0.2"}], "2031-02-05T00:00:00Z")
        invoice("SGD", [{"net": "1.00", "tax_rate": "0"}], "2031-02-01T00:00:00Z")
        invoice("SGD", [{"net": "2.00", "tax_rate": "0"}], "2031-02-28T23:59:59Z")
        # 带目标币换算的发票：归集金额仍是原币，不混入换算列
        invoice(
            "SGD", [{"net": "10.00", "tax_rate": "0.1"}],
            "2031-02-25T00:00:00Z", fx=True,
        )
        # 期间外发票：3 月开票排除；1 月发票本期不开票但其本期凭证仍计入抵减
        invoice("SGD", [{"net": "5.00", "tax_rate": "0.1"}], "2031-03-01T00:00:00Z")
        old = invoice(
            "SGD", [{"net": "10.00", "tax_rate": "0.13"}], "2031-01-15T00:00:00Z"
        )

        # 贷项凭证：本期创建即计入本期抵减（即使对应 1 月开票）；
        # 3 月创建的凭证不进入 2 月期间
        m = memo(a, [(1, "10.00")], "2031-02-20T09:00:00Z")
        self.assertEqual((m["lines"][0]["net"], m["lines"][0]["tax"]), ("10.00", "1.00"))
        memo(old, [(1, "5.00")], "2031-02-21T09:00:00Z")  # round(1.30*5/10)=0.65
        memo(a, [(1, "20.00")], "2031-03-05T09:00:00Z")

        status, body = ledger("2031-02-01", "2031-02-28")
        self.assertEqual(status, 200, body)
        self.assertEqual(
            body,
            {
                "start_date": "2031-02-01",
                "end_date": "2031-02-28",
                "period_start": "2031-02-01T00:00:00Z",
                "period_end": "2031-02-28T23:59:59Z",
                "groups": [
                    {"currency": "JPY", "currency_digits": 0, "tax_rate": "0.1",
                     "invoiced": {"net": "1000", "tax": "100", "gross": "1100"},
                     "credited": {"net": "0", "tax": "0", "gross": "0"},
                     "net": {"net": "1000", "tax": "100", "gross": "1100"}},
                    {"currency": "SGD", "currency_digits": 2, "tax_rate": "0",
                     "invoiced": {"net": "3.00", "tax": "0.00", "gross": "3.00"},
                     "credited": {"net": "0.00", "tax": "0.00", "gross": "0.00"},
                     "net": {"net": "3.00", "tax": "0.00", "gross": "3.00"}},
                    {"currency": "SGD", "currency_digits": 2, "tax_rate": "0.05",
                     "invoiced": {"net": "50.00", "tax": "2.50", "gross": "52.50"},
                     "credited": {"net": "0.00", "tax": "0.00", "gross": "0.00"},
                     "net": {"net": "50.00", "tax": "2.50", "gross": "52.50"}},
                    {"currency": "SGD", "currency_digits": 2, "tax_rate": "0.1",
                     # 100.00（a 行1）+ 10.00（带换算发票）；抵减 10.00
                     "invoiced": {"net": "110.00", "tax": "11.00", "gross": "121.00"},
                     "credited": {"net": "10.00", "tax": "1.00", "gross": "11.00"},
                     "net": {"net": "100.00", "tax": "10.00", "gross": "110.00"}},
                    {"currency": "SGD", "currency_digits": 2, "tax_rate": "0.13",
                     # 0.130 与 0.13 数值规范化后同组：10+20；抵减来自 1 月发票
                     "invoiced": {"net": "30.00", "tax": "3.90", "gross": "33.90"},
                     "credited": {"net": "5.00", "tax": "0.65", "gross": "5.65"},
                     "net": {"net": "25.00", "tax": "3.25", "gross": "28.25"}},
                    {"currency": "SGD", "currency_digits": 2, "tax_rate": "0.2",
                     "invoiced": {"net": "-40.00", "tax": "-8.00", "gross": "-48.00"},
                     "credited": {"net": "0.00", "tax": "0.00", "gross": "0.00"},
                     "net": {"net": "-40.00", "tax": "-8.00", "gross": "-48.00"}},
                ],
            },
        )

        # 分组排序固定：币种升序，同币种税率数值升序
        keys = [(g["currency"], g["tax_rate"]) for g in body["groups"]]
        self.assertEqual(
            keys,
            [("JPY", "0.1"), ("SGD", "0"), ("SGD", "0.05"),
             ("SGD", "0.1"), ("SGD", "0.13"), ("SGD", "0.2")],
        )
        # 每组恒有 net = invoiced - credited（未税/税/含税各列）
        for g in body["groups"]:
            for col in ("net", "tax", "gross"):
                self.assertEqual(
                    Decimal(g["net"][col]),
                    Decimal(g["invoiced"][col]) - Decimal(g["credited"][col]),
                )

        # 1 月：仅 1 月开票，本期无任何凭证 -> 该组 credited 全 0
        status, jan = ledger("2031-01-01", "2031-01-31")
        self.assertEqual(status, 200, jan)
        self.assertEqual(
            jan["groups"],
            [{"currency": "SGD", "currency_digits": 2, "tax_rate": "0.13",
              "invoiced": {"net": "10.00", "tax": "1.30", "gross": "11.30"},
              "credited": {"net": "0.00", "tax": "0.00", "gross": "0.00"},
              "net": {"net": "10.00", "tax": "1.30", "gross": "11.30"}}],
        )

        # 3 月：3 月发票与 3 月凭证（抵 a 行1 再 20.00 -> tax 2.00），净额为负
        status, mar = ledger("2031-03-01", "2031-03-31")
        self.assertEqual(status, 200, mar)
        self.assertEqual(
            mar["groups"],
            [{"currency": "SGD", "currency_digits": 2, "tax_rate": "0.1",
              "invoiced": {"net": "5.00", "tax": "0.50", "gross": "5.50"},
              "credited": {"net": "20.00", "tax": "2.00", "gross": "22.00"},
              "net": {"net": "-15.00", "tax": "-1.50", "gross": "-16.50"}}],
        )

        # 空期间 -> 空分组（字段仍回显）
        status, empty = ledger("2032-01-01", "2032-01-31")
        self.assertEqual(status, 200, empty)
        self.assertEqual(empty["groups"], [])
        self.assertEqual(empty["start_date"], "2032-01-01")
        self.assertEqual(empty["period_end"], "2032-01-31T23:59:59Z")

    def test_41_tax_ledger_validation(self) -> None:
        # 缺参数 / 非法日期 / 倒置日期 -> 422
        def ledger(path: str):
            return self._request("GET", path)

        for path in (
            "/api/v1/tax-ledger",
            "/api/v1/tax-ledger?start=2031-02-01",
            "/api/v1/tax-ledger?end=2031-02-28",
        ):
            status, body = ledger(path)
            self.assertEqual(status, 422, path)

        # 缺参数的 422 为 FastAPI 校验错误列表
        status, body = ledger("/api/v1/tax-ledger")
        self.assertIsInstance(body["detail"], list)

        # 非法/倒置日期走全局 ValueError -> 422 {"detail": "..."}
        for bad in (
            "start=2031-13-01&end=2031-02-28",
            "start=2031-02-30&end=2031-03-01",
            "start=2031-02-01T00:00:00Z&end=2031-02-28",
            "start=abc&end=2031-02-28",
            "start=2031-03-02&end=2031-03-01",
            "start=2031-03-02&end=2031-03-01",
        ):
            status, body = ledger(f"/api/v1/tax-ledger?{bad}")
            self.assertEqual(status, 422, bad)
            self.assertIsInstance(body["detail"], str, bad)

    def test_42_huge_amount_precision(self) -> None:
        # 超大金额：FastAPI 工作线程的 Decimal 默认 28 位精度不得影响结果。
        # 非最小单位金额 -> 422 且不写入
        status, before = self._request("GET", "/api/v1/invoices?page=1&page_size=1")
        self.assertEqual(status, 200)
        total_before = before["total"]

        status, body = self._request(
            "POST",
            "/api/v1/invoices",
            {
                "currency": "USD",
                "lines": [{"net": "10000000000000000000000000.001", "tax_rate": "0.10"}],
            },
        )
        self.assertEqual(status, 422, body)
        self.assertIn("最小单位", body["detail"])

        status, after = self._request("GET", "/api/v1/invoices?page=1&page_size=1")
        self.assertEqual(after["total"], total_before)  # 未写入任何记录

        # 合法 30 位整数净额 -> 201，金额精确保存
        payload = {
            "currency": "USD",
            "lines": [{"net": "100000000000000000000000000000.00", "tax_rate": "0.10"}],
        }
        status, body = self._request("POST", "/api/v1/invoices", payload)
        self.assertEqual(status, 201, body)
        self.assertEqual(body["lines"][0]["net"], "100000000000000000000000000000.00")
        self.assertEqual(body["lines"][0]["exact_tax"], "10000000000000000000000000000")
        self.assertEqual(body["lines"][0]["tax"], "10000000000000000000000000000.00")
        self.assertEqual(
            body["totals"],
            {
                "currency": "USD",
                "total_net": "100000000000000000000000000000.00",
                "total_tax": "10000000000000000000000000000.00",
                "total_gross": "110000000000000000000000000000.00",
            },
        )
        self.assertTrue(body["recompute_matched"])

        # 读回：逐字段一致且 recompute_matched 为 true
        status, fetched = self._request("GET", f"/api/v1/invoices/{body['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["totals"], body["totals"])
        self.assertEqual(fetched["lines"], body["lines"])
        self.assertTrue(fetched["recompute_matched"])

    def test_43_huge_amount_fx(self) -> None:
        # 超大金额跨币种：原币与目标币税额分配都精确、可复算
        payload = {
            "currency": "USD",
            "target_currency": "JPY",
            "exchange_rate": "150",
            "lines": [{"net": "100000000000000000000000000000.00", "tax_rate": "0.10"}],
        }
        status, body = self._request("POST", "/api/v1/invoices", payload)
        self.assertEqual(status, 201, body)
        self.assertEqual(body["totals"]["total_tax"], "10000000000000000000000000000.00")
        self.assertEqual(
            body["lines"][0]["converted"]["net"], "15000000000000000000000000000000"
        )
        self.assertEqual(
            body["lines"][0]["converted"]["tax"], "1500000000000000000000000000000"
        )
        self.assertEqual(body["fx"]["total_net"], "15000000000000000000000000000000")
        self.assertEqual(body["fx"]["total_tax"], "1500000000000000000000000000000")
        self.assertEqual(body["fx"]["total_gross"], "16500000000000000000000000000000")
        self.assertTrue(body["recompute_matched"])

        status, fetched = self._request("GET", f"/api/v1/invoices/{body['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["fx"], body["fx"])
        self.assertTrue(fetched["recompute_matched"])

    BIG_NET = "100000000000000000000000000000.00"      # 30 位整数 + 2 位小数
    BIG_GROSS = "110000000000000000000000000000.00"    # 10% 税后的含税总额
    BIG_GROSS_LESS_CENT = "109999999999999999999999999999.99"

    def _make_huge_invoice(self, due_date: str | None = None) -> int:
        payload = {
            "currency": "USD",
            "lines": [{"net": self.BIG_NET, "tax_rate": "0.10"}],
        }
        if due_date is not None:
            payload["due_date"] = due_date
        status, inv = self._request("POST", "/api/v1/invoices", payload)
        self.assertEqual(status, 201, inv)
        self.assertEqual(inv["totals"]["total_gross"], self.BIG_GROSS)
        return inv["id"]

    def _find_overdue(self, invoice_id: int, as_of: str) -> dict | None:
        page = 1
        while True:
            status, body = self._request(
                "GET",
                f"/api/v1/invoices/overdue?as_of={as_of}&page={page}&page_size=100",
            )
            self.assertEqual(status, 200, body)
            for item in body["items"]:
                if item["id"] == invoice_id:
                    return item
            if page >= body["pages"]:
                return None
            page += 1

    def test_44_huge_invoice_payment_precision(self) -> None:
        # 30 位整数 USD 发票：登记 0.01 后未收余额不丢分位；登记大额收款不再
        # 抛 InvalidOperation；收款响应、查询、快照与逾期查询的余额口径一致
        invoice_id = self._make_huge_invoice(due_date="2020-01-01")

        # 登记 0.01：未收余额必须精确到分位（默认 28 位精度下曾丢失 .99）
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "0.01"}
        )
        self.assertEqual(status, 201, pay)
        self.assertEqual(pay["amount"], "0.01")
        self.assertEqual(pay["paid_total"], "0.01")
        self.assertEqual(pay["outstanding"], self.BIG_GROSS_LESS_CENT)

        # 大额但非最小单位整数倍的金额 -> 422（不得抛 500）
        status, body = self._request(
            "POST",
            f"/api/v1/invoices/{invoice_id}/payments",
            {"amount": "110000000000000000000000000000.001"},
        )
        self.assertEqual(status, 422, body)

        # 查询口径与收款响应一致
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(status, 200)
        self.assertEqual(ov["paid_total"], "0.01")
        self.assertEqual(ov["outstanding"], self.BIG_GROSS_LESS_CENT)

        # 历史快照同一口径
        at = "2099-01-01T00:00:00Z"
        status, snap = self._request(
            "GET",
            f"/api/v1/invoices/{invoice_id}/payment-snapshot"
            f"?at={urllib.parse.quote(at, safe='')}",
        )
        self.assertEqual(status, 200, snap)
        self.assertEqual(snap["total_gross"], self.BIG_GROSS)
        self.assertEqual(snap["received_total"], "0.01")
        self.assertEqual(snap["net_paid"], "0.01")
        self.assertEqual(snap["outstanding"], self.BIG_GROSS_LESS_CENT)

        # 逾期查询同一口径
        item = self._find_overdue(invoice_id, "2030-01-01")
        self.assertIsNotNone(item)
        self.assertEqual(item["total_gross"], self.BIG_GROSS)
        self.assertEqual(item["paid_total"], "0.01")
        self.assertEqual(item["outstanding"], self.BIG_GROSS_LESS_CENT)

        # 登记剩余大额收款（此前会抛 InvalidOperation）：恰好收满
        status, pay = self._request(
            "POST",
            f"/api/v1/invoices/{invoice_id}/payments",
            {"amount": self.BIG_GROSS_LESS_CENT},
        )
        self.assertEqual(status, 201, pay)
        self.assertEqual(pay["amount"], self.BIG_GROSS_LESS_CENT)
        self.assertEqual(pay["paid_total"], self.BIG_GROSS)
        self.assertEqual(pay["outstanding"], "0.00")

        # 收满后超收 0.01 -> 422，且不留下任何流水
        status, body = self._request(
            "POST", f"/api/v1/invoices/{invoice_id}/payments", {"amount": "0.01"}
        )
        self.assertEqual(status, 422, body)
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(ov["payment_count"], 2)
        self.assertEqual(ov["paid_total"], self.BIG_GROSS)
        self.assertEqual(ov["outstanding"], "0.00")

        # 逾期列表中消失（已收满）
        self.assertIsNone(self._find_overdue(invoice_id, "2030-01-01"))

        # 0 位小数币种（JPY）的大额发票同样精确
        status, inv = self._request(
            "POST",
            "/api/v1/invoices",
            {
                "currency": "JPY",
                "lines": [{"net": "100000000000000000000000000000", "tax_rate": "0.10"}],
            },
        )
        self.assertEqual(status, 201, inv)
        jpy_id = inv["id"]
        self.assertEqual(inv["totals"]["total_gross"], "110000000000000000000000000000")
        status, pay = self._request(
            "POST", f"/api/v1/invoices/{jpy_id}/payments", {"amount": "1"}
        )
        self.assertEqual(status, 201, pay)
        self.assertEqual(pay["outstanding"], "109999999999999999999999999999")
        status, ov = self._request("GET", f"/api/v1/invoices/{jpy_id}/payments")
        self.assertEqual(ov["outstanding"], "109999999999999999999999999999")

    def test_45_huge_payment_reversal_precision(self) -> None:
        # 大额收款分次冲销：净入账额精确到分位；超冲 422 且不留流水
        invoice_id = self._make_huge_invoice()
        status, pay = self._request(
            "POST",
            f"/api/v1/invoices/{invoice_id}/payments",
            {"amount": self.BIG_GROSS},
        )
        self.assertEqual(status, 201, pay)
        self.assertEqual(pay["outstanding"], "0.00")
        payment_id = pay["id"]

        # 先冲 0.01：净入账额 = 总额 - 0.01，分位不丢
        status, rev = self._request(
            "POST", f"/api/v1/payments/{payment_id}/reversals", {"amount": "0.01"}
        )
        self.assertEqual(status, 201, rev)
        self.assertEqual(rev["reversed_total"], "0.01")
        self.assertEqual(rev["net_amount"], self.BIG_GROSS_LESS_CENT)

        # 再冲剩余大额：恰好冲完
        status, rev = self._request(
            "POST",
            f"/api/v1/payments/{payment_id}/reversals",
            {"amount": self.BIG_GROSS_LESS_CENT},
        )
        self.assertEqual(status, 201, rev)
        self.assertEqual(rev["reversed_total"], self.BIG_GROSS)
        self.assertEqual(rev["net_amount"], "0.00")

        # 超冲 0.01 -> 422，且不留下任何冲销流水
        status, body = self._request(
            "POST", f"/api/v1/payments/{payment_id}/reversals", {"amount": "0.01"}
        )
        self.assertEqual(status, 422, body)

        # 未收余额精确回升到含税总额，各查询口径一致
        status, ov = self._request("GET", f"/api/v1/invoices/{invoice_id}/payments")
        self.assertEqual(ov["payment_count"], 1)
        self.assertEqual(ov["paid_total"], "0.00")
        self.assertEqual(ov["reversed_total"], self.BIG_GROSS)
        self.assertEqual(ov["outstanding"], self.BIG_GROSS)
        self.assertEqual(ov["payments"][0]["net_amount"], "0.00")

        at = "2099-01-01T00:00:00Z"
        status, snap = self._request(
            "GET",
            f"/api/v1/invoices/{invoice_id}/payment-snapshot"
            f"?at={urllib.parse.quote(at, safe='')}",
        )
        self.assertEqual(status, 200, snap)
        self.assertEqual(snap["received_total"], self.BIG_GROSS)
        self.assertEqual(snap["reversed_total"], self.BIG_GROSS)
        self.assertEqual(snap["net_paid"], "0.00")
        self.assertEqual(snap["outstanding"], self.BIG_GROSS)

    def test_46_huge_allocation_and_batch_reversal(self) -> None:
        # 大额来款跨两张大额发票分配、按批次整批撤销：金额全程精确
        id_a = self._make_huge_invoice(due_date="2026-01-01")
        id_b = self._make_huge_invoice(due_date="2026-02-01")

        # 来款 = A 全额 + B 的 0.01（入参顺序打乱，仍按到期日顺序抵扣）
        amount = "110000000000000000000000000000.01"
        status, res = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": amount, "invoice_ids": [id_b, id_a]},
        )
        self.assertEqual(status, 201, res)
        self.assertEqual(res["allocated_total"], amount)
        by_invoice = {it["invoice_id"]: it for it in res["allocations"]}
        self.assertEqual(by_invoice[id_a]["allocated"], self.BIG_GROSS)
        self.assertEqual(by_invoice[id_a]["outstanding"], "0.00")
        self.assertEqual(by_invoice[id_b]["allocated"], "0.01")
        self.assertEqual(by_invoice[id_b]["outstanding"], self.BIG_GROSS_LESS_CENT)

        # B 的剩余未收（总额 − 0.01）可以恰好分完；再超出的来款 -> 422 整笔拒绝
        status, body = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {
                "currency": "USD",
                "amount": self.BIG_GROSS_LESS_CENT,
                "invoice_ids": [id_b],
            },
        )
        self.assertEqual(status, 201, body)
        status, body = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {
                "currency": "USD",
                "amount": "0.01",
                "invoice_ids": [id_b],
            },
        )
        self.assertEqual(status, 422, body)
        # 整笔拒绝不留流水：B 仍只有前两批各一条收款
        status, ov = self._request("GET", f"/api/v1/invoices/{id_b}/payments")
        self.assertEqual(ov["payment_count"], 2)
        self.assertEqual(ov["paid_total"], self.BIG_GROSS)
        self.assertEqual(ov["outstanding"], "0.00")

        # 整批撤销第一个批次：逐笔净入账额精确冲销
        status, rev = self._request(
            "POST",
            f"/api/v1/payment-allocation-batches/{res['batch_id']}/reversals",
        )
        self.assertEqual(status, 201, rev)
        self.assertEqual(rev["reversed_total"], amount)
        for item in rev["items"]:
            self.assertEqual(item["net_amount"], "0.00")
        by_invoice = {it["invoice_id"]: it for it in rev["items"]}
        self.assertEqual(by_invoice[id_a]["reversed_amount"], self.BIG_GROSS)
        self.assertEqual(by_invoice[id_b]["reversed_amount"], "0.01")

        # 幂等：重复请求 200，内容恒定
        status, rev2 = self._request(
            "POST",
            f"/api/v1/payment-allocation-batches/{res['batch_id']}/reversals",
        )
        self.assertEqual(status, 200, rev2)
        self.assertEqual(rev2, rev)

        # 撤销后 A 的未收余额精确回升
        status, ov = self._request("GET", f"/api/v1/invoices/{id_a}/payments")
        self.assertEqual(ov["paid_total"], "0.00")
        self.assertEqual(ov["outstanding"], self.BIG_GROSS)

    def test_47_allocation_mixed_currency_digits(self) -> None:
        # 同币种发票保存了不同 currency_digits（模拟币种精度配置变更：旧发票
        # 保留 2 位、新发票按 0 位保存）：整笔按实际抵扣顺序逐张校验分配额。
        import sqlite3

        db_file = os.environ["DB_PATH"]

        def make_usd(gross: str, due_date: str | None) -> int:
            payload = {"currency": "USD", "lines": [{"net": gross, "tax_rate": "0"}]}
            if due_date is not None:
                payload["due_date"] = due_date
            status, inv = self._request("POST", "/api/v1/invoices", payload)
            self.assertEqual(status, 201, inv)
            return inv["id"]

        def set_digits(invoice_id: int, digits: int, gross: str) -> None:
            # 直接改 SQLite 模拟历史发票以旧精度保存；分配只读取头表的
            # currency_digits / total_gross
            c = sqlite3.connect(db_file, timeout=30)
            try:
                c.execute(
                    "UPDATE invoices SET currency_digits = ?, total_gross = ?"
                    " WHERE id = ?",
                    (digits, gross, invoice_id),
                )
                c.commit()
            finally:
                c.close()

        def batch_count() -> int:
            c = sqlite3.connect(db_file, timeout=30)
            try:
                return int(
                    c.execute("SELECT COUNT(*) FROM payment_allocation_batches")
                    .fetchone()[0]
                )
            finally:
                c.close()

        # old：2 位旧发票 gross 5.00，due 2026-09-01（更早到期）
        # new：0 位新发票 gross 10（按整数保存），due 2026-09-10
        id_old = make_usd("5.00", "2026-09-01")
        id_new = make_usd("10.00", "2026-09-10")
        set_digits(id_new, 0, "10")
        batches_before = batch_count()

        # 场景 1：来款 5.01 全部应先抵 old(5.00)，余 0.01 落到 0 位 new
        # -> 0.01 不是 new 的最小单位整数倍：整笔 422，批次/收款/明细均不落库
        status, resp = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "5.01",
             "invoice_ids": [id_new, id_old]},  # 传入顺序打乱，仍按到期日抵扣
        )
        self.assertEqual(status, 422, resp)
        self.assertIn(str(id_new), resp["detail"])
        self.assertIn("0 位", resp["detail"])
        self.assertEqual(batch_count(), batches_before)
        status, ov = self._request("GET", f"/api/v1/invoices/{id_old}/payments")
        self.assertEqual(ov["payment_count"], 0)
        status, ov = self._request("GET", f"/api/v1/invoices/{id_new}/payments")
        self.assertEqual(ov["payment_count"], 0)

        # 场景 2：同样的 5.01，但把 0 位 new 放在更晚到期且其未收在前一张抵完前
        # 不需要承担零头——old 未收恰为 5.01 时余 0，合法来款不得被误拒绝。
        # 再造一张 2 位发票 old2：gross 5.01、due 2026-09-02（仍早于 new）
        id_old2 = make_usd("5.01", "2026-09-02")
        status, body = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "5.01",
             "invoice_ids": [id_new, id_old2]},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["amount"], "5.01")
        self.assertEqual(
            [(it["invoice_id"], it["allocated"], it["outstanding"], it["payment_id"] is not None)
             for it in body["allocations"]],
            [(id_old2, "5.01", "0.00", True),
             (id_new, "0", "10", False)],
        )

        # 场景 3：混合精度精确分配——old 抵 5.00（2 位），余 3 抵 new（0 位），
        # 整笔 8.00 成功；各张金额按自身保存位数展示
        status, body = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "8.00",
             "invoice_ids": [id_new, id_old]},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["amount"], "8.00")
        self.assertEqual(
            [(it["invoice_id"], it["allocated"], it["outstanding"])
             for it in body["allocations"]],
            [(id_old, "5.00", "0.00"),
             (id_new, "3", "7")],
        )
        mixed_batch_id = body["batch_id"]

        # 场景 4：0.5 落到 0 位 new（old 已抵满）-> 422，不落库
        status, resp = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "0.5", "invoice_ids": [id_old, id_new]},
        )
        self.assertEqual(status, 422, resp)

        # 混合精度批次整批撤销：各笔按自身发票净入账额冲销，合计保留两位
        status, rev = self._request(
            "POST",
            f"/api/v1/payment-allocation-batches/{mixed_batch_id}/reversals",
        )
        self.assertEqual(status, 201, rev)
        self.assertEqual(rev["reversed_total"], "8.00")
        self.assertEqual(
            [(it["invoice_id"], it["allocated"], it["reversed_amount"])
             for it in rev["items"]],
            [(id_old, "5.00", "5.00"),
             (id_new, "3", "3")],
        )
        # 撤销幂等，内容恒定
        status, again = self._request(
            "POST",
            f"/api/v1/payment-allocation-batches/{mixed_batch_id}/reversals",
        )
        self.assertEqual(status, 200)
        self.assertEqual(again, rev)

    def test_48_saved_invoices_keep_digits_after_config_change(self) -> None:
        # 创建时固定原币/目标币小数位：CURRENCY_DIGITS 变更后，已保存发票的读取、
        # 行/合计/残差分配与 recompute_matched 仍按入库精度；新发票按新配置校验。
        # 旧发票（USD 两位、净额 0.01）
        status, old_inv = self._request(
            "POST",
            "/api/v1/invoices",
            {"currency": "USD", "lines": [{"net": "0.01", "tax_rate": "0.10"}]},
        )
        self.assertEqual(status, 201, old_inv)
        self.assertEqual(old_inv["currency_digits"], 2)
        self.assertEqual(old_inv["lines"][0]["net"], "0.01")
        self.assertEqual(old_inv["lines"][0]["tax"], "0.00")
        old_id = old_inv["id"]

        # 含残差分配的两位旧发票（10.05/20.05 @10% -> 残差 -1 分给第 1 行）
        status, residual_inv = self._request(
            "POST",
            "/api/v1/invoices",
            {
                "currency": "USD",
                "lines": [
                    {"net": "10.05", "tax_rate": "0.10"},
                    {"net": "20.05", "tax_rate": "0.10"},
                ],
            },
        )
        self.assertEqual(status, 201, residual_inv)
        self.assertEqual(residual_inv["rounding"]["residual_units"], -1)
        self.assertEqual(
            [ln["tax"] for ln in residual_inv["lines"]], ["1.00", "2.01"]
        )
        residual_id = residual_inv["id"]

        # 跨币种旧发票：USD(2) -> JPY(0)
        status, fx_inv = self._request(
            "POST",
            "/api/v1/invoices",
            {
                "currency": "USD",
                "target_currency": "JPY",
                "exchange_rate": "150.25",
                "lines": [{"net": "1.00", "tax_rate": "0.10"}],
            },
        )
        self.assertEqual(status, 201, fx_inv)
        self.assertEqual(fx_inv["fx"]["target_digits"], 0)
        self.assertEqual(fx_inv["lines"][0]["converted"]["net"], "150")
        fx_id = fx_inv["id"]

        # 在**同一运行进程**内把 USD 改成零位、JPY 改成两位（配置即时生效）
        old_env = os.environ.get("CURRENCY_DIGITS")
        os.environ["CURRENCY_DIGITS"] = "USD:0,JPY:2"
        try:
            # 旧 USD 发票读取不再 422，逐行/合计/残差分配与入库时完全一致
            status, fetched = self._request("GET", f"/api/v1/invoices/{old_id}")
            self.assertEqual(status, 200, fetched)
            self.assertEqual(fetched["currency_digits"], 2)
            self.assertEqual(fetched["lines"][0]["net"], "0.01")
            self.assertEqual(fetched["lines"][0]["tax"], "0.00")
            self.assertEqual(fetched["totals"], old_inv["totals"])
            self.assertEqual(fetched["rounding"]["minor_unit"], "0.01")
            self.assertTrue(fetched["recompute_matched"])

            # 残差分配明细按旧精度复算，保持一致
            status, fr = self._request("GET", f"/api/v1/invoices/{residual_id}")
            self.assertEqual(status, 200, fr)
            self.assertEqual(fr["currency_digits"], 2)
            self.assertEqual(fr["rounding"]["residual_units"], -1)
            self.assertEqual(
                fr["rounding"]["allocations"],
                residual_inv["rounding"]["allocations"],
            )
            self.assertEqual(
                [ln["tax"] for ln in fr["lines"]], ["1.00", "2.01"]
            )
            self.assertEqual(fr["totals"], residual_inv["totals"])
            self.assertTrue(fr["recompute_matched"])

            # 旧 FX 发票：目标币仍按入库时的零位恢复（历史行 target_digits 为
            # NULL 也须从已持久化换算金额恢复），换算列与残差单位不变
            status, ff = self._request("GET", f"/api/v1/invoices/{fx_id}")
            self.assertEqual(status, 200, ff)
            self.assertEqual(ff["currency_digits"], 2)
            self.assertEqual(ff["fx"]["target_digits"], 0)
            self.assertEqual(ff["fx"]["residual_unit"], "1")
            self.assertEqual(
                ff["lines"][0]["converted"], fx_inv["lines"][0]["converted"]
            )
            self.assertEqual(
                ff["fx"]["total_net"], fx_inv["fx"]["total_net"]
            )
            self.assertTrue(ff["recompute_matched"])

            # 新建发票仍按当前配置校验：USD 零位下 0.01 净额被拒绝
            status, rejected = self._request(
                "POST",
                "/api/v1/invoices",
                {"currency": "USD", "lines": [{"net": "0.01", "tax_rate": "0.10"}]},
            )
            self.assertEqual(status, 422, rejected)

            # 零位整数净额的新发票按新精度保存与复算
            status, new_inv = self._request(
                "POST",
                "/api/v1/invoices",
                {"currency": "USD", "lines": [{"net": "5", "tax_rate": "0.10"}]},
            )
            self.assertEqual(status, 201, new_inv)
            self.assertEqual(new_inv["currency_digits"], 0)
            self.assertEqual(new_inv["lines"][0]["net"], "5")
            self.assertEqual(new_inv["lines"][0]["tax"], "1")
            self.assertTrue(new_inv["recompute_matched"])
            new_id = new_inv["id"]
            status, nf = self._request("GET", f"/api/v1/invoices/{new_id}")
            self.assertEqual(status, 200, nf)
            self.assertEqual(nf["currency_digits"], 0)
            self.assertEqual(nf["rounding"]["minor_unit"], "1")
            self.assertTrue(nf["recompute_matched"])

            # 新 FX 发票的目标币按当前配置固定为两位并持久化
            status, new_fx = self._request(
                "POST",
                "/api/v1/invoices",
                {
                    "currency": "USD",
                    "target_currency": "JPY",
                    "exchange_rate": "150.25",
                    "lines": [{"net": "5", "tax_rate": "0.10"}],
                },
            )
            self.assertEqual(status, 201, new_fx)
            self.assertEqual(new_fx["fx"]["target_digits"], 2)
            self.assertEqual(new_fx["lines"][0]["converted"]["net"], "751.25")
        finally:
            if old_env is None:
                os.environ.pop("CURRENCY_DIGITS", None)
            else:
                os.environ["CURRENCY_DIGITS"] = old_env

    def test_49_allocation_negative_and_settled_invoices(self) -> None:
        # 候选列表含负额冲红单 / 已结清发票：两者分配 0，不消耗也不增加剩余
        # 来款；可分配余额只计入正未收额，成功时各笔收款合计恒等于来款金额。
        import sqlite3

        db_file = os.environ["DB_PATH"]

        def make(gross: str, due_date: str | None) -> int:
            payload = {"currency": "USD", "lines": [{"net": gross, "tax_rate": "0"}]}
            if due_date is not None:
                payload["due_date"] = due_date
            status, inv = self._request("POST", "/api/v1/invoices", payload)
            self.assertEqual(status, 201, inv)
            return inv["id"]

        def batch_count() -> int:
            c = sqlite3.connect(db_file, timeout=30)
            try:
                return int(
                    c.execute("SELECT COUNT(*) FROM payment_allocation_batches")
                    .fetchone()[0]
                )
            finally:
                c.close()

        # N: 负数冲红单（total_gross -100.00，最早到期，排在抵扣顺序最前）
        # S: 已结清发票（30.00，先单笔收满）；A: 正未收 50.00；B: 正未收 100.00
        id_n = make("-100.00", "2026-09-01")
        id_s = make("30.00", "2026-09-03")
        id_a = make("50.00", "2026-09-05")
        id_b = make("100.00", "2026-09-10")

        status, pay = self._request(
            "POST", f"/api/v1/invoices/{id_s}/payments", {"amount": "30.00"}
        )
        self.assertEqual(status, 201, pay)

        # 来款 50.00，抵扣顺序 N -> S -> A -> B：负额 N 与已结清 S 分配 0，
        # 不消耗也不增加剩余来款；A 抵 50.00，B 本次不抵扣。
        # （修复前 N 的 -100.00 会被当作分配额，剩余来款滚成 150.00 并写进 B，
        #  50.00 来款实际入账 150.00，批次金额与入账不符）
        status, body = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "50.00",
             "invoice_ids": [id_b, id_a, id_s, id_n]},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["amount"], "50.00")
        self.assertEqual(body["allocated_total"], "50.00")
        self.assertEqual(
            [(it["invoice_id"], it["allocated"], it["outstanding"],
              it["payment_id"] is not None)
             for it in body["allocations"]],
            [(id_n, "0.00", "-100.00", False),
             (id_s, "0.00", "0.00", False),
             (id_a, "50.00", "0.00", True),
             (id_b, "0.00", "100.00", False)],
        )

        # 实际入账合计必须等于来款金额：只有 A 新增一条 50.00 流水
        status, ov = self._request("GET", f"/api/v1/invoices/{id_a}/payments")
        self.assertEqual(ov["payment_count"], 1)
        self.assertEqual(ov["paid_total"], "50.00")
        self.assertEqual(ov["outstanding"], "0.00")
        status, ov = self._request("GET", f"/api/v1/invoices/{id_b}/payments")
        self.assertEqual(ov["payment_count"], 0)
        # 冲红单余额保持负值、不产生流水，不被来款改变
        status, ov = self._request("GET", f"/api/v1/invoices/{id_n}/payments")
        self.assertEqual(ov["payment_count"], 0)
        self.assertEqual(ov["outstanding"], "-100.00")

        # 正未收额合计不足：B 正未收 100.00，来款 100.01 -> 422，
        # 批次与收款均不落库（负额 N 不计入可分配余额）
        batches_before = batch_count()
        status, resp = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "100.01",
             "invoice_ids": [id_n, id_s, id_b]},
        )
        self.assertEqual(status, 422, resp)
        self.assertIn("detail", resp)
        self.assertEqual(batch_count(), batches_before)
        status, ov = self._request("GET", f"/api/v1/invoices/{id_b}/payments")
        self.assertEqual(ov["payment_count"], 0)

        # 列表里全是负额/已结清发票：正未收合计为 0，任何来款都 422
        status, resp = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "1.00", "invoice_ids": [id_n, id_s]},
        )
        self.assertEqual(status, 422, resp)
        self.assertEqual(batch_count(), batches_before)

        # 负额发票在场也不影响后续精确分配：B 的 100.00 恰好收满
        status, body = self._request(
            "POST",
            "/api/v1/payment-allocations",
            {"currency": "USD", "amount": "100.00",
             "invoice_ids": [id_n, id_b]},
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["allocated_total"], "100.00")
        self.assertEqual(
            [(it["invoice_id"], it["allocated"], it["outstanding"])
             for it in body["allocations"]],
            [(id_n, "0.00", "-100.00"), (id_b, "100.00", "0.00")],
        )
        status, ov = self._request("GET", f"/api/v1/invoices/{id_b}/payments")
        self.assertEqual(ov["payment_count"], 1)
        self.assertEqual(ov["paid_total"], "100.00")

    def test_50_aging_and_ledger_mixed_precision_order_independent(self) -> None:
        # 同币种混合入库精度（币种精度配置变更后旧 0 位 + 新 2 位共存）：
        # 每张发票按自己的入库精度算未收额；账龄桶/币种合计与税额组在 Decimal
        # 累加后按**该组实际参与记录的最高入库小数位**格式化并回显 currency_digits。
        # XBA 先 0 位后 2 位、XBB 先 2 位后 0 位（插入顺序相反）：两组数据同构，
        # 结果必须一致（与插入顺序无关），合计 0.81 不得被抹成 "1"。
        import sqlite3
        from decimal import Decimal

        db_file = os.environ["DB_PATH"]

        def set_time(table: str, row_id: int, ts: str) -> None:
            c = sqlite3.connect(db_file, timeout=30)
            try:
                c.execute(
                    f"UPDATE {table} SET created_at = ? WHERE id = ?", (ts, row_id)
                )
                c.commit()
            finally:
                c.close()

        def invoice(code: str, net: str, rate: str, ts: str,
                    due: str | None = None) -> int:
            payload = {"currency": code,
                       "lines": [{"net": net, "tax_rate": rate}]}
            if due is not None:
                payload["due_date"] = due
            status, body = self._request("POST", "/api/v1/invoices", payload)
            self.assertEqual(status, 201, body)
            set_time("invoices", body["id"], ts)
            return body["id"]

        def pay(invoice_id: int, amount: str, ts: str) -> None:
            status, body = self._request(
                "POST", f"/api/v1/invoices/{invoice_id}/payments",
                {"amount": amount},
            )
            self.assertEqual(status, 201, body)
            set_time("payments", body["id"], ts)

        old_env = os.environ.get("CURRENCY_DIGITS")
        os.environ["CURRENCY_DIGITS"] = "XBA:0,XBB:2"
        try:
            # 阶段 1：XBA 按 0 位、XBB 按 2 位保存
            # 无到期日 @10%：XBA gross 11；XBB gross 2.81（随后部分收款 2.00）
            a0 = invoice("XBA", "10", "0.10", "2032-01-10T00:00:00Z")
            b2 = invoice("XBB", "2.55", "0.10", "2032-01-10T00:00:00Z")
            pay(b2, "2.00", "2032-01-11T00:00:00Z")  # XBB 余 0.81
            # 有到期日 @0%：只进逾期桶，用于验证**桶级**位数（0 位桶出 "5"）
            invoice("XBA", "5", "0", "2032-01-10T00:00:00Z", due="2032-02-01")
            invoice("XBB", "5.00", "0", "2032-01-10T00:00:00Z", due="2032-02-01")
            self.assertEqual(self._request("GET", f"/api/v1/invoices/{a0}")[1]
                             ["currency_digits"], 0)

            # 阶段 2：精度配置翻转——XBA 按 2 位、XBB 按 0 位保存
            os.environ["CURRENCY_DIGITS"] = "XBA:2,XBB:0"
            a2 = invoice("XBA", "2.55", "0.10", "2032-01-12T00:00:00Z")
            pay(a2, "2.00", "2032-01-13T00:00:00Z")  # XBA 余 0.81
            invoice("XBB", "10", "0.10", "2032-01-12T00:00:00Z")
            self.assertEqual(self._request("GET", f"/api/v1/invoices/{a2}")[1]
                             ["currency_digits"], 2)
        finally:
            if old_env is None:
                os.environ.pop("CURRENCY_DIGITS", None)
            else:
                os.environ["CURRENCY_DIGITS"] = old_env

        # ---- 应收账龄汇总 ----
        status, body = self._request(
            "GET", "/api/v1/receivables/aging?as_of=2032-03-01"
        )
        self.assertEqual(status, 200, body)
        by_ccy = {c["currency"]: c for c in body["currencies"]}
        self.assertIn("XBA", by_ccy)
        self.assertIn("XBB", by_ccy)

        expected_no_due = "11.81"   # 11 + 0.81，不得舍成 "12"
        for code, overdue_text in (("XBA", "5"), ("XBB", "5.00")):
            block = by_ccy[code]
            # 组精度为参与记录最高位数（2），与插入顺序无关
            self.assertEqual(block["currency_digits"], 2, code)
            self.assertEqual(block["invoice_count"], 3, code)
            self.assertEqual(block["total_outstanding"], "16.81", code)
            bk = {b["bucket"]: b for b in block["buckets"]}
            self.assertEqual(bk["no_due_date"]["invoice_count"], 2, code)
            self.assertEqual(bk["no_due_date"]["outstanding"], expected_no_due, code)
            # 桶级位数：XBA 该桶只有 0 位发票 -> "5"；XBB 2 位 -> "5.00"
            self.assertEqual(bk["overdue_1_30"]["invoice_count"], 1, code)
            self.assertEqual(bk["overdue_1_30"]["outstanding"], overdue_text, code)
            # 空桶沿用币种位数（2 位）
            self.assertEqual(bk["current"]["outstanding"], "0.00", code)
            # 桶未收之和（Decimal）恒等于币种总未收
            self.assertEqual(
                sum(Decimal(b["outstanding"]) for b in block["buckets"]),
                Decimal("16.81"),
                code,
            )
            self.assertEqual(
                sum(b["invoice_count"] for b in block["buckets"]), 3, code
            )

        # ---- 期间税额归集 ----
        status, body = self._request(
            "GET", "/api/v1/tax-ledger?start=2032-01-01&end=2032-01-31"
        )
        self.assertEqual(status, 200, body)
        groups = {
            (g["currency"], g["tax_rate"]): g
            for g in body["groups"]
            if g["currency"] in ("XBA", "XBB")
        }
        # @0% 组只有各币种的有到期日发票：组位数即该发票入库位数
        self.assertEqual(groups[("XBA", "0")]["currency_digits"], 0)
        self.assertEqual(
            groups[("XBA", "0")]["invoiced"],
            {"net": "5", "tax": "0", "gross": "5"},
        )
        self.assertEqual(groups[("XBB", "0")]["currency_digits"], 2)
        self.assertEqual(
            groups[("XBB", "0")]["invoiced"],
            {"net": "5.00", "tax": "0.00", "gross": "5.00"},
        )
        # @10% 组：0 位行 + 2 位行，两种插入顺序结果完全一致
        for code in ("XBA", "XBB"):
            g = groups[(code, "0.1")]
            self.assertEqual(g["currency_digits"], 2, code)
            self.assertEqual(
                g["invoiced"],
                {"net": "12.55", "tax": "1.26", "gross": "13.81"},
                code,
            )
            self.assertEqual(
                g["credited"],
                {"net": "0.00", "tax": "0.00", "gross": "0.00"},
                code,
            )
            self.assertEqual(
                g["net"],
                {"net": "12.55", "tax": "1.26", "gross": "13.81"},
                code,
            )
            for col in ("net", "tax", "gross"):
                self.assertEqual(
                    Decimal(g["net"][col]),
                    Decimal(g["invoiced"][col]) - Decimal(g["credited"][col]),
                    (code, col),
                )

    def test_51_tax_ledger_credit_memo_lines_set_group_digits(self) -> None:
        # 贷项凭证行参与税额组精度判定：组内开票行全是 0 位、本期抵减凭证来自
        # 2 位精度发票时，整组（含 invoiced/credited/net）按 2 位展示，抵减额
        # 0.10/0.01 不得被抹成 0。XMC/XMD 插入顺序相反，结果一致。
        import sqlite3
        from decimal import Decimal

        db_file = os.environ["DB_PATH"]

        def set_time(table: str, row_id: int, ts: str) -> None:
            c = sqlite3.connect(db_file, timeout=30)
            try:
                c.execute(
                    f"UPDATE {table} SET created_at = ? WHERE id = ?", (ts, row_id)
                )
                c.commit()
            finally:
                c.close()

        def setup(code: str, first_digits: int) -> None:
            # first_digits=0：先 0 位（5 月开票）后 2 位（6 月开票 + 5 月凭证）；
            # first_digits=2：顺序相反
            def make(net: str) -> int:
                status, body = self._request(
                    "POST",
                    "/api/v1/invoices",
                    {"currency": code,
                     "lines": [{"net": net, "tax_rate": "0.10"}]},
                )
                self.assertEqual(status, 201, body)
                return body["id"]

            def memo(iid: int, net: str) -> dict:
                status, body = self._request(
                    "POST", f"/api/v1/invoices/{iid}/credit-memos",
                    {"lines": [{"line_no": 1, "net": net}]},
                )
                self.assertEqual(status, 201, body)
                return body

            order = [0, 2] if first_digits == 0 else [2, 0]
            ids: dict[int, int] = {}
            for i, digits in enumerate(order):
                os.environ["CURRENCY_DIGITS"] = f"{code}:{digits}"
                if digits == 0:
                    iid = make("10")                 # 0 位：5 月开票
                    set_time("invoices", iid, "2033-05-10T00:00:00Z")
                else:
                    iid = make("2.00")               # 2 位：6 月开票
                    set_time("invoices", iid, "2033-06-05T00:00:00Z")
                    m = memo(iid, "0.10")            # 凭证抵 0.10（税 0.01）
                    self.assertEqual(
                        (m["lines"][0]["net"], m["lines"][0]["tax"]),
                        ("0.10", "0.01"),
                    )
                    # 凭证创建在 5 月：抵减计入 5 月台账（原发票 6 月才开票）
                    set_time("credit_memos", m["id"], "2033-05-20T00:00:00Z")
                ids[digits] = iid

        old_env = os.environ.get("CURRENCY_DIGITS")
        try:
            setup("XMC", first_digits=0)
            setup("XMD", first_digits=2)
        finally:
            if old_env is None:
                os.environ.pop("CURRENCY_DIGITS", None)
            else:
                os.environ["CURRENCY_DIGITS"] = old_env

        status, body = self._request(
            "GET", "/api/v1/tax-ledger?start=2033-05-01&end=2033-05-31"
        )
        self.assertEqual(status, 200, body)
        for code in ("XMC", "XMD"):
            g = next(g for g in body["groups"]
                     if g["currency"] == code and g["tax_rate"] == "0.1")
            # 凭证行（2 位）把整组位数抬到 2，即使本期开票行全是 0 位
            self.assertEqual(g["currency_digits"], 2, code)
            self.assertEqual(
                g["invoiced"],
                {"net": "10.00", "tax": "1.00", "gross": "11.00"},
                code,
            )
            self.assertEqual(
                g["credited"],
                {"net": "0.10", "tax": "0.01", "gross": "0.11"},
                code,
            )
            self.assertEqual(
                g["net"],
                {"net": "9.90", "tax": "0.99", "gross": "10.89"},
                code,
            )
            for col in ("net", "tax", "gross"):
                self.assertEqual(
                    Decimal(g["net"][col]),
                    Decimal(g["invoiced"][col]) - Decimal(g["credited"][col]),
                    (code, col),
                )


class ChunkedApiTests(unittest.TestCase):
    """超大 ID 集合下收款余额读取链路的端到端验证（独立服务与数据库）。

    直接向服务库批量写入 600 张发票及收款/冲销（超过 app.db._IN_CHUNK_SIZE
    的固定批量，收款/冲销读取必然分段），再经真实 HTTP 查询应收账龄汇总与
    逾期列表：分段取数对调用方透明，结果与小批量一致。
    """

    server: ServerThread
    tmpdir: tempfile.TemporaryDirectory
    base: str
    db_file: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.db_file = str(Path(cls.tmpdir.name) / "chunked_api.db")
        os.environ["DB_PATH"] = cls.db_file
        port = _free_port()
        cls.base = f"http://127.0.0.1:{port}"
        cls.server = ServerThread(cls.db_file, port)
        cls.server.start()
        _wait_ready(cls.base)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmpdir.cleanup()
        os.environ.pop("DB_PATH", None)

    def _request(self, method: str, path: str, body: dict | None = None):
        return _request(self.base, method, path, body)

    def test_01_aging_and_overdue_with_huge_id_set(self) -> None:
        import sqlite3

        n = 600  # > app.db._IN_CHUNK_SIZE：收款/冲销的 IN 查询必然分段
        conn = sqlite3.connect(self.db_file, timeout=30)
        try:
            conn.executemany(
                """
                INSERT INTO invoices (
                    currency, currency_digits, total_net, total_tax, total_gross,
                    residual_units, rule_id, request_json, due_date, created_at
                ) VALUES ('USD', 2, '100.00', '0.00', '100.00', 0, 'test', '{}',
                          '2026-01-01', '2026-01-01T00:00:00Z')
                """,
                [()] * n,
            )
            invoice_ids = [
                r[0] for r in conn.execute("SELECT id FROM invoices ORDER BY id")
            ]
            self.assertEqual(len(invoice_ids), n)
            conn.executemany(
                "INSERT INTO payments (invoice_id, amount, currency, created_at)"
                " VALUES (?, '10.00', 'USD', '2026-01-02T00:00:00Z')",
                [(iid,) for iid in invoice_ids],
            )
            payment_ids = [
                r[0] for r in conn.execute("SELECT id FROM payments ORDER BY id")
            ]
            self.assertEqual(len(payment_ids), n)
            conn.executemany(
                "INSERT INTO payment_reversals (payment_id, amount, currency, created_at)"
                " VALUES (?, '1.00', 'USD', '2026-01-03T00:00:00Z')",
                [(pid,) for pid in payment_ids],
            )
            conn.commit()
        finally:
            conn.close()

        # 应收账龄汇总：全库 600 张发票的收款/冲销分段取数后逐笔 Decimal 合并
        status, aging = self._request(
            "GET", "/api/v1/receivables/aging?as_of=2026-01-15"
        )
        self.assertEqual(status, 200, aging)
        self.assertEqual(aging["cutoff_at"], "2026-01-15T23:59:59Z")
        self.assertEqual(len(aging["currencies"]), 1)
        usd = aging["currencies"][0]
        self.assertEqual(usd["currency"], "USD")
        self.assertEqual(usd["invoice_count"], n)
        # 每张净入账 10.00 − 1.00 = 9.00，未收 91.00；600 × 91.00 = 54600.00
        self.assertEqual(usd["total_outstanding"], "54600.00")
        buckets = {b["bucket"]: b for b in usd["buckets"]}
        self.assertEqual(buckets["overdue_1_30"]["invoice_count"], n)
        self.assertEqual(buckets["overdue_1_30"]["outstanding"], "54600.00")

        # 逾期列表：total 覆盖全部 600 张，逐页核对金额口径与 id 倒序
        seen = 0
        last_id = None
        for page in range(1, 7):
            status, body = self._request(
                "GET",
                f"/api/v1/invoices/overdue?page={page}&page_size=100&as_of=2026-01-15",
            )
            self.assertEqual(status, 200, body)
            self.assertEqual(body["total"], n)
            self.assertEqual(body["pages"], 6)
            for item in body["items"]:
                self.assertEqual(item["total_gross"], "100.00")
                self.assertEqual(item["paid_total"], "9.00")
                self.assertEqual(item["outstanding"], "91.00")
                if last_id is not None:
                    self.assertLess(item["id"], last_id)
                last_id = item["id"]
            seen += len(body["items"])
        self.assertEqual(seen, n)


if __name__ == "__main__":
    unittest.main()
