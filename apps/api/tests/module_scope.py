"""作用域感知的静态检查：找出"引用了但在此作用域内解析不到"的名字。

为什么不能用"整模块 defined 集合"的粗暴版本（第一版就是这样，漏了真问题）：
工具类里有 `def _ensure_checkpoint(self, ...)` 这样的**方法**，方法名会被算成
"模块里已定义"，于是方法体里对**模块级同名函数** `_ensure_checkpoint(...)` 的
调用就被误判为已定义——拆 `fs_tools.py` 时正是这个漏洞让 14 个用例在运行期
`NameError`（静态检查当时是绿的）。

规则：
- 模块作用域可见：顶层赋值/def/class/import。
- 函数体内可见：自己的 locals（参数、赋值、for/with/except 目标、推导式目标...）
  + 外层函数的 locals（闭包）+ 模块作用域 + 内建。
- 类体可见：模块作用域 + 目前已经出现的类级赋值（方法名不进入方法体）。
"""

from __future__ import annotations

import ast
import builtins
import pathlib
import sys

BUILTINS = set(dir(builtins))
#: 允许"未定义即使用"的特殊名字。
#: 注意**不含 `logger`**：拆分时最容易漏的就是模块级 `logger`，如果把它放进白名单，
#: `logger.exception(...)` 这种漏 import 就查不出来了——拆 `fs_tools.py` 与
#: `execution.py` 各踩过一次（都是运行期 NameError，静态检查全绿）。
ALLOWED = {"self", "cls", "__name__", "__file__", "__doc__", "__package__", "__all__"}


def _local_names(node: ast.AST) -> set[str]:
    """一个函数（含其嵌套子树）里绑定出来的名字。"""
    names: set[str] = set()
    for child in ast.walk(node):
        if child is node:
            # 关键：不要把**函数自己的名字**算成本地名（否则 `def f(): f()` 这类
            # "方法名与模块级函数同名"的调用会被误判为已定义）
            continue
        if isinstance(child, ast.arg):
            names.add(child.arg)
        elif isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            names.add(child.id)
        elif isinstance(child, ast.ExceptHandler) and child.name:
            names.add(child.name)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            for alias in child.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(child.name)
        elif isinstance(child, ast.Global):
            names.update(child.names)
    return names


def _target_names(target) -> set[str]:
    """解包赋值/循环/with 目标里的名字（`a, (b, *c)` 这种也要取全）。"""
    if target is None:
        return set()
    if isinstance(target, ast.Name):
        return {target.id}
    names: set[str] = set()
    for child in ast.iter_child_nodes(target):
        names |= _target_names(child)
    return names


def _collect_scope_statements(statements) -> set[str]:
    names: set[str] = set()
    for node in statements:
        if isinstance(node, (ast.If, ast.Try)):
            names |= _collect_scope_statements(node.body)
            names |= _collect_scope_statements(node.orelse)
            for handler in getattr(node, "handlers", []):
                # `except ... as name` 绑定的名字属于**本作用域**。
                # 缺这一条会让模块级 try/except 的目标名被误报成「未定义名字」
                # （函数级那边走 _local_names，一直是采集的）—— RK-1 实测踩到。
                # 同时删掉一行恒为空的死代码（handlers and [] or []）。
                if handler.name:
                    names.add(handler.name)
                names |= _collect_scope_statements(handler.body)
            names |= _collect_scope_statements(getattr(node, "finalbody", []))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            # 模块级 for/with 的目标名同样属于本作用域；缺这两条会把
            # `for i in ...:` / `with open(...) as fh:` 的 i/fh 误报成未定义
            # （RK-1 实测：与 except-as 是同一类漏采）。
            names |= _target_names(node.target)
            names |= _collect_scope_statements(node.body)
            names |= _collect_scope_statements(node.orelse)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                names |= _target_names(item.optional_vars)
            names |= _collect_scope_statements(node.body)
        elif isinstance(node, ast.While):
            names |= _collect_scope_statements(node.body)
            names |= _collect_scope_statements(node.orelse)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names |= _target_names(target)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Global):
            names.update(node.names)
    return names


def _module_scope(tree: ast.Module) -> set[str]:
    return _collect_scope_statements(tree.body)


class _Checker(ast.NodeVisitor):
    def __init__(self, module_scope: set[str]) -> None:
        self.module_scope = module_scope
        self.stack: list[set[str]] = []
        self.missing: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load):
            return
        name = node.id
        if name in BUILTINS or name in ALLOWED or name in self.module_scope:
            return
        if any(name in scope for scope in self.stack):
            return
        self.missing.add(name)

    def _visit_function(self, node) -> None:
        locals_ = _local_names(node)
        # 装饰器与默认值在**外层**作用域求值
        for decorator in node.decorator_list:
            self.visit(decorator)
        args = node.args
        for default in list(args.defaults) + [d for d in args.kw_defaults if d]:
            self.visit(default)
        self.stack.append(locals_)
        for statement in node.body:
            self.visit(statement)
        self.stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def _visit_comprehension(self, node) -> None:
        targets: set[str] = set()
        for generator in node.generators:
            for child in ast.walk(generator.target):
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                    targets.add(child.id)
            self.stack.append(targets)
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
            self.stack.pop()
        self.stack.append(targets)
        for field in ("elt", "key", "value"):
            if hasattr(node, field):
                self.visit(getattr(node, field))
        self.stack.pop()

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_DictComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        class_scope: set[str] = set()
        for statement in node.body:
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        class_scope.add(target.id)
                with_class = list(self.stack)
                self.stack.append(class_scope)
                self.visit(statement.value)
                self.stack = with_class
                continue
            with_class = list(self.stack)
            self.stack.append(class_scope)
            self.visit(statement)
            self.stack = with_class


def check_file(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    checker = _Checker(_module_scope(tree))
    checker.visit(tree)
    return sorted(checker.missing)


def main(paths: list[str]) -> int:
    root = pathlib.Path(paths[0]) if paths else pathlib.Path("src/endless_task")
    problems = {}
    for path in sorted(root.rglob("*.py")):
        missing = check_file(path)
        if missing:
            problems[str(path)] = missing
    for path, missing in problems.items():
        print(f"{path}: {missing}")
    print("有问题的模块数:", len(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
