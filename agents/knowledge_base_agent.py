"""KnowledgeBase Agent v1.0 - 个人知识库（索引 + 检索）

把"喂资料 → 存下来 → 以后能检索到"这条链路接进流水线。

与摄入层的组合是这里的重点：`ingest` 负责把 PDF/图片/文本变成 Markdown，
本 Agent 再切块向量化入库 —— 于是**扫描件里的内容也能被检索到**，
而不只是纯文本。

两个动作（由 payload 的 action 字段或存在哪些键推断）：
- `index`：把文件/内容切块入库
- `search`：按查询检索相关切块

设计要点：
- 库路径默认 `knowledge_base.db`（与 message_store/task_queue 同级约定）
- 嵌入器默认 auto（语义优先、自动回落），可显式指定
- 索引时如实回报每份资料的成败与所用后端，不静默吞错
"""
from pathlib import Path

from pipeline_core import embeddings as emb_core
from pipeline_core import ingest as ingest_core
from pipeline_core.base_agent import AgentStatus, BaseAgent, Message
from pipeline_core.knowledge_base import KnowledgeBase

AGENT_NAME = "knowledge_base"
AGENT_VERSION = "1.0"
AGENT_DESC = "知识库 Agent - 资料索引与向量检索（支持 PDF/图片经摄入层）"
AGENT_AUTHOR = "doc-pipeline"
AGENT_PRIORITY = 15
INPUT_TOPICS = ["kb.index", "kb.search", "kb.input", "ingest.done"]
OUTPUT_TOPICS = ["kb.done", "kb.failed", "kb.results", "kb.indexed"]
DEPENDENCIES: list[str] = []
CACHE_TTL = 0
RESPAWN = False


