"""PDF 后端：优先 PyMuPDF（快、表格支持好），没有就退回纯标准库实现。

离线环境可以完全不安装任何第三方包：pip 不可用时直接用源码或 zipapp 运行。
"""
from __future__ import annotations

import importlib.util
import os


class BackendError(RuntimeError):
    pass


def _has_pymupdf() -> bool:
    return importlib.util.find_spec("pymupdf") is not None


class Doc:
    """统一的只读文档接口。"""

    def __init__(self, path: str, backend: str = "auto"):
        self.path = path
        want = os.environ.get("CHIPDOC_BACKEND", "auto") if backend == "auto" else backend
        if want == "auto":
            want = "pymupdf" if _has_pymupdf() else "pure"
        if want == "pymupdf":
            try:
                self._open_pymupdf()
            except Exception:
                if backend != "auto":
                    raise
                self._open_pure()
        elif want == "pure":
            self._open_pure()
        else:
            raise BackendError(f"未知后端: {backend}")
        self.name = self._name

    # -- PyMuPDF ------------------------------------------------------
    def _open_pymupdf(self) -> None:
        import pymupdf
        self._doc = pymupdf.open(self.path)
        self._name = f"pymupdf {pymupdf.__version__}"

    # -- 纯标准库 -----------------------------------------------------
    def _open_pure(self) -> None:
        from .pdfread import PDF
        self._pdf = PDF(self.path)
        self._name = "pure-python"

    # -- 统一接口 -----------------------------------------------------
    @property
    def page_count(self) -> int:
        if hasattr(self, "_doc"):
            return self._doc.page_count
        return self._pdf.page_count()

    def page_text(self, index: int) -> str:
        if hasattr(self, "_doc"):
            return self._doc[index].get_text("text")
        return self._pdf.page_text(index)

    def toc(self) -> list[tuple[int, str, int]]:
        if hasattr(self, "_doc"):
            return [(max(1, lvl), title, page) for lvl, title, page in self._doc.get_toc(simple=True)]
        return self._pdf.outline()

    def metadata(self) -> dict:
        if hasattr(self, "_doc"):
            return dict(self._doc.metadata or {})
        return self._pdf.metadata()

    def close(self) -> None:
        if hasattr(self, "_doc"):
            self._doc.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
