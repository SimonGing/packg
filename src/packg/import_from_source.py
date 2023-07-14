"""
Utility to import everything from a module and check import path sanity.

In detail: check whether imports inside a library are correctly importing from the source file
and not from the library base __init__.py file. This ensures clean dependencies and avoids
circular imports.

https://github.com/amenck/circular_import_refactor/blob/test-imports-from-source/objects/tests/_import_from_source.py

Changes:
    - in visit_ImportFrom method replace error with dynamic import, before:
        assert hasattr(module, alias.name)
    - unprivate some names
    - change the import x from y instead of z to a warning since it can be false positive sometimes
        e.g. if the name matches with a public module

Usage:
    import pytest

    from packg.import_from_source import (
        apply_visitor, ImportFromSourceChecker, recurse_modules)

    module_list = list(recurse_modules("packagename", ignore_tests=True, packages_only=False))


    @pytest.mark.parametrize("module", module_list)
    def test_imports_from_source(module: str) -> None:
        print(f"Importing: {module}")
        apply_visitor(module=module, visitor=ImportFromSourceChecker(module))

Notes:
    - Currently does not respect try/except blocks around the imports and throws errors anyway
        - This functionality looks difficult to implement
        - Workaround: pass the list of false positives like this:
          ImportFromSourceChecker(module, module_list_to_ignore_not_found=["module.submodule"])
          Then all ModuleNotFoundErrors where the module name starts with a module in this list
          will be ignored.


"""
import logging
from ast import parse, NodeVisitor, ImportFrom
from importlib import util as import_util, import_module
from importlib.machinery import ModuleSpec
from os import path
from pkgutil import iter_modules
from typing import Any, List, Iterator, Optional


def _is_test_module(module_name: str) -> bool:
    components = module_name.split(".")

    return len(components) >= 2 and components[1] == "tests"


def _is_package(module_spec: ModuleSpec) -> bool:
    return module_spec.origin is not None and module_spec.origin.endswith("__init__.py")


def _recurse_modules(
    module_name: str, ignore_tests: bool, packages_only: bool
) -> Iterator[str]:
    if ignore_tests and _is_test_module(module_name):
        return

    module_spec = import_util.find_spec(module_name)

    if module_spec is not None and module_spec.origin is not None:
        if not (packages_only and not _is_package(module_spec)):
            yield module_name

        for child in iter_modules([path.dirname(module_spec.origin)]):
            if child.ispkg:
                yield from _recurse_modules(
                    f"{module_name}.{child.name}",
                    ignore_tests=ignore_tests,
                    packages_only=packages_only,
                )
            elif not packages_only:
                yield f"{module_name}.{child.name}"


class _ImportFromSourceChecker(NodeVisitor):
    def __init__(
        self, module: str, module_list_to_ignore_not_found: Optional[List] = None
    ):
        module_spec = import_util.find_spec(module)
        is_pkg = (
            module_spec is not None
            and module_spec.origin is not None
            and module_spec.origin.endswith("__init__.py")
        )

        self._module = module if is_pkg else ".".join(module.split(".")[:-1])
        self._top_level_module = self._module.split(".")[0]
        self._module_list_to_ignore_not_found = module_list_to_ignore_not_found

    def visit_ImportFrom(self, node: ImportFrom) -> Any:
        # Check that there are no relative imports that attempt to read from a parent module. We've found that there
        # generally is no good reason to have such imports.
        if node.level >= 2:
            raise ValueError(
                f"Import in {self._module} attempts to import from parent module using relative import. Please "
                f"switch to absolute import instead."
            )

        # Figure out which module to import in the case where this is a...
        if node.level == 0:
            # (1) absolute import where a submodule is specified
            assert node.module is not None
            module_to_import: str = node.module
        elif node.module is None:
            # (2) relative import where no module is specified (ie: "from . import foo")
            module_to_import = self._module
        else:
            # (3) relative import where a submodule is specified (ie: "from .bar import foo")
            module_to_import = f"{self._module}.{node.module}"

        # We're only looking at imports of objects defined inside this top-level package
        if not module_to_import.startswith(self._top_level_module):
            return

        # Actually import the module and iterate through all the objects potentially exported by it.
        print(f"    Importing module: {module_to_import}")
        try:
            module = import_module(module_to_import)
        except ModuleNotFoundError as e:
            if self._module_list_to_ignore_not_found is not None:
                for module_to_ignore_not_found in self._module_list_to_ignore_not_found:
                    if module_to_import.startswith(module_to_ignore_not_found):
                        print(f"        Ignore missing module: {module_to_import}")
                        return
            raise e
        for alias in node.names:
            # assert hasattr(module, alias.name), f"Imported {alias.name} from {module_to_import}, but this object does not exist. in {module}"
            if not hasattr(module, alias.name):
                if alias.name == "*":
                    continue
                attr = import_module(f"{module_to_import}.{alias.name}")
            else:
                attr = getattr(module, alias.name)

            # For some objects (pretty much everything except for classes and functions), we are not able to figure
            # out which module they were defined in... in that case there's not much we can do here, since we cannot
            # easily figure out where we *should* be importing this from in the first place.
            if isinstance(attr, type) or callable(attr):
                attribute_module = attr.__module__
            else:
                continue

            # Figure out where we should be importing this class from, and assert that the *actual* import we found
            # matches the place we *should* import from.
            should_import_from = self._get_module_should_import(
                module_to_import=attribute_module
            )
            if module_to_import != should_import_from:
                logging.warning(
                    f"(Potential false positive) "
                    f"Imported {alias.name} from {module_to_import}, "
                    f"which is not the public module where this object "
                    f"is defined. Please import from {should_import_from} instead."
                )

    def _get_module_should_import(self, module_to_import: str) -> str:
        """
        This function figures out the correct import path for "module_to_import" from the "self._module" module in
        this instance. The trivial solution here would be to always just return "module_to_import", but we want
        to actually take into account the fact that some submodules can be "private" (ie: start with an "_"), in
        which case we should only import from them if self._module is internal to that private module.
        """
        module_components = module_to_import.split(".")
        result: List[str] = []

        for component in module_components:
            if component.startswith("_") and not self._module.startswith(
                ".".join(result)
            ):
                break
            result.append(component)

        return ".".join(result)


def _apply_visitor(module: str, visitor: NodeVisitor) -> None:
    module_spec = import_util.find_spec(module)
    assert module_spec is not None
    assert module_spec.origin is not None

    with open(module_spec.origin, "r", encoding="utf-8") as source_file:
        ast = parse(source=source_file.read(), filename=module_spec.origin)

    visitor.visit(ast)


# unprivate some stuff
apply_visitor = _apply_visitor
ImportFromSourceChecker = _ImportFromSourceChecker
recurse_modules = _recurse_modules
