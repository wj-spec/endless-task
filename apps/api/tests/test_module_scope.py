"""`tests/module_scope.py` 检查器**自身**的用例：把「针」也钉住。

为什么需要这一条：`test_module_hygiene` 做的是**整包**扫描，它只证明
「当前 src 没有违规」，**不证明检查器本身能咬**。批次 G 的教训正是
「守卫很容易变成钉住了扫描器、没钉住针」。

RK-1 在这里实测踩到过三个同类漏采（全部是**模块级**，函数级一直是对的）：
`except ... as e` 的目标名、`for` 的目标名、`with ... as f` 的目标名。
见 `tests/module_scope.py` 里对应分支的注释。
"""
from __future__ import annotations

import pathlib
import tempfile
import unittest

from tests.module_scope import check_file


def _missing(source: str) -> list[str]:
    """把源码写进临时文件并跑检查器，返回被判定为未定义的名字。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "sample.py"
        path.write_text(source, encoding="utf-8")
        return check_file(path)


class ModuleScopeCheckerTest(unittest.TestCase):
    def test_reports_a_genuinely_undefined_name(self) -> None:
        """正向：真·未定义名必须被抓（否则检查器就是死的）。"""
        self.assertEqual(["never_defined"], _missing("print(never_defined)\n"))

    def test_module_level_bindings_are_recognised(self) -> None:
        """反向：模块级各种绑定形式都不许误报（RK-1 补的三个漏采）。"""
        cases = {
            "except_as": "try:\n    import os\nexcept Exception as exc:\n    print(exc)\n",
            "for_target": "for i in range(3):\n    print(i)\n",
            "with_target": "with open('/dev/null') as fh:\n    print(fh)\n",
            "tuple_unpack": "a, (b, c) = (1, (2, 3))\nprint(a, b, c)\n",
            "while_body": "n = 0\nwhile n < 3:\n    n = n + 1\nprint(n)\n",
            "try_finally": "try:\n    v = 1\nfinally:\n    print(v)\n",
        }
        for name, source in cases.items():
            with self.subTest(case=name):
                self.assertEqual([], _missing(source), name)

    def test_binding_fix_did_not_blind_the_checker(self) -> None:
        """★ 关键：补上「绑定采集」后，**语句体内**的真问题仍必须被抓。

        没有这一条，上面的修复就可以被改写成「把 try/for/with 整块忽略」，
        而用例照样全绿 —— 那正是批次 G 说的「钉住了扫描器、没钉住针」。
        """
        cases = {
            "except_body": "try:\n    import os\nexcept Exception as exc:\n    print(exc, still_undefined)\n",
            "for_body": "for i in range(3):\n    print(i, still_undefined)\n",
            "with_body": "with open('/dev/null') as fh:\n    print(fh, still_undefined)\n",
        }
        for name, source in cases.items():
            with self.subTest(case=name):
                self.assertEqual(["still_undefined"], _missing(source), name)
