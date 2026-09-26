"""计算核心测试（仅依赖标准库，可在未安装 FastAPI 时运行）。

运行：python -m pytest tests/test_calc.py  或  python -m unittest tests.test_calc
"""

from __future__ import annotations

import threading
import unittest
from decimal import ROUND_HALF_UP, Decimal, localcontext

from app.calc import (
    RULE_ID,
    compute_invoice,
    digits_from_quantized_text,
    money_context,
    money_prec,
    recompute_matches,
)


def line(net: str, rate: str, desc: str | None = None) -> dict:
    return {"net": net, "tax_rate": rate, "description": desc}


def run_in_fresh_thread(fn):
    """在全新线程（Decimal 上下文为默认 28 位精度）中执行 fn。"""

    box: dict = {}

    def runner() -> None:
        try:
            box["result"] = fn()
        except Exception as exc:  # 测试需要把异常带回主线程断言
            box["error"] = exc

    t = threading.Thread(target=runner)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["result"]


class RoundingAndResidualTests(unittest.TestCase):
    def test_basic_half_up_and_no_residual(self):
        r = compute_invoice(
            {"currency": "USD", "lines": [line("100.00", "0.10"), line("50.00", "0.20")]}
        )
        self.assertEqual(r["lines"][0]["tax"], "10.00")
        self.assertEqual(r["lines"][1]["tax"], "10.00")
        self.assertEqual(r["totals"]["total_tax"], "20.00")
        self.assertEqual(r["totals"]["total_net"], "150.00")
        self.assertEqual(r["totals"]["total_gross"], "170.00")
        self.assertEqual(r["rounding"]["residual_units"], 0)
        self.assertEqual(r["rounding"]["allocations"], [])

    def test_residual_one_cent_largest_remainder(self):
        # 1.005 -> HALF_UP 1.01 ; 2.005 -> 2.01；行和 3.02，总额(30.10*0.1=3.01) -> 残差 -1
        r = compute_invoice(
            {
                "currency": "USD",
                "lines": [line("10.05", "0.10"), line("20.05", "0.10")],
            }
        )
        taxes = [l["tax"] for l in r["lines"]]
        # 零头相同（都是 0.005），负残差从零头小的先扣，相同则行号小的先扣
        self.assertEqual(taxes, ["1.00", "2.01"])
        self.assertEqual(sum(Decimal(t) for t in taxes), Decimal(r["totals"]["total_tax"]))
        self.assertEqual(r["rounding"]["residual_units"], -1)
        self.assertEqual(r["rounding"]["allocations"][0]["line_no"], 1)

    def test_largest_remainder_picks_biggest_fraction(self):
        # 经典案例：三行各 10.00 @3% -> 精确税 0.30/行，总额 0.90，无残差；
        # 改为净额 3.33 等使零头不同
        r = compute_invoice(
            {
                "currency": "USD",
                "lines": [
                    line("1.00", "0.10"),  # 0.10 exact
                    line("1.01", "0.10"),  # 0.101 -> 0.10, rem .001
                    line("1.05", "0.10"),  # 0.105 -> 0.11, rem .005
                ],
            }
        )
        # exact total = 0.306 -> 0.31；行舍入和 = 0.10+0.10+0.11 = 0.31，无残差
        self.assertEqual(r["totals"]["total_tax"], "0.31")
        self.assertEqual(r["rounding"]["residual_units"], 0)

    def test_positive_residual_allocated_to_largest_remainder(self):
        # 构造：每行精确税额零头都 < 0.5，但总额零头 >= 0.5
        # 两行 net 各 0.02 @0.25 不可行（net 须 0.01 整数倍）
        # net 0.03 @ 0.20 = 0.006 -> 0.01；三行：行和 0.03，精确总额 0.018 -> 0.02，残差 -1
        # 正残差场景：net 0.01 @ 0.30 = 0.003 -> 0.00(rounds down? HALF_UP .003 -> .00)
        # 4 行：行和 0.00，总额 0.012 -> 0.01，残差 +1，分给零头最大者(并列->line_no 1)
        r = compute_invoice(
            {
                "currency": "USD",
                "lines": [line("0.01", "0.30") for _ in range(4)],
            }
        )
        self.assertEqual(r["totals"]["total_tax"], "0.01")
        taxes = [l["tax"] for l in r["lines"]]
        self.assertEqual(taxes, ["0.01", "0.00", "0.00", "0.00"])
        self.assertEqual(r["rounding"]["residual_units"], 1)
        self.assertEqual(r["rounding"]["allocations"][0]["line_no"], 1)
        # 不变量：行税合计 == 总税
        self.assertEqual(
            sum(Decimal(t) for t in taxes), Decimal(r["totals"]["total_tax"])
        )

    def test_zero_decimal_currency_jpy(self):
        r = compute_invoice(
            {"currency": "JPY", "lines": [line("100", "0.10"), line("200", "0.10")]}
        )
        self.assertEqual(r["currency_digits"], 0)
        self.assertEqual(r["rounding"]["minor_unit"], "1")
        self.assertEqual(r["totals"]["total_tax"], "30")

    def test_three_decimal_currency_kwd(self):
        r = compute_invoice({"currency": "KWD", "lines": [line("1.001", "0.10")]})
        self.assertEqual(r["currency_digits"], 3)
        self.assertEqual(r["lines"][0]["tax"], "0.100")

    def test_half_up_005_rounds_up(self):
        # 0.005 HALF_UP -> 0.01
        r = compute_invoice({"currency": "USD", "lines": [line("0.05", "0.10")]})
        self.assertEqual(r["lines"][0]["exact_tax"], "0.005")
        self.assertEqual(r["lines"][0]["tax"], "0.01")

    def test_negative_net_credit_note(self):
        r = compute_invoice(
            {
                "currency": "USD",
                "lines": [line("-100.00", "0.10"), line("-100.00", "0.10")],
            }
        )
        self.assertEqual(r["totals"]["total_tax"], "-20.00")
        self.assertEqual(r["totals"]["total_gross"], "-220.00")

    def test_zero_tax_rate(self):
        r = compute_invoice({"currency": "USD", "lines": [line("100.00", "0")]})
        self.assertEqual(r["lines"][0]["tax"], "0.00")
        self.assertEqual(r["rounding"]["residual_units"], 0)


