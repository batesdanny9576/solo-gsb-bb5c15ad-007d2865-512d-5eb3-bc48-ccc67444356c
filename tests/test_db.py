"""收款余额读取链路的分段取数测试（app.db 层，直接连临时库，不经 HTTP）。

按发票 ID 取收款、按收款 ID 取冲销的 ``IN (...)`` 查询统一按固定批量
（``app.db._IN_CHUNK_SIZE``）分段取数，覆盖：

* 固定批量连同 UTC 截止时点参数仍低于 SQLite 旧版构建的绑定参数上限；
* 空集合不发起任何 ``IN`` 查询；
* 超大 ID 集合（1200 张发票/收款）下逾期列表、应收账龄汇总可查询，
  每条语句的绑定参数数不超预算，且确实发生了分段；
* 逾期列表、账龄汇总、收款概览、历史快照在强制小批量（分段数放大）下
  的结果与默认批量完全一致；
* 历史快照的冲销明细在分段合并后仍按冲销流水 id 升序返回；
* 收款概览在单张发票流水数超过批量上限时结果不变。

运行：python -m unittest tests.test_db -v
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from app import db as database


class ChunkedReadTests(unittest.TestCase):
    """直接对临时 SQLite 库调用 app.db 的读取函数，验证分段取数口径。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.conn = database.connect(str(Path(self.tmpdir.name) / "chunked.db"))
        self.addCleanup(self.conn.close)
        database.init_db(self.conn)

    # ---- 数据构造（直接 INSERT，绕开 API 层校验，专注读取链路） ----

    def _invoice(
        self,
        gross: str,
        due_date: str | None,
        created_at: str,
        digits: int = 2,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO invoices (
                currency, currency_digits, total_net, total_tax, total_gross,
                residual_units, rule_id, request_json, due_date, created_at
            ) VALUES ('USD', ?, '0', '0', ?, 0, 'test', '{}', ?, ?)
            """,
            (digits, gross, due_date, created_at),
        )
        return int(cur.lastrowid)

    def _payment(self, invoice_id: int, amount: str, created_at: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO payments (invoice_id, amount, currency, created_at)"
            " VALUES (?, ?, 'USD', ?)",
            (invoice_id, amount, created_at),
        )
        return int(cur.lastrowid)

    def _reversal(self, payment_id: int, amount: str, created_at: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO payment_reversals (payment_id, amount, currency, created_at)"
            " VALUES (?, ?, 'USD', ?)",
            (payment_id, amount, created_at),
        )
        return int(cur.lastrowid)

    def _traced(self, fn):
        """执行 fn 并返回 (结果, 期间本连接执行的全部 SQL 语句列表)。"""

        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)
        try:
            result = fn()
        finally:
            self.conn.set_trace_callback(None)
        return result, statements

    # ---- 测试 ----

    def test_01_chunk_size_within_bind_budget(self) -> None:
        # 固定批量连同截止时点参数（+1）仍低于 SQLite 旧版构建的 999 绑定参数上限
        self.assertGreater(database._IN_CHUNK_SIZE, 0)
        self.assertLess(database._IN_CHUNK_SIZE + 1, 999)

    def test_02_empty_collections_issue_no_query(self) -> None:
        # 空 ID 集合：四个读取函数都不应发起任何 SQL
        _, statements = self._traced(
            lambda: (
                database._paid_totals(self.conn, []),
                database._paid_totals_as_of(self.conn, [], "2030-01-01T00:00:00Z"),
                database._reversed_totals(self.conn, []),
                database._reversed_totals(
                    self.conn, [], at_text="2030-01-01T00:00:00Z"
                ),
            )
        )
        self.assertEqual(statements, [])

        # 无收款的发票做历史快照：冲销明细的空集合同样不发起 IN 查询
        iid = self._invoice("100.00", None, "2026-01-01T00:00:00Z")
        inv = database.get_invoice(self.conn, iid)
        snap, statements = self._traced(
            lambda: database.payment_snapshot(
                self.conn, inv, datetime(2026, 6, 1, tzinfo=timezone.utc)
            )
        )
        self.assertEqual([s for s in statements if " IN (" in s], [])
        self.assertEqual(snap["payment_count"], 0)
        self.assertEqual(snap["reversal_count"], 0)
        self.assertEqual(snap["payments"], [])
        self.assertEqual(snap["reversals"], [])
        self.assertEqual(snap["outstanding"], "100.00")

    def test_03_huge_id_set_aging_and_overdue(self) -> None:
        # 1200 张发票（> 2 个分段），每张收款 10.00、冲销 1.00 -> 净入账 9.00
        n = 1200
        for _ in range(n):
            iid = self._invoice("100.00", "2026-01-01", "2026-01-01T00:00:00Z")
            pid = self._payment(iid, "10.00", "2026-01-02T00:00:00Z")
            self._reversal(pid, "1.00", "2026-01-03T00:00:00Z")
        self.conn.commit()

        as_of = date(2026, 1, 15)  # 到期 14 天 -> overdue_1_30

        aging, aging_sql = self._traced(
            lambda: database.receivables_aging_summary(self.conn, as_of)
        )
        self.assertEqual(aging["cutoff_at"], "2026-01-15T23:59:59Z")
        self.assertEqual(len(aging["currencies"]), 1)
        usd = aging["currencies"][0]
        self.assertEqual(usd["currency"], "USD")
        self.assertEqual(usd["invoice_count"], n)
        # 1200 × (100.00 − 9.00) = 109200.00
        self.assertEqual(usd["total_outstanding"], "109200.00")
        buckets = {b["bucket"]: b for b in usd["buckets"]}
        self.assertEqual(buckets["overdue_1_30"]["invoice_count"], n)
        self.assertEqual(buckets["overdue_1_30"]["outstanding"], "109200.00")

        # 确实按固定批量分段，且每条语句的绑定参数（含截止时点）不超预算
        in_statements = [s for s in aging_sql if " IN (" in s]
        # 收款 3 段（1200 / 500 向上取整）+ 冲销 3 段
        self.assertEqual(len(in_statements), 6)
        for sql in in_statements:
            self.assertLessEqual(sql.count("?"), database._IN_CHUNK_SIZE + 1)

        (items, total), overdue_sql = self._traced(
            lambda: database.list_overdue_invoices(self.conn, as_of, page=1, page_size=20)
        )
        self.assertEqual(total, n)
        self.assertEqual(len(items), 20)
        for item in items:
            self.assertEqual(item["total_gross"], "100.00")
            self.assertEqual(item["paid_total"], "9.00")
            self.assertEqual(item["outstanding"], "91.00")
        # 结果按 id 倒序
        self.assertGreater(items[0]["id"], items[1]["id"])
        overdue_in = [s for s in overdue_sql if " IN (" in s]
        self.assertEqual(len(overdue_in), 6)
        for sql in overdue_in:
            self.assertLessEqual(sql.count("?"), database._IN_CHUNK_SIZE + 1)

    def test_04_chunked_results_match_small_batch(self) -> None:
        # 混合数据集：多币种精度、多笔收款/分次冲销、有无到期日、截止时点前后流水
        paid_full = self._invoice("50.00", "2026-01-10", "2026-01-01T00:00:00Z")
        pid = self._payment(paid_full, "50.00", "2026-01-02T00:00:00Z")
        partial = self._invoice("80.00", "2026-01-05", "2026-01-01T00:00:00Z")
        p1 = self._payment(partial, "20.00", "2026-01-02T00:00:00Z")
        p2 = self._payment(partial, "15.00", "2026-01-03T00:00:00Z")
        self._reversal(p1, "5.00", "2026-01-04T00:00:00Z")
        self._reversal(p1, "2.50", "2026-01-05T00:00:00Z")
        self._reversal(p2, "15.00", "2026-01-06T00:00:00Z")
        no_due = self._invoice("30.00", None, "2026-01-01T00:00:00Z")
        self._payment(no_due, "10.00", "2026-01-02T00:00:00Z")
        jpy_like = self._invoice("1000", "2026-01-08", "2026-01-01T00:00:00Z", digits=0)
        pj = self._payment(jpy_like, "400", "2026-01-02T00:00:00Z")
        self._reversal(pj, "100", "2026-01-03T00:00:00Z")
        # 截止时点之后的流水不应计入快照/账龄
        late = self._invoice("60.00", "2026-01-06", "2026-01-01T00:00:00Z")
        self._payment(late, "60.00", "2026-07-01T00:00:00Z")
        untouched = self._invoice("25.00", "2026-01-09", "2026-01-01T00:00:00Z")
        self.conn.commit()

        as_of = date(2026, 2, 1)
        snap_inv = database.get_invoice(self.conn, partial)
        snap_at = datetime(2026, 1, 10, tzinfo=timezone.utc)

        def collect():
            return (
                database.receivables_aging_summary(self.conn, as_of),
                database.list_overdue_invoices(self.conn, as_of, page=1, page_size=100),
                database.payment_overview(
                    self.conn, database.get_invoice(self.conn, partial)
                ),
                database.payment_snapshot(self.conn, snap_inv, snap_at),
            )

        baseline = collect()

        # 强制极小批量，把每条 IN 查询放大成多段；结果必须与默认批量完全一致
        original = database._IN_CHUNK_SIZE
        database._IN_CHUNK_SIZE = 2
        try:
            chunked = collect()
        finally:
            database._IN_CHUNK_SIZE = original

        self.assertEqual(chunked, baseline)

        # 口径抽查（默认批量下的基线值本身也须正确）
        aging, (items, total), overview, snapshot = baseline
        usd = {c["currency"]: c for c in aging["currencies"]}["USD"]
        # USD 未收（混合入库精度同组，按最高入库位数 2 位格式化）：
        # partial 67.50 + no_due 20.00 + late 60.00 + untouched 25.00
        # + jpy_like 700（0 位精度、同币种同组）= 872.50（paid_full 已收满排除）
        self.assertEqual(usd["total_outstanding"], "872.50")
        self.assertEqual(usd["currency_digits"], 2)
        self.assertEqual(usd["invoice_count"], 5)
        # 逾期列表按当前净入账口径：late 的 7 月收款已将其收满而排除，
        # no_due 无到期日排除，余 partial / jpy_like / untouched 三张
        self.assertEqual(total, 3)
        self.assertEqual(overview["paid_total"], "12.50")
        self.assertEqual(overview["outstanding"], "67.50")
        self.assertEqual(snapshot["received_total"], "35.00")
        self.assertEqual(snapshot["reversed_total"], "22.50")
        self.assertEqual(snapshot["net_paid"], "12.50")
        self.assertEqual(snapshot["outstanding"], "67.50")

    def test_05_snapshot_reversals_stay_ordered_by_id(self) -> None:
        # 两笔收款交错冲销：p2 先冲（id 小）、p1 后冲（id 大）。
        # 按收款 ID 分段后各段分别 ORDER BY，合并必须恢复全局 id 升序。
        iid = self._invoice("100.00", None, "2026-01-01T00:00:00Z")
        p1 = self._payment(iid, "40.00", "2026-01-02T00:00:00Z")
        p2 = self._payment(iid, "30.00", "2026-01-03T00:00:00Z")
        r1 = self._reversal(p2, "10.00", "2026-01-04T00:00:00Z")
        r2 = self._reversal(p1, "5.00", "2026-01-05T00:00:00Z")
        self.conn.commit()
        inv = database.get_invoice(self.conn, iid)
        at = datetime(2026, 1, 10, tzinfo=timezone.utc)

        for chunk_size in (database._IN_CHUNK_SIZE, 1):
            original = database._IN_CHUNK_SIZE
            database._IN_CHUNK_SIZE = chunk_size
            try:
                snap = database.payment_snapshot(self.conn, inv, at)
            finally:
                database._IN_CHUNK_SIZE = original
            self.assertEqual(
                [r["id"] for r in snap["reversals"]], [r1, r2],
                f"chunk_size={chunk_size} 时冲销明细必须按流水 id 升序",
            )
            self.assertEqual([r["payment_id"] for r in snap["reversals"]], [p2, p1])
            self.assertEqual(snap["received_total"], "70.00")
            self.assertEqual(snap["reversed_total"], "15.00")
            self.assertEqual(snap["net_paid"], "55.00")

    def test_06_payment_overview_beyond_chunk_size(self) -> None:
        # 单张发票 600 笔收款（> 1 个分段），其中 100 笔各被冲销 0.50
        iid = self._invoice("700.00", None, "2026-01-01T00:00:00Z")
        payment_ids = [
            self._payment(iid, "1.00", "2026-01-02T00:00:00Z") for _ in range(600)
        ]
        for pid in payment_ids[:100]:
            self._reversal(pid, "0.50", "2026-01-03T00:00:00Z")
        self.conn.commit()

        overview, statements = self._traced(
            lambda: database.payment_overview(
                self.conn, database.get_invoice(self.conn, iid)
            )
        )
        self.assertEqual(overview["payment_count"], 600)
        # 600 × 1.00 − 100 × 0.50 = 550.00
        self.assertEqual(overview["paid_total"], "550.00")
        self.assertEqual(overview["reversed_total"], "50.00")
        self.assertEqual(overview["outstanding"], "150.00")
        # 流水按 id 升序返回，首笔冲销 0.50、第 101 笔起无冲销
        self.assertEqual(overview["payments"][0]["reversed_total"], "0.50")
        self.assertEqual(overview["payments"][0]["net_amount"], "0.50")
        self.assertEqual(overview["payments"][100]["reversed_total"], "0.00")
        self.assertEqual(overview["payments"][100]["net_amount"], "1.00")

        # 冲销合计按 2 段取数，且每段绑定参数不超预算
        in_statements = [s for s in statements if " IN (" in s]
        self.assertEqual(len(in_statements), 2)
        for sql in in_statements:
            self.assertLessEqual(sql.count("?"), database._IN_CHUNK_SIZE + 1)

    def test_07_decimal_merge_precision_preserved(self) -> None:
        # 分段合并仍在足量局部精度下逐笔累计：30 位整数净额量级不丢分位
        huge_gross = "123456789012345678901234567890.13"
        iid = self._invoice(huge_gross, "2026-01-01", "2026-01-01T00:00:00Z")
        # 600 笔 0.01 收款（强制 2 段），其中一笔被冲销 0.01
        payment_ids = [
            self._payment(iid, "0.01", "2026-01-02T00:00:00Z") for _ in range(600)
        ]
        self._reversal(payment_ids[0], "0.01", "2026-01-03T00:00:00Z")
        self.conn.commit()

        (items, total) = database.list_overdue_invoices(
            self.conn, date(2026, 2, 1), page=1, page_size=10
        )
        self.assertEqual(total, 1)
        # 净入账 600 × 0.01 − 0.01 = 5.99；未收 = gross − 5.99，分位不丢
        # （期望值直接写字面量：在默认 28 位精度下现场计算 30 位金额的减法
        #   本身就会被截断，无法用作断言基准）
        self.assertEqual(items[0]["paid_total"], "5.99")
        expected = "123456789012345678901234567884.14"
        self.assertEqual(items[0]["outstanding"], expected)

        aging = database.receivables_aging_summary(self.conn, date(2026, 2, 1))
        usd = aging["currencies"][0]
        self.assertEqual(usd["total_outstanding"], expected)


if __name__ == "__main__":
    unittest.main()
