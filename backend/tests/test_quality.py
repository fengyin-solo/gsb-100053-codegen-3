"""版本生效台测试：规则套用、审批生效、同事务发布重算、断点续发、留档与同源读取。

只用标准库运行：cd backend && python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.seed import SEED_ROWS
from app.services import quality_engine
from app.services.core import CoreService
from app.services.quality import (
    PublishInterrupted,
    quality_service,
)
from app.store import store


def reset_store() -> None:
    """把单例仓库还原成种子状态（服务模块都引用同一个 store 对象）。"""
    store._tables = {
        name: copy.deepcopy(rows) for name, rows in SEED_ROWS.items()
    }
    quality_service.fault_version_id = None


V2_RULES = [
    {"borehole": None, "depth_from": 0, "depth_to": 100, "min_recovery": 0.9},
    {"borehole": None, "depth_from": 100, "depth_to": 9999, "min_recovery": 0.85},
    {"borehole": "ZK001", "depth_from": 50, "depth_to": 80, "min_recovery": 0.6},
]


def make_v2() -> int:
    version, error = quality_service.create_version({
        "title": "采取率口径收紧",
        "rules": V2_RULES,
    })
    assert error is None, error
    vid = version["id"]
    assert quality_service.submit_for_approval(vid)[1] is None
    assert quality_service.approve(vid)[1] is None
    return vid


class RuleEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()

    def test_按钻孔与深度套用_专用规则优先(self):
        version = quality_service.current_version()
        # ZK001 56m：同时落入通用 0-100(0.7) 与 ZK001 专用 50-80(0.6)，专用优先
        verdict = quality_engine.evaluate(
            version, borehole="ZK001", depth_from=56, recovery=0.62,
        )
        self.assertEqual(verdict["result"], "合格")
        self.assertEqual(verdict["matched_rule"]["min_recovery"], 0.6)

    def test_通用规则按深度区间判定(self):
        version = quality_service.current_version()
        verdict = quality_engine.evaluate(
            version, borehole="ZK002", depth_from=110, recovery=0.86,
        )
        self.assertEqual(verdict["matched_rule"]["min_recovery"], 0.8)
        self.assertEqual(verdict["result"], "合格")
        verdict_bad = quality_engine.evaluate(
            version, borehole="ZK002", depth_from=110, recovery=0.79,
        )
        self.assertEqual(verdict_bad["result"], "不合格")

    def test_未生效版本不允许保存判定(self):
        vid = make_v2()  # 审批通过但未发布
        draft = store.find("quality_rule_version", vid)
        with self.assertRaises(quality_engine.RuleError):
            quality_engine.evaluate(
                draft, borehole="ZK001", depth_from=12, recovery=0.88,
            )

    def test_规则校验(self):
        with self.assertRaises(quality_engine.RuleError):
            quality_engine.normalize_rules([
                {"borehole": None, "depth_from": 100, "depth_to": 50, "min_recovery": 0.8},
            ])
        with self.assertRaises(quality_engine.RuleError):
            quality_engine.normalize_rules([
                {"borehole": None, "depth_from": 0, "depth_to": 100, "min_recovery": 1.5},
            ])


class PublishRecalcTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()

    def test_发布即生效并回溯重算已编录岩心(self):
        vid = make_v2()
        result = quality_service.publish_pending()
        self.assertTrue(result["ok"])

        v1 = store.find("quality_rule_version", 1)
        v2 = store.find("quality_rule_version", vid)
        self.assertEqual(v1["status"], "已失效")
        self.assertEqual(v2["status"], "已生效")
        self.assertEqual(quality_service.current_version()["id"], vid)

        # CORE-0001：0.88 < 新通用阈值 0.90 → 重算不合格，转入待复检
        c1 = store.find("core", 1)
        self.assertEqual(c1["status"], "待复检")
        self.assertTrue(c1["pending"])
        concl1 = quality_service.current_conclusion(1)
        self.assertEqual(concl1["result"], "不合格")
        self.assertEqual(concl1["version_id"], vid)
        self.assertEqual(concl1["basis"], "换版重算")
        # CORE-0002：ZK001 专用 0.6 仍合格，但同样转入待复检
        self.assertEqual(store.find("core", 2)["status"], "待复检")
        self.assertEqual(quality_service.current_conclusion(2)["result"], "合格")
        # CORE-0004：100m 以深阈值升到 0.85，0.86 仍合格，也转待复检
        self.assertEqual(store.find("core", 4)["status"], "待复检")
        # CORE-0003 已归还（历史送样）：不重算、不动状态
        self.assertEqual(store.find("core", 3)["status"], "已归还")
        self.assertEqual(quality_service.current_conclusion(3)["version_id"], 1)

        # 旧结论不删除：同一条岩心结论历史可追溯
        history = quality_service.conclusion_history(1)
        self.assertEqual([c["version_id"] for c in history], [1, vid])
        self.assertFalse(history[0]["current"])
        self.assertTrue(history[1]["current"])

    def test_发布与重算同一事务_中断整体回滚(self):
        vid = make_v2()
        quality_service.fault_version_id = vid
        with self.assertRaises(PublishInterrupted):
            quality_service.publish_pending()

        # 版本未生效、旧版仍在位
        self.assertEqual(store.find("quality_rule_version", 1)["status"], "已生效")
        self.assertEqual(store.find("quality_rule_version", vid)["status"], "发布中")
        # 重算随事务回滚：状态没变、没有新增结论
        self.assertEqual(store.find("core", 1)["status"], "已编录")
        self.assertEqual(len(store.rows("quality_conclusion")), 4)
        # 失败游标独立提交，记住从哪一版恢复
        cursor = quality_service.checkpoint()
        self.assertEqual(cursor["failed_version_id"], vid)
        self.assertEqual(cursor["status"], "发布失败")

    def test_中断后从失败版重新取_不回落旧阈值(self):
        vid = make_v2()
        quality_service.fault_version_id = vid
        with self.assertRaises(PublishInterrupted):
            quality_service.publish_pending()

        # 再次发布：从失败的 V2 续发
        result = quality_service.publish_pending()
        self.assertEqual(result["resumed_from"], vid)
        self.assertEqual(quality_service.current_version()["id"], vid)
        c1 = quality_service.current_conclusion(1)
        self.assertEqual(c1["version_id"], vid)
        self.assertEqual(c1["result"], "不合格")
        self.assertEqual(store.find("core", 1)["status"], "待复检")
        cursor = quality_service.checkpoint()
        self.assertIsNone(cursor["failed_version_id"])

        # 禁止回落到旧阈值：把游标人为拨回旧版再发布，必须被拒绝
        cursor["failed_version_id"] = 1
        with self.assertRaises(PublishInterrupted):
            quality_service.publish_pending()
        # 旧版没有被重新激活
        self.assertEqual(store.find("quality_rule_version", 1)["status"], "已失效")

    def test_多版本顺序发布_最新审批生效版为准(self):
        v2 = make_v2()
        v3, error = quality_service.create_version({
            "title": "再收紧",
            "rules": [{"borehole": None, "depth_from": 0, "depth_to": 9999, "min_recovery": 0.99}],
        })
        self.assertIsNone(error)
        self.assertIsNone(quality_service.submit_for_approval(v3["id"])[1])
        self.assertIsNone(quality_service.approve(v3["id"])[1])

        result = quality_service.publish_pending()
        self.assertEqual([p["version_id"] for p in result["published"]], [v2, v3["id"]])
        self.assertEqual(quality_service.current_version()["id"], v3["id"])
        self.assertEqual(store.find("quality_rule_version", v2)["status"], "已失效")


class SubmissionArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.core_service = CoreService()

    def test_送样按当时口径冻结留档_换版不改写(self):
        # V1 口径下把 CORE-0004 送样（结论合格、版本 V1、阈值 0.8）
        entry, message = self.core_service.run_action(4, "送样分析")
        self.assertIsNotNone(entry, message)
        subs_before = [s for s in store.rows("quality_submission") if s["core_id"] == 4]
        self.assertEqual(len(subs_before), 1)
        frozen = dict(subs_before[0])

        make_v2()
        quality_service.publish_pending()

        subs_after = [s for s in store.rows("quality_submission") if s["core_id"] == 4][0]
        # 冻结行一字不改：历史送样结论按送样当时口径留档
        for key in ("frozen_result", "frozen_recovery", "frozen_version_id",
                    "frozen_version_no", "frozen_rule", "frozen_conclusion_id"):
            self.assertEqual(subs_after[key], frozen[key], f"{key} 不应随换版变化")
        self.assertEqual(subs_after["frozen_version_no"], "V2026-09-01")
        # 送样中的岩心不参与回溯重算
        self.assertEqual(store.find("core", 4)["status"], "送样中")


class SameSourceReadTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()

    def test_台账送样清单编录待办读到同一份结果(self):
        # CORE-0003 有送样留档：台账与送样清单必须指向同一条结论记录
        ledger3 = next(row for row in quality_service.ledger(size=100)[0] if row["id"] == 3)
        sub3 = quality_service.submissions(size=100)[0][0]
        self.assertEqual(ledger3["conclusion_id"], 3)
        self.assertEqual(sub3["conclusion_id"], 3)
        self.assertEqual(ledger3["检验结论"], sub3["检验结论"])
        self.assertEqual(ledger3["口径版本"], sub3["口径版本"])

        vid = make_v2()
        quality_service.publish_pending()

        # 换版后：台账与编录待办同样指向同一条重算结论
        ledger = {row["id"]: row for row in quality_service.ledger(size=100)[0]}
        todos = {row["id"]: row for row in quality_service.review_todos(size=100)[0]}
        self.assertEqual(set(todos), {1, 2, 4})
        for core_id in (1, 2, 4):
            self.assertEqual(
                todos[core_id]["conclusion_id"],
                ledger[core_id]["conclusion_id"],
                "编录待办与岩心台账必须读到同一份结论",
            )
            self.assertEqual(todos[core_id]["口径版本"], ledger[core_id]["口径版本"])
            self.assertEqual(ledger[core_id]["口径版本"], store.find("quality_rule_version", vid)["version_no"])
        # 已归还的 CORE-0003 不进待办，台账与留档仍是 V1 同一条
        self.assertNotIn(3, todos)
        sub3_after = quality_service.submissions(size=100)[0][0]
        self.assertEqual(sub3_after["conclusion_id"], ledger[3]["conclusion_id"])


class CoreActionTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.core_service = CoreService()

    def test_新编录按当前生效版本判定_无生效版本拒绝(self):
        # CORE-0005 采取率 0.55，V1 通用阈值 0.7 → 不合格
        entry, message = self.core_service.run_action(5, "地质编录")
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["status"], "已编录")
        self.assertEqual(entry["检验结论"], "不合格")
        self.assertEqual(entry["口径版本"], "V2026-09-01")

        # 未生效版本不能用于判定：人为让全部版本失效
        for row in store.rows("quality_rule_version"):
            row["status"] = "已失效"
        store.rows("core").append({
            "id": 6, "status": "待编录", "pending": True, "abnormal": False,
            "岩心编号": "CORE-0006", "所属钻孔": "ZK003",
            "取样深度起": 20, "取样深度止": 22, "采取率": 0.99,
        })
        entry, message = self.core_service.run_action(6, "地质编录")
        self.assertIsNone(entry)
        self.assertIn("已生效", message)
        self.assertEqual(store.find("core", 6)["status"], "待编录")

    def test_区间未覆盖时判定不落库(self):
        vid = make_v2()
        quality_service.publish_pending()
        # V2 最大区间到 9999，造一条深度 12000 的岩心：无适用规则，编录被拒且不留判定
        store.rows("core").append({
            "id": 6, "status": "待编录", "pending": True, "abnormal": False,
            "岩心编号": "CORE-0006", "所属钻孔": "ZK009",
            "取样深度起": 12000, "取样深度止": 12002, "采取率": 0.99,
        })
        entry, message = self.core_service.run_action(6, "地质编录")
        self.assertIsNone(entry)
        self.assertIn("未覆盖", message)
        self.assertIsNone(quality_service.current_conclusion(6))

    def test_待复检复检通过仍不合格则挂起_改值后合格回归(self):
        make_v2()
        quality_service.publish_pending()
        # CORE-0001 按 V2 不合格：复检通过被拒，继续待复检
        entry, message = self.core_service.run_action(1, "复检通过")
        self.assertIsNone(entry)
        self.assertIn("仍不合格", message)
        self.assertEqual(store.find("core", 1)["status"], "待复检")

        # 采取率补采到 0.95 后复检：合格，回已编录，追加复检结论
        store.find("core", 1)["采取率"] = 0.95
        entry, message = self.core_service.run_action(1, "复检通过")
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["status"], "已编录")
        self.assertEqual(entry["检验结论"], "合格")
        self.assertEqual(quality_service.current_conclusion(1)["basis"], "复检")


if __name__ == "__main__":
    unittest.main()
