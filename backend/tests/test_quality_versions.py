"""版本生效台与岩心质量判定的业务口径测试。

运行：backend/.venv/bin/python -m unittest discover -s tests
（本机没有 venv 时：PYTHONPATH=backend python3 -m unittest discover -s backend/tests）
"""
from __future__ import annotations

import unittest

from app.services import quality_engine as engine
from app.services.core import CoreService
from app.services.quality_version import QualityVersionService
from app.services.sample_registry import SampleRegistryService
from app.store import store

CORE = CoreService()
VERSIONS = QualityVersionService()
SAMPLES = SampleRegistryService()


def reset() -> None:
    store.reset()


def find_core(code: str) -> dict:
    return next(row for row in store.rows("core") if row.get("岩心编号") == code)


def find_sample(code: str) -> dict:
    return next(row for row in store.rows("sample_registry") if row.get("送检编号") == code)


def make_version(code: str, rules: list[dict]) -> None:
    entry, error = VERSIONS.create_version({"版本号": code, "说明": f"{code} 口径"}, rules)
    assert not error, error
    entry, error = VERSIONS.submit_approval(code)
    assert not error, error
    entry, error = VERSIONS.approve(code)
    assert not error, error


# V2：BORE-0001 浅层阈值提到 90%；通配区间仍为 0~200m
V2_RULES = [
    {"所属钻孔": "BORE-0001", "深度起": 0.0, "深度止": 100.0, "最低采取率": 90.0},
    {"所属钻孔": "*", "深度起": 0.0, "深度止": 200.0, "最低采取率": 90.0},
]
# V3：再收紧通配阈值到 92%
V3_RULES = [
    {"所属钻孔": "BORE-0001", "深度起": 0.0, "深度止": 100.0, "最低采取率": 90.0},
    {"所属钻孔": "*", "深度起": 0.0, "深度止": 200.0, "最低采取率": 92.0},
]
# 覆盖不全：只覆盖 BORE-0001，BORE-0002 的岩心会落空 -> 发布必须失败
PARTIAL_RULES = [
    {"所属钻孔": "BORE-0001", "深度起": 0.0, "深度止": 100.0, "最低采取率": 80.0},
]


class RuleMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        reset()

    def test_core_sample_uses_borehole_and_depth_interval(self) -> None:
        # CORE-0002：BORE-0001 48m 落在 BORE-0001 专规 0~100（85%），采取率 88 -> 合格
        core = find_core("CORE-0002")
        matched = engine.find_rule_for_core(core)
        self.assertIsNotNone(matched)
        version, rule = matched
        self.assertEqual(version["版本号"], "QY-V1")
        self.assertEqual(rule["所属钻孔"], "BORE-0001")
        verdict = engine.evaluate(core, judged_at="2026-09-30T00:00:00")
        self.assertEqual(verdict["检验结论"], "合格")
        self.assertEqual(verdict["判定阈值"], 85.0)

    def test_wildcard_rule_for_other_boreholes(self) -> None:
        # CORE-0005：BORE-0002 65m 走 * 通规（90%），采取率 91 -> 合格
        core = find_core("CORE-0005")
        verdict = engine.evaluate(core, judged_at="2026-09-30T00:00:00")
        self.assertEqual(verdict["检验结论"], "合格")
        self.assertEqual(verdict["判定阈值"], 90.0)

    def test_threshold_conflict_latest_effective_wins(self) -> None:
        make_version("QY-V2", V2_RULES)
        detail, message = VERSIONS.publish()
        self.assertTrue(detail, message)
        # BORE-0001 48m 在 V1（85%）和 V2（90%）都有规则，冲突取最新生效的 V2
        core = find_core("CORE-0002")
        verdict = engine.evaluate(core, judged_at="2026-09-30T00:00:00")
        self.assertEqual(verdict["判定版本号"], "QY-V2")
        self.assertEqual(verdict["判定阈值"], 90.0)

    def test_depth_interval_half_open_and_exact_borehole_preferred(self) -> None:
        core = {"岩心编号": "X", "所属钻孔": "BORE-0001", "取样深度起": 100.0, "采取率": 99.0}
        # 100m 不属于 [0,100)，BORE-0001 专规不命中，应回落通规
        matched = engine.find_rule_for_core(core)
        self.assertIsNotNone(matched)
        _, rule = matched
        self.assertEqual(rule["所属钻孔"], "*")


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        reset()

    def test_catalog_writes_verdict_and_moves_to_review(self) -> None:
        entry, message = CORE.run_action(1, "地质编录")  # CORE-0001 采取率 92，阈值 85
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["status"], "待复检")
        self.assertTrue(entry["pending"])
        self.assertEqual(entry["检验结论"], "合格")
        self.assertEqual(entry["判定版本号"], "QY-V1")

    def test_confirm_review_then_send_freezes_conclusion(self) -> None:
        CORE.run_action(1, "地质编录")
        entry, message = CORE.run_action(1, "复检确认")
        self.assertEqual(entry["status"], "已编录")
        self.assertFalse(entry["pending"])
        entry, message = CORE.run_action(1, "送样分析")
        self.assertEqual(entry["status"], "送样中")
        self.assertEqual(entry["送样编号"], "SAMP-0004")
        self.assertEqual(entry["送样冻结结论"], "合格")
        self.assertEqual(entry["送样冻结版本号"], "QY-V1")
        # 送样清单在同一事务里写了关联行，且读到同一份判定
        items, total = SAMPLES.list_entries(page=1, size=100)
        linked = next(row for row in items if row["岩心编号"] == "CORE-0001")
        self.assertEqual(linked["检验结论"], "合格")
        self.assertEqual(linked["判定版本号"], "QY-V1")

    def test_illegal_transition_rejected(self) -> None:
        # CORE-0001 待编录不能直接送样
        entry, message = CORE.run_action(1, "送样分析")
        self.assertIsNone(entry)
        self.assertIn("不能执行", message)
        # CORE-0002 已编录不能重复编录
        entry, message = CORE.run_action(2, "地质编录")
        self.assertIsNone(entry)

    def test_catalog_without_effective_rule_is_not_saved(self) -> None:
        # 新钻孔没有任何规则能覆盖：判定写不进，编录动作整体失败，状态不动
        entry, missing = CORE.create_entry({
            "岩心编号": "CORE-X1",
            "所属钻孔": "BORE-9999",
            "取样深度起": 800.0,
            "取样深度止": 802.0,
            "采取率": 99.0,
        })
        self.assertFalse(missing)
        entry, message = CORE.run_action(entry["id"], "地质编录")
        self.assertIsNone(entry)
        self.assertIn("没有可套用的生效阈值规则", message)
        stored = next(row for row in store.rows("core") if row["岩心编号"] == "CORE-X1")
        self.assertEqual(stored["status"], "待编录")
        self.assertNotIn("检验结论", stored)