class FxTests(unittest.TestCase):
    def test_fx_keeps_original_and_converts(self):
        r = compute_invoice(
            {
                "currency": "USD",
                "target_currency": "CNY",
                "exchange_rate": "7.123456",
                "lines": [line("100.00", "0.13"), line("50.00", "0.06")],
            }
        )
        # 原币保留
        self.assertEqual(r["totals"]["total_net"], "150.00")
        self.assertEqual(r["lines"][0]["tax"], "13.00")
        # 换算：100*7.123456 = 712.3456 -> 712.35
        self.assertEqual(r["lines"][0]["converted"]["net"], "712.35")
        # 精确税 100*0.13*7.123456 = 92.604928 -> 92.60；
        # 行2: 50*0.06*7.123456 = 21.370368 -> 21.37；行舍入和 113.97，
        # 精确总额 113.975296 -> 113.98，残差 +1 分给零头更大的第 1 行 -> 92.61
        self.assertEqual(r["lines"][0]["converted"]["tax"], "92.61")
        self.assertEqual(r["lines"][1]["converted"]["tax"], "21.37")
        self.assertEqual(r["fx"]["total_tax"], "113.98")
        self.assertEqual(r["fx"]["residual_units"], 1)
        self.assertEqual(r["fx"]["target_currency"], "CNY")
        self.assertEqual(r["fx"]["exchange_rate"], "7.123456")
        self.assertIn("1 USD = 7.123456 CNY", r["fx"]["rate_direction"])
        # 目标币行税合计 == 目标币总税
        conv_tax_sum = sum(Decimal(l["converted"]["tax"]) for l in r["lines"])
        self.assertEqual(conv_tax_sum, Decimal(r["fx"]["total_tax"]))

    def test_fx_to_zero_digit_currency(self):
        r = compute_invoice(
            {
                "currency": "USD",
                "target_currency": "JPY",
                "exchange_rate": "150",
                "lines": [line("1.00", "0.10"), line("1.00", "0.10")],
            }
        )
        # 1*150 = 150；税 15/行，总额 30，无残差
        self.assertEqual(r["lines"][0]["converted"]["net"], "150")
        self.assertEqual(r["fx"]["total_tax"], "30")

    def test_fx_requires_rate(self):
        with self.assertRaises(ValueError):
            compute_invoice({"currency": "USD", "target_currency": "CNY",
                             "lines": [line("100.00", "0.1")]})

    def test_fx_same_currency_rejected(self):
        with self.assertRaises(ValueError):
            compute_invoice({"currency": "USD", "target_currency": "USD",
                             "exchange_rate": "1", "lines": [line("100.00", "0.1")]})