class KnowledgeBaseAgent(BaseAgent):
    def __init__(self, name, meta, config, message_bus, registry):
        super().__init__(name, meta, config, message_bus, registry)
        self._db_path = config.get("db_path", "knowledge_base.db")
        self._embedder_name = config.get("embedder", "auto")
        # 哈希后端需要显式维度；不传就会被静默忽略（配了 256 维实际用 1024）
        self._dim = int(config["dim"]) if config.get("dim") else None
        self._default_top_k = int(config.get("top_k", 5))
        self._max_per_doc = int(config.get("max_per_doc", 0))
        self._use_ingest = bool(config.get("use_ingest_layer", True))
        self._kb: KnowledgeBase | None = None
        self.log_info(
            f"KnowledgeBase v{AGENT_VERSION} 初始化完成"
            f"（库: {self._db_path}，嵌入器: {self._embedder_name}）"
        )

    @property
    def kb(self) -> KnowledgeBase:
        """惰性建库：避免 Agent 加载时就创建/打开数据库文件。"""
        if self._kb is None:
            self._kb = KnowledgeBase(
                self._db_path,
                embedder_name=self._embedder_name,
                dim=self._dim,
            )
            reasons = emb_core.auto_fallback_reasons()
            if reasons:
                for cand, why in reasons.items():
                    self.log_warning(f"嵌入后端 {cand} 不可用，已回落: {why}")
            self.log_info(f"知识库就绪（实际嵌入器: {self._kb._embedder.name}）")
        return self._kb

    def handle(self, msg: Message) -> dict | None:
        payload = msg.payload
        action = str(payload.get("action", "")).strip().lower()

        if not action:
            # 推断：有 query 就是检索，否则是索引
            action = "search" if payload.get("query") else "index"

        if action == "search":
            result = self._do_search(payload)
            self.publish("kb.results" if result.get("status") == "ok"
                         else "kb.failed", result)
        elif action == "index":
            result = self._do_index(payload)
            self.publish("kb.indexed" if result.get("status") == "ok"
                         else "kb.failed", result)
        else:
            result = {"status": "error",
                      "message": f"未知动作: {action}（支持 index / search）"}
            self.publish("kb.failed", result)
        return result

    # ── 索引 ──

    def _do_index(self, payload: dict) -> dict:
        self.report(AgentStatus.RUNNING, "索引资料...")
        docs: list[dict] = []

        # 直接给内容
        content = payload.get("content", "")
        if content:
            res = self.kb.add_document(
                content,
                source=payload.get("source", ""),
                title=payload.get("title", ""),
                meta=payload.get("meta"),
            )
            docs.append({"source": payload.get("source", "(inline)"), **res})

        # 给文件（走摄入层，支持 PDF/图片）
        for path in self._collect_files(payload):
            docs.append(self._index_one(path, payload))

        if not docs:
            return {"status": "error", "task_id": payload.get("task_id", ""),
                    "message": "未指定 content 或 files"}

        ok = [d for d in docs if d.get("status") == "ok"]
        failed = [d for d in docs if d.get("status") != "ok"]
        for d in failed:
            self.log_warning(
                f"索引失败 {Path(str(d.get('source', '?'))).name}: "
                f"{d.get('message', '')}")

        return {
            "status": "ok" if ok else "error",
            "task_id": payload.get("task_id", ""),
            "indexed": len(ok),
            "failed": len(failed),
            "chunks": sum(d.get("chunks", 0) for d in ok),
            "documents": docs,
            "stats": self.kb.stats(),
        }

    def _index_one(self, path: str, payload: dict) -> dict:
        """索引单个文件：非文本格式先经摄入层转 Markdown。"""
        p = Path(path)
        if not p.exists():
            return {"source": path, "status": "error",
                    "message": f"文件不存在: {p}"}

        text = ""
        via = "direct"

        if self._use_ingest and p.suffix.lower() in ingest_core.SUPPORTED_SUFFIXES:
            ing = ingest_core.ingest(p)
            if ing.get("status") == "ok" and ing.get("markdown"):
                text = ing["markdown"]
                via = f"ingest:{ing.get('backend', '?')}"
            elif ing.get("needs_ocr"):
                # 扫描件/图片需要 OCR —— 如实回报而非静默跳过
                return {"source": path, "status": "error",
                        "message": ing.get("message", "需要 OCR 后端"),
                        "needs_ocr": True}
            # 摄入失败则继续走下面的直读兜底

        if not text:
            try:
                raw = p.read_bytes()
            except OSError as e:
                return {"source": path, "status": "error", "message": str(e)}
            for enc in ("utf-8", "utf-8-sig", "gb18030", "gbk", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue

        if not text.strip():
            return {"source": path, "status": "error", "message": "内容为空"}

        res = self.kb.add_document(
            text,
            source=str(p),
            title=payload.get("title") or p.stem,
            meta={"via": via},
        )
        res["via"] = via
        return {"source": path, **res}

    # ── 检索 ──

    def _do_search(self, payload: dict) -> dict:
        query = str(payload.get("query", "")).strip()
        if not query:
            return {"status": "error", "message": "未指定查询词"}

        self.report(AgentStatus.RUNNING, f"检索: {query}")
        res = self.kb.search(
            query,
            top_k=int(payload.get("top_k", self._default_top_k)),
            min_score=float(payload.get("min_score", 0.0)),
            max_per_doc=int(payload.get("max_per_doc", self._max_per_doc)),
            source_prefix=str(payload.get("source_prefix", "")),
        )
        res["task_id"] = payload.get("task_id", "")
        res["query"] = query
        if res.get("status") == "ok":
            res["hits"] = len(res.get("results", []))
            self.report(AgentStatus.RUNNING, f"命中 {res['hits']} 个切块")
        else:
            self.log_warning(f"检索未完成: {res.get('message', '')}")
        return res

    # ── 工具 ──

    @staticmethod
    def _collect_files(payload: dict) -> list[str]:
        files: list[str] = []
        single = payload.get("file") or payload.get("path")
        if single:
            files.append(str(single))
        listed = payload.get("files") or payload.get("paths") or []
        if isinstance(listed, str):
            files.append(listed)
        else:
            files.extend(str(f) for f in listed)
        seen: set[str] = set()
        unique: list[str] = []
        for f in files:
            if f not in seen:
                seen.add(f)
                unique.append(f)
        return unique

    def get_info(self) -> dict:
        return {
            "db_path": self._db_path,
            "requested_embedder": self._embedder_name,
            "requested_dim": self._dim,
            "active_embedder": (self._kb._embedder.name if self._kb else None),
            "candidate_embedders": emb_core.available_embedders(),
            "fallback_reasons": emb_core.auto_fallback_reasons(),
            "use_ingest_layer": self._use_ingest,
        }

    def on_snapshot(self) -> dict:
        return {"db_path": self._db_path,
                "embedder": self._embedder_name,
                "dim": self._dim,
                "default_top_k": self._default_top_k}

    def on_stop(self):
        if self._kb is not None:
            self._kb.close_all()
            self._kb = None