class PublishRecalcTests(unittest.TestCase):
    def setUp(self) -> None:
        reset()

    def test_publish_and_recalc_share_transaction_and_moves_to_review(self) -> None:
        make_version("QY-V2", V2_RULES)
        detail, message = VERSIONS.publish()
        self.assertTrue(detail, message)
        self.assertEqual(detail["published"], ["QY-V2"])
        # 早期已编录样本回溯重算：CORE-0002 采取率 88，V2 阈值 90 -> 不合格、待复检
        core2 = find_core("CORE-0002")
        self.assertEqual(core2["status"], "待复检")
        self.assertTrue(core2["pending"])
        self.assertTrue(core2["abnormal"])
        self.assertEqual(core2["检验结论"], "不合格")
        self.assertEqual(core2["判定版本号"], "QY-V2")
        self.assertEqual(core2["判定阈值"], 90.0)
        # CORE-0005 采取率 91 >= 90 -> 合格但仍转待复检
        core5 = find_core("CORE-0005")
        self.assertEqual(core5["status"], "待复检")
        self.assertEqual(core5["检验结论"], "合格")
        self.assertEqual(core5["判定版本号"], "QY-V2")

    def test_sent_and_returned_cores_keep_send_time_caliber(self) -> None:
        make_version("QY-V2", V2_RULES)
        detail, _ = VERSIONS.publish()
        self.assertTrue(detail)
        # 送样中的 CORE-0003、已归还的 CORE-0004 不参与重算，结论仍是送样当时的 V1
        for code in ("CORE-0003", "CORE-0004"):
            core = find_core(code)
            self.assertEqual(core["判定版本号"], "QY-V1")
            self.assertEqual(core["送样冻结版本号"], "QY-V1")
            self.assertIn(core["status"], ("送样中", "已归还"))
        # 待编录的 CORE-0001 也不重算（编录时才取最新口径）
        self.assertEqual(find_core("CORE-0001")["status"], "待编录")

    def test_publish_failure_rolls_back_everything(self) -> None:
        make_version("QY-V2", PARTIAL_RULES)
        detail, message = VERSIONS.publish()
        self.assertFalse(detail)
        self.assertIn("发布", message)
        # 整版回滚：版本没有生效
        self.assertEqual(VERSIONS.get_version("QY-V2")["状态"], "已审批")
        # 早期样本判定仍是 V1，也没被打成待复检
        core2 = find_core("CORE-0002")
        self.assertEqual(core2["status"], "已编录")
        self.assertEqual(core2["判定版本号"], "QY-V1")
        core5 = find_core("CORE-0005")
        self.assertEqual(core5["status"], "已编录")
        self.assertEqual(core5["判定版本号"], "QY-V1")
        # 没有保存任何未生效版本 V2 的判定
        self.assertFalse(
            any(row.get("判定版本号") == "QY-V2" for row in store.rows("core"))
        )

    def test_publish_never_silently_falls_back_to_old_threshold(self) -> None:
        # PARTIAL_RULES 里 BORE-0002 本可被 V1 通规兜住，但发布重算严格只认新版 -> 必须失败
        make_version("QY-V2", PARTIAL_RULES)
        detail, _ = VERSIONS.publish()
        self.assertFalse(detail)
        self.assertEqual(VERSIONS.get_version("QY-V2")["状态"], "已审批")

    def test_interrupted_publish_resumes_from_failed_version(self) -> None:
        # V2、V3 都审批通过；让 V2 重算 1 条后中断（CORE-0002 id=2 先被处理）
        make_version("QY-V2", V2_RULES)
        make_version("QY-V3", V3_RULES)
        detail, message = VERSIONS.publish(fail_after=1)
        self.assertFalse(detail)
        self.assertIn("QY-V2", message)
        checkpoint = VERSIONS.checkpoint()
        self.assertIsNotNone(checkpoint)
        self.assertEqual(checkpoint["failed_version"], "QY-V2")
        # 中断时 V2 已迁移的那一条随事务回滚：没有半生效状态
        self.assertEqual(VERSIONS.get_version("QY-V2")["状态"], "已审批")
        self.assertEqual(VERSIONS.get_version("QY-V3")["状态"], "已审批")
        self.assertFalse(
            any(row.get("判定版本号") == "QY-V2" for row in store.rows("core"))
        )
        # 续发：从失败的 V2 重新取（不是跳过它），两版按序生效
        detail, message = VERSIONS.publish()
        self.assertTrue(detail, message)
        self.assertEqual(detail["published"], ["QY-V2", "QY-V3"])
        self.assertIsNone(VERSIONS.checkpoint())
        self.assertEqual(VERSIONS.get_version("QY-V2")["状态"], "已生效")
        self.assertEqual(VERSIONS.get_version("QY-V3")["状态"], "已生效")
        # CORE-0005：V2 下合格(91>=90)，V3 阈值 92 -> 不合格，最终按最新版 V3
        core5 = find_core("CORE-0005")
        self.assertEqual(core5["检验结论"], "不合格")
        self.assertEqual(core5["判定版本号"], "QY-V3")
        self.assertEqual(core5["判定阈值"], 92.0)
        self.assertEqual(core5["status"], "待复检")

    def test_checkpoint_survives_rollback_and_blocks_new_batch(self) -> None:
        make_version("QY-V2", PARTIAL_RULES)
        detail, _ = VERSIONS.publish()
        self.assertFalse(detail)
        # 检查点在事务外，回滚后仍在
        checkpoint = VERSIONS.checkpoint()
        self.assertEqual(checkpoint["failed_version"], "QY-V2")
        # 又审批一个 V3：中断未恢复前，发布动作仍然先从失败的 V2 重新取
        make_version("QY-V3", V3_RULES)
        detail, message = VERSIONS.publish()
        self.assertFalse(detail)
        self.assertIn("QY-V2", message)
        self.assertEqual(VERSIONS.get_version("QY-V3")["状态"], "已审批")
        # 修好 V2 规则（已审批版本不能改，需要走驳回→改→重审）后续发两版
        VERSIONS.reject("QY-V2")
        error = VERSIONS.replace_rules("QY-V2", V2_RULES)
        self.assertFalse(error)
        VERSIONS.submit_approval("QY-V2")
        VERSIONS.approve("QY-V2")
        detail, message = VERSIONS.publish()
        self.assertTrue(detail, message)
        self.assertEqual(detail["published"], ["QY-V2", "QY-V3"])