class ValidationTests(unittest.TestCase):
    def test_unknown_currency(self):
        with self.assertRaises(ValueError):
            compute_invoice({"currency": "ZZZ", "lines": [line("1.00", "0.1")]})

    def test_float_rejected(self):
        with self.assertRaises(ValueError):
            compute_invoice({"currency": "USD", "lines": [{"net": 1.0, "tax_rate": "0.1"}]})

    def test_net_not_multiple_of_minor_unit(self):
        with self.assertRaises(ValueError):
            compute_invoice({"currency": "USD", "lines": [line("1.005", "0.1")]})

    def test_rate_out_of_range(self):
        with self.assertRaises(ValueError):
            compute_invoice({"currency": "USD", "lines": [line("1.00", "1.5")]})

    def test_empty_lines(self):
        with self.assertRaises(ValueError):
            compute_invoice({"currency": "USD", "lines": []})


class DeterminismTests(unittest.TestCase):
    def test_recompute_is_identical(self):
        payload = {
            "currency": "USD",
            "target_currency": "CNY",
            "exchange_rate": "7.123456",
            "lines": [
                line("3.33", "0.13"),
                line("7.77", "0.06"),
                line("9.99", "0.13"),
            ],
        }
        r1 = compute_invoice(payload)
        r2 = compute_invoice(payload)
        self.assertEqual(r1, r2)
        self.assertTrue(recompute_matches(r1, payload))
        self.assertEqual(r1["rule_id"], RULE_ID)

    def test_tie_break_by_line_order(self):
        # 四行完全相同：正残差 1 时必须分给第 1 行（零头相同按行号）
        r = compute_invoice(
            {"currency": "USD", "lines": [line("0.01", "0.30") for _ in range(4)]}
        )
        assigned = [a["line_no"] for a in r["rounding"]["allocations"]]
        self.assertEqual(assigned, [1])


