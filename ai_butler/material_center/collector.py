"""P2 多源自动化采集链路。

计划书依据
----------
- 三、P2「开发**多源自动化爬虫采集链路**；原始素材自动生成 Agent-Wiki source.* 源页面，
  保存来源元数据」
- 三、P2「开发多源增量数据采集」（P1 同源要求）
- 五、3「统一底层采集底座：公共数据——天气 API、景区公开信息、交通实时状态；
  统一清洗、来源标记、去重」

设计
----
采集与清洗解耦：连接器只负责「拿到原始文档」，清洗/去重/落库由 materials 管道负责。
连接器统一走 SourceConnector 协议，便于把真实爬虫换成 API 或本地素材而无需改动下游。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol, Sequence

from ai_butler.material_center.cleaner import RawDocument

#: 采集端默认标识（实现参数；真实部署应填写可联系的 UA）
DEFAULT_USER_AGENT = "aibutler-material-collector/1.0"


class CollectError(RuntimeError):
    pass


class SourceConnector(Protocol):
    name: str

    def fetch(self, target: str) -> RawDocument:
        ...


@dataclass(frozen=True)
class CollectPolicy:
    """采集策略（计划书未给数值 → 实现参数）。"""

    min_interval_seconds: float = 1.0     # 对同一来源的礼貌间隔
    timeout_seconds: float = 20.0
    max_documents: int = 50
    user_agent: str = DEFAULT_USER_AGENT

    def __post_init__(self) -> None:
        if self.min_interval_seconds < 0:
            raise CollectError("min_interval_seconds 不得为负")
        if self.max_documents <= 0:
            raise CollectError("max_documents 必须为正数")


class StaticConnector:
    """直接给定内容（测试与已知素材）。"""

    name = "static"

    def __init__(self, documents: dict[str, RawDocument]) -> None:
        self._documents = dict(documents)

    def fetch(self, target: str) -> RawDocument:
        try:
            return self._documents[target]
        except KeyError as exc:
            raise CollectError(f"static 连接器没有目标 {target!r}") from exc


class LocalDirConnector:
    """从本地目录采集（离线素材、人工导入）。"""

    name = "local-dir"

    def __init__(self, root: str | Path, *, suffix: str = ".txt") -> None:
        self.root = Path(root)
        self.suffix = suffix

    def fetch(self, target: str) -> RawDocument:
        path = self.root / f"{target}{self.suffix}"
        if not path.is_file():
            raise CollectError(f"本地素材不存在：{path}")
        text = path.read_text(encoding="utf-8")
        title = text.splitlines()[0].strip() if text.strip() else target
        return RawDocument(doc_id=target, url=f"file://{path.as_posix()}",
                           title=title[:120], text=text, publisher="local",
                           fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    def targets(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.stem for p in self.root.glob(f"*{self.suffix}"))


class HttpConnector:
    """HTTP 采集。可注入 httpx.Client（测试用 MockTransport，无需真实网络）。"""

    name = "http"

    def __init__(self, *, policy: CollectPolicy | None = None, client=None,
                 doc_id_from: str = "url") -> None:
        self.policy = policy or CollectPolicy()
        self._client = client
        self._doc_id_from = doc_id_from

    def _new_client(self):
        if self._client is not None:
            return self._client
        import httpx
        return httpx.Client(timeout=self.policy.timeout_seconds, follow_redirects=True,
                            headers={"User-Agent": self.policy.user_agent})

    def fetch(self, target: str) -> RawDocument:
        client = self._new_client()
        try:
            response = client.get(target)
            response.raise_for_status()
        except CollectError:
            raise
        except Exception as exc:                     # noqa: BLE001 — 采集失败要能逐源降级
            raise CollectError(f"采集 {target} 失败：{type(exc).__name__}: {exc}") from exc

        # 编码兜底：中文站点常见声明缺失
        if not response.encoding or response.encoding.lower() == "iso-8859-1":
            response.encoding = response.charset_encoding or "utf-8"
        body = response.text

        doc_id = target if self._doc_id_from == "url" else target.rsplit("/", 1)[-1]
        return RawDocument(
            doc_id=doc_id, url=target, title=_extract_title(body) or target,
            text=body, publisher=_host_of(target),
            fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


def _extract_title(html: str) -> str:
    import re
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else ""


def _host_of(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).netloc or ""


@dataclass
class CollectResult:
    documents: list[RawDocument] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return len(self.documents)

    @property
    def failed_count(self) -> int:
        return len(self.failures)


class Collector:
    """按策略跨多个连接器采集。单源失败不阻断其余来源。"""

    def __init__(self, connectors: Sequence[SourceConnector], *,
                 policy: CollectPolicy | None = None) -> None:
        if not connectors:
            raise CollectError("至少需要一个采集连接器")
        self.connectors = list(connectors)
        self.policy = policy or CollectPolicy()

    def connectors_for(self, source: str) -> list[SourceConnector]:
        return [c for c in self.connectors if c.name == source]

    def collect(self, jobs: Iterable[tuple[str, str]]) -> CollectResult:
        """jobs: [(连接器名, 目标)]。逐条采集并遵守礼貌间隔。"""
        result = CollectResult()
        last_at: dict[str, float] = {}
        for source, target in jobs:
            if len(result.documents) >= self.policy.max_documents:
                break
            matched = self.connectors_for(source)
            if not matched:
                result.failures.append((f"{source}:{target}", f"没有名为 {source!r} 的连接器"))
                continue

            elapsed = time.monotonic() - last_at.get(source, -1e9)
            if elapsed < self.policy.min_interval_seconds:
                time.sleep(self.policy.min_interval_seconds - elapsed)
            last_at[source] = time.monotonic()

            for connector in matched:
                try:
                    result.documents.append(connector.fetch(target))
                    break
                except CollectError as exc:
                    result.failures.append((f"{source}:{target}", str(exc)))
        return result