class SameResultEverywhereTests(unittest.TestCase):
    def setUp(self) -> None:
        reset()

    def test_ledger_sample_list_and_review_todos_read_one_result(self) -> None:
        make_version("QY-V2", V2_RULES)
        detail, _ = VERSIONS.publish()
        self.assertTrue(detail)
        core2 = find_core("CORE-0002")
        # 1) 岩心台账
        ledger = CORE.get_entry(core2["id"])
        # 2) 编录待办
        todos = {row["岩心编号"]: row for row in CORE.review_todos()}
        self.assertIn("CORE-0002", todos)
        # 3) 送样清单（CORE-0003 关联 SAMP-0001）
        items, _ = SAMPLES.list_entries(page=1, size=100)
        sample0001 = next(row for row in items if row["送检编号"] == "SAMP-0001")
        core3 = find_core("CORE-0003")
        for field in ("检验结论", "判定版本号", "判定规则", "判定阈值", "判定时间"):
            self.assertEqual(sample0001[field], core3[field])
        # 台账与待办是同一份行对象内容
        for field in ("检验结论", "判定版本号", "判定阈值"):
            self.assertEqual(ledger[field], todos["CORE-0002"][field])

    def test_historical_send_conclusion_archived_at_send_time_caliber(self) -> None:
        # SAMP-0001 对应 CORE-0003：V1 阈值 90、采取率 81 -> 不合格，送样时冻结
        items, _ = SAMPLES.list_entries(page=1, size=100)
        sample = next(row for row in items if row["送检编号"] == "SAMP-0001")
        self.assertEqual(sample["送样冻结版本号"], "QY-V1")
        self.assertEqual(sample["送样冻结阈值"], 90.0)
        self.assertEqual(sample["送样冻结结论"], "不合格")
        # 发布 V3 后，权威列随台账保持 V1（该岩心不重算），冻结列也不动
        make_version("QY-V3", V3_RULES)
        detail, _ = VERSIONS.publish()
        self.assertTrue(detail)
        items, _ = SAMPLES.list_entries(page=1, size=100)
        sample = next(row for row in items if row["送检编号"] == "SAMP-0001")
        self.assertEqual(sample["检验结论"], "不合格")
        self.assertEqual(sample["判定版本号"], "QY-V1")
        self.assertEqual(sample["送样冻结版本号"], "QY-V1")


class VersionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        reset()

    def test_draft_rules_validated(self) -> None:
        entry, error = VERSIONS.create_version({"版本号": "QY-BAD"}, [])
        self.assertIsNone(entry)
        self.assertIn("至少要有一条", error)
        entry, error = VERSIONS.create_version(
            {"版本号": "QY-BAD"},
            [{"所属钻孔": "*", "深度起": 100.0, "深度止": 50.0, "最低采取率": 90.0}],
        )
        self.assertIsNone(entry)
        self.assertIn("深度区间非法", error)

    def test_effective_version_is_immutable(self) -> None:
        entry, error = VERSIONS.update_version(
            "QY-V1", {"说明": "改已生效版本"}, V2_RULES
        )
        self.assertIsNone(entry)
        self.assertIn("已生效", error)
        error = VERSIONS.replace_rules("QY-V1", V2_RULES)
        self.assertIn("已生效", error)

    def test_only_approved_versions_publish(self) -> None:
        entry, _ = VERSIONS.create_version({"版本号": "QY-V2"}, V2_RULES)
        detail, message = VERSIONS.publish()
        self.assertFalse(detail)
        self.assertIn("没有待发布", message)
        # 草稿判定不允许被使用：编录仍走 V1
        core1 = find_core("CORE-0001")
        verdict = engine.evaluate(core1, judged_at="2026-09-30T00:00:00")
        self.assertEqual(verdict["判定版本号"], "QY-V1")

    def test_reject_then_edit_resubmit_flow(self) -> None:
        VERSIONS.create_version({"版本号": "QY-V2"}, V2_RULES)
        VERSIONS.submit_approval("QY-V2")
        entry, _ = VERSIONS.reject("QY-V2", "阈值依据不足")
        self.assertEqual(entry["状态"], "已驳回")
        # 待审批时改不动，驳回后可以改
        error = VERSIONS.replace_rules("QY-V2", V3_RULES)
        self.assertFalse(error)
        VERSIONS.submit_approval("QY-V2")
        entry, _ = VERSIONS.approve("QY-V2")
        self.assertEqual(entry["状态"], "已审批")
        detail, message = VERSIONS.publish()
        self.assertTrue(detail, message)
        self.assertEqual(VERSIONS.get_version("QY-V2")["状态"], "已生效")


if __name__ == "__main__":
    unittest.main()