class LocalPrecisionTests(unittest.TestCase):
    """局部精度：计算结果不依赖调用线程的全局 Decimal 上下文。

    Decimal 上下文是线程本地的，FastAPI 工作线程拿到的是默认 28 位精度；
    compute_invoice 必须自带按输入量级计算的足够局部精度，任何线程下
    结果一致（含超大金额）。
    """

    BIG_NET = "100000000000000000000000000000.00"  # 30 位整数 + 2 位小数

    def _run_in_fresh_thread(self, fn):
        """在全新线程（Decimal 上下文为默认 28 位精度）中执行 fn。"""

        return run_in_fresh_thread(fn)

    def test_non_minor_unit_huge_net_rejected_in_worker_thread(self):
        # 26 位整数 + 3 位小数：28 位默认精度下 net/unit 的除法会把 .001 舍没，
        # 局部精度必须保证最小单位校验精确 -> 仍然拒绝（API 层映射 422）
        payload = {
            "currency": "USD",
            "lines": [line("10000000000000000000000000.001", "0.10")],
        }
        with self.assertRaises(ValueError):
            self._run_in_fresh_thread(lambda: compute_invoice(payload))

    def test_huge_30_digit_net_exact_in_worker_thread(self):
        payload = {"currency": "USD", "lines": [line(self.BIG_NET, "0.10")]}
        r = self._run_in_fresh_thread(lambda: compute_invoice(payload))
        self.assertEqual(r["lines"][0]["net"], self.BIG_NET)
        self.assertEqual(r["lines"][0]["exact_tax"], "10000000000000000000000000000")
        self.assertEqual(r["lines"][0]["tax"], "10000000000000000000000000000.00")
        self.assertEqual(r["totals"]["total_net"], "100000000000000000000000000000.00")
        self.assertEqual(r["totals"]["total_tax"], "10000000000000000000000000000.00")
        self.assertEqual(r["totals"]["total_gross"], "110000000000000000000000000000.00")
        self.assertTrue(
            self._run_in_fresh_thread(lambda: recompute_matches(r, payload))
        )

    def test_huge_net_with_long_rate_beyond_50_digits(self):
        # 30 位整数净额 × 31 位有效数字税率：精确税额近 60 位，
        # 固定 50 位精度也会被截断，局部精度必须随输入放大
        rate = "0.123456789012345678901234567890"
        payload = {"currency": "USD", "lines": [line(self.BIG_NET, rate)]}
        r = self._run_in_fresh_thread(lambda: compute_invoice(payload))
        with localcontext() as ctx:
            ctx.prec = 200
            expected_exact = Decimal(self.BIG_NET) * Decimal(rate)
            expected_tax = expected_exact.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        self.assertEqual(Decimal(r["lines"][0]["exact_tax"]), expected_exact)
        self.assertEqual(Decimal(r["totals"]["total_tax"]), expected_tax)
        self.assertTrue(recompute_matches(r, payload))

    def test_huge_net_fx_exact_in_worker_thread(self):
        payload = {
            "currency": "USD",
            "target_currency": "JPY",
            "exchange_rate": "150",
            "lines": [line(self.BIG_NET, "0.10")],
        }
        r = self._run_in_fresh_thread(lambda: compute_invoice(payload))
        self.assertEqual(
            r["lines"][0]["converted"]["net"], "15000000000000000000000000000000"
        )
        self.assertEqual(
            r["lines"][0]["converted"]["tax"], "1500000000000000000000000000000"
        )
        self.assertEqual(r["fx"]["total_net"], "15000000000000000000000000000000")
        self.assertEqual(r["fx"]["total_tax"], "1500000000000000000000000000000")
        self.assertEqual(r["fx"]["residual_units"], 0)
        self.assertTrue(
            self._run_in_fresh_thread(lambda: recompute_matches(r, payload))
        )

    def test_same_result_in_any_thread(self):
        payload = {
            "currency": "USD",
            "target_currency": "CNY",
            "exchange_rate": "7.123456",
            "lines": [line(self.BIG_NET, "0.13"), line("9.99", "0.06")],
        }
        r_main = compute_invoice(payload)
        r_worker = self._run_in_fresh_thread(lambda: compute_invoice(payload))
        self.assertEqual(r_main, r_worker)


class PinnedDigitsTests(unittest.TestCase):
    """入库时固定的币种小数位：读取/复算旧发票不随 CURRENCY_DIGITS 配置改变。"""

    def setUp(self) -> None:
        import os

        self._old = os.environ.get("CURRENCY_DIGITS")
        os.environ["CURRENCY_DIGITS"] = "USD:0,JPY:2"

    def tearDown(self) -> None:
        import os

        if self._old is None:
            os.environ.pop("CURRENCY_DIGITS", None)
        else:
            os.environ["CURRENCY_DIGITS"] = self._old

    def test_create_uses_current_config(self):
        # 当前配置 USD 为零位：0.01 净额创建时按当前配置被拒绝
        with self.assertRaises(ValueError):
            compute_invoice({"currency": "USD", "lines": [line("0.01", "0.10")]})
        r = compute_invoice({"currency": "USD", "lines": [line("5", "0.10")]})
        self.assertEqual(r["currency_digits"], 0)
        self.assertEqual(r["lines"][0]["tax"], "1")

    def test_pinned_source_digits_keep_old_precision(self):
        payload = {"currency": "USD", "lines": [line("0.01", "0.10")]}
        # 当前 USD 为零位，但显式固定两位（旧发票入库精度）仍按两位计算
        r = compute_invoice(payload, digits=2)
        self.assertEqual(r["currency_digits"], 2)
        self.assertEqual(r["rounding"]["minor_unit"], "0.01")
        self.assertEqual(r["lines"][0]["net"], "0.01")
        self.assertTrue(recompute_matches(r, payload, digits=2))
        # 不固定位数按当前配置复算：0.01 落不到零位最小单位（正是修复前 GET 旧发票
        # 返回 422 的根因），而固定旧精度的复算正常吻合
        with self.assertRaises(ValueError):
            recompute_matches(r, payload)

    def test_pinned_target_digits_keep_old_fx_precision(self):
        payload = {
            "currency": "USD",
            "target_currency": "JPY",
            "exchange_rate": "150.25",
            "lines": [line("1.00", "0.10")],
        }
        # 旧发票：USD 两位、JPY 零位；当前配置已把 JPY 改成两位
        r = compute_invoice(payload, digits=2, target_digits=0)
        self.assertEqual(r["fx"]["target_digits"], 0)
        self.assertEqual(r["lines"][0]["converted"]["net"], "150")
        self.assertEqual(r["fx"]["residual_unit"], "1")
        self.assertTrue(recompute_matches(r, payload, digits=2, target_digits=0))


class DigitsRecoveryTests(unittest.TestCase):
    """历史记录（无 target_digits 列）从已持久化金额恢复目标币精度。"""

    def test_digits_from_quantized_text(self):
        self.assertEqual(digits_from_quantized_text("150"), 0)
        self.assertEqual(digits_from_quantized_text("712.35"), 2)
        self.assertEqual(digits_from_quantized_text("0.100"), 3)
        self.assertIsNone(digits_from_quantized_text(None))
        self.assertIsNone(digits_from_quantized_text(""))
        self.assertIsNone(digits_from_quantized_text("1.23456"))


class MoneyContextTests(unittest.TestCase):
    """收支累计的局部精度策略（与 compute_invoice 的 _sufficient_prec 同源）。

    收款/冲销/余额的累计与量化在 money_context 内进行：默认 28 位精度的线程下，
    大额金额（如 30 位整数净额发票）的余额不丢分位、大额量化不抛 InvalidOperation。
    """

    BIG_GROSS = "110000000000000000000000000000.00"  # 32 位有效数字
    BIG_LESS_CENT = "109999999999999999999999999999.99"

    def test_huge_balance_keeps_cents_in_worker_thread(self):
        # 默认 28 位精度下 gross - 0.01 曾丢分位、大额 quantize 曾抛 InvalidOperation
        def work():
            gross = Decimal(self.BIG_GROSS)
            with money_context([gross, Decimal("0.01")]):
                outstanding = gross - Decimal("0.01")
                quantized = Decimal(self.BIG_LESS_CENT).quantize(Decimal("0.01"))
            return format(outstanding, "f"), format(quantized, "f")

        outstanding, quantized = run_in_fresh_thread(work)
        self.assertEqual(outstanding, self.BIG_LESS_CENT)
        self.assertEqual(quantized, self.BIG_LESS_CENT)

    def test_huge_sum_carry_in_worker_thread(self):
        # 多笔大额累计的进位同样精确（count 预留 log10(n) 进位）
        amounts = [Decimal(self.BIG_GROSS)] * 10
        with money_context(amounts, len(amounts)):
            total = sum(amounts, Decimal("0"))
        self.assertEqual(format(total, "f"), "1100000000000000000000000000000.00")

    def test_prec_scales_with_magnitude(self):
        # 常规金额维持 50 位基准；大额金额自动放大
        self.assertEqual(money_prec([Decimal("1.00")]), 50)
        self.assertGreater(money_prec([Decimal(self.BIG_GROSS)]), 50)
        self.assertEqual(money_prec([]), 50)

    def test_context_restores_default_precision(self):
        # 不读不写全局/线程上下文：退出后恢复默认 28 位
        import decimal

        before = decimal.getcontext().prec
        with money_context([Decimal(self.BIG_GROSS)]):
            self.assertGreater(decimal.getcontext().prec, before)
        self.assertEqual(decimal.getcontext().prec, before)


if __name__ == "__main__":
    unittest.main()
