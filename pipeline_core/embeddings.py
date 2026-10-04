"""嵌入层：文本 → 稠密向量（可插拔后端）。

知识库的检索质量取决于这里的嵌入能力。三个后端，按可用性自动选：

| 后端 | 依赖 | 语义能力 | 适用 |
|---|---|---|---|
| `hash` | 无（内置） | **词法相似，非语义** | 默认/离线/CI/测试 |
| `local` | sentence-transformers（~100MB+） | 语义 | 单机高质量检索 |
| `api` | 网络 + API Key | 语义 | 已配 LLM 供应商时最省事 |

**诚实边界**：`hash` 后端是特征哈希 + 余弦相似度，本质仍是词法匹配
（类似 TF-IDF 的向量化形式），它能找到"用词相近"的内容，
**不能**理解同义词与语义改写（"营收" vs "收入" 匹配不上）。
需要真语义请装 `sentence-transformers` 或配 API Key。

关键实现约束：
1. **哈希必须用 hashlib 而非内置 hash()** —— 内置 hash 对 str 按进程随机
   加盐，同一文本在不同进程会得到不同向量，持久化的向量将全部失效
2. 向量用 float32 packed bytes 存储（无 numpy 依赖，体积是 JSON 的 1/4）
3. 后端切换后旧向量不可用 —— 每个 chunk 记录产出它的 embedder 名，
   不匹配时明确报错而非静默给出垃圾结果
"""
from __future__ import annotations

import contextlib
import hashlib
import math
import os
import re
import struct

DEFAULT_HASH_DIM = 1024

# CJK 单字（中文以字为语义单位，用 bigram 捕捉词序）
_RE_CJK = re.compile(r"[\u4e00-\u9fff]")
# 拉丁词
_RE_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9_'-]*")
# 数字（保留，便于匹配年份/指标）
_RE_NUM = re.compile(r"\d+(?:\.\d+)?")

# 拉丁停用词：高频无区分度，从特征里剔除
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at",
    "for", "with", "by", "from", "as", "is", "are", "was", "were", "be",
    "been", "it", "this", "that", "these", "those", "which", "who", "what",
})


# ────────────────────────────── 特征提取 ──────────────────────────────

def extract_features(text: str) -> dict[str, float]:
    """把文本拆成带权特征：CJK bigram + 拉丁词 + 数字。

    bigram 而非单字：单字碰撞太严重（"的"字遍地），bigram 能保留
    词序信息（"数据" vs "据数"）。长度不足 2 的中文串退化为单字。
    """
    feats: dict[str, float] = {}
    if not text:
        return feats

    # CJK：按连续字串切分，生成 bigram（单字串则用单字）
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            feats[f"c:{run}"] = feats.get(f"c:{run}", 0.0) + 1.0
        else:
            for i in range(len(run) - 1):
                g = run[i:i + 2]
                feats[f"c:{g}"] = feats.get(f"c:{g}", 0.0) + 1.0

    for w in _RE_WORD.findall(text.lower()):
        if w in _STOPWORDS or len(w) < 2:
            continue
        feats[f"w:{w}"] = feats.get(f"w:{w}", 0.0) + 1.0

    for n in _RE_NUM.findall(text):
        feats[f"n:{n}"] = feats.get(f"n:{n}", 0.0) + 1.0

    return feats


def _bucket(token: str, dim: int) -> tuple[int, float]:
    """特征 → (桶下标, 符号)。符号技巧可抵消部分哈希碰撞带来的偏差。"""
    h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    idx = int.from_bytes(h[:4], "big") % dim
    sign = 1.0 if h[4] & 1 else -1.0
    return idx, sign


# ────────────────────────────── 后端基类 ──────────────────────────────

class Embedder:
    """嵌入后端接口。"""

    name: str = "base"
    dim: int = DEFAULT_HASH_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


# ─────────────────────────── 哈希后端（内置）───────────────────────────

class HashEmbedder(Embedder):
    """特征哈希嵌入：无依赖、离线、确定性。**词法相似，非语义。**"""

    name = "hash"

    def __init__(self, dim: int = DEFAULT_HASH_DIM):
        if dim <= 0:
            raise ValueError("dim 必须为正整数")
        self.dim = dim
        # 身份必须含维度：1024 维与 256 维是**不同的向量空间**，
        # 同叫 "hash" 会让知识库的"换模型检测"漏判，静默返回空结果
        self.name = f"hash:{dim}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        feats = extract_features(text)
        if not feats:
            return vec

        for token, tf in feats.items():
            idx, sign = _bucket(token, self.dim)
            # 次线性 TF：高频词不应线性放大（与 TF-IDF 的 1+log(tf) 同思路）
            vec[idx] += sign * (1.0 + math.log(tf))

        # L2 归一化：让余弦相似度等价于点积，且长度无关
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


# ─────────────────────────── 本地模型后端（可选）───────────────────────────

class LocalEmbedder(Embedder):
    """sentence-transformers 本地模型。首次使用会下载模型权重。"""

    name = "local"

    def __init__(self, model: str = "paraphrase-multilingual-MiniLM-L12-v2"):
        try:
            from sentence_transformers import SentenceTransformer  # 可选依赖
        except ImportError as e:
            raise ValueError(
                "sentence-transformers 未安装（pip install sentence-transformers）"
            ) from e
        self._model_name = model
        try:
            self._model = SentenceTransformer(model)
        except Exception as e:
            # 最常见的失败是模型权重下载不通（huggingface.co 在部分网络下
            # 不可达），必须给出可操作的提示而不是原始堆栈
            raise ValueError(
                f"本地嵌入模型加载失败: {model}。"
                "常见原因：无法访问 huggingface.co（可用镜像 "
                "HF_ENDPOINT=https://hf-mirror.com 后重试），"
                "或模型名有误。原始错误: " + str(e)[:200]
            ) from e
        # 从模型读取真实维度，别假设
        self.dim = int(self._model.get_sentence_embedding_dimension())
        self.name = f"local:{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        arr = self._model.encode(texts, normalize_embeddings=True)
        return [list(map(float, row)) for row in arr]


# ──────────────────────────── API 后端（可选）───────────────────────────

class APIEmbedder(Embedder):
    """OpenAI 兼容 /embeddings 接口。

    读环境变量：EMBEDDING_API_KEY（回退 OPENAI_API_KEY）、
    EMBEDDING_BASE_URL（回退 OPENAI_BASE_URL）、EMBEDDING_MODEL。
    """

    name = "api"

    def __init__(self, model: str = "", api_key: str = "",
                 base_url: str = "", timeout: int = 30):
        self._model = model or os.environ.get(
            "EMBEDDING_MODEL", "text-embedding-3-small")
        self._key = api_key or os.environ.get("EMBEDDING_API_KEY", "") \
            or os.environ.get("OPENAI_API_KEY", "")
        self._base = (base_url or os.environ.get("EMBEDDING_BASE_URL", "")
                      or os.environ.get("OPENAI_BASE_URL", "")
                      or "https://api.openai.com/v1").rstrip("/")
        self._timeout = timeout
        if not self._key:
            raise ValueError(
                "缺少 API Key：设置 EMBEDDING_API_KEY 或 OPENAI_API_KEY")
        self.dim = 0        # 首次调用后从响应推断
        self.name = f"api:{self._model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        import requests
        resp = requests.post(
            f"{self._base}/embeddings",
            headers={"Authorization": f"Bearer {self._key}",
                     "Content-Type": "application/json"},
            json={"model": self._model, "input": texts},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        rows = sorted(data["data"], key=lambda d: d.get("index", 0))
        out = [[float(x) for x in row["embedding"]] for row in rows]
        if out and not self.dim:
            self.dim = len(out[0])
        return out


# ──────────────────────────── 工厂 ────────────────────────────

def available_embedders() -> list[str]:
    """列出**候选**嵌入后端。

    注意这是候选而非保证：`local` 只表示 sentence-transformers 库
    import 得到，其模型权重仍可能需要联网下载（实测部分网络下
    huggingface.co 不可达，加载会失败）。真正的可用性由
    `get_embedder("auto")` 逐个尝试构造来判定 —— 与 ingest 模块
    "能力探测必须真实例化" 的结论一致。
    """
    out = ["hash"]
    # 用 find_spec 而不是 import：sentence_transformers 会拉起 torch，
    # 本机实测冷导入 78 秒——而 auto 只是想知道"有没有这个候选"，
    # 真要用它（模型已缓存）才付得起加载成本。
    try:
        import importlib.util
        if importlib.util.find_spec("sentence_transformers") is not None:
            out.append("local")
    except (ImportError, ValueError):
        pass
    if os.environ.get("EMBEDDING_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        out.append("api")
    return out


# auto 模式下的尝试顺序：语义能力优先
_AUTO_ORDER = ("api", "local")

# 记录 auto 选择过程中各候选的失败原因，便于状态接口如实汇报
_AUTO_FALLBACK_REASONS: dict[str, str] = {}


def auto_fallback_reasons() -> dict[str, str]:
    """上次 auto 选择时各高优先级后端失败的原因（无失败则为空）。"""
    return dict(_AUTO_FALLBACK_REASONS)


DEFAULT_LOCAL_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"


def model_is_cached(model: str = DEFAULT_LOCAL_MODEL) -> bool | None:
    """本地是否已有模型权重：True 有 / False 没有 / None 判不了。

    auto 探测**必须先问这一步再决定是否构造**：`HF_HUB_OFFLINE=1` 并不足以
    拦住网络（实测 sentence-transformers 仍会去查 Hub 的 revision，
    huggingface.co 不可达时按 1/2/4/8/16s 退避重试 5 次，一个嵌入器探测
    就能吃掉数分钟，流水线节点表现为挂死）。
    """
    try:
        from huggingface_hub import try_to_load_from_cache
    except Exception:
        return None
    try:
        probe = try_to_load_from_cache(model, "config.json")
    except Exception:
        return None
    if probe is None:
        return False
    # _CACHED_NO_EXIST 是哨兵对象：明确"问过且没缓存"
    if isinstance(probe, str):
        return True
    return getattr(probe, "name", str(probe)) != "CACHED_NO_EXIST"


@contextlib.contextmanager
def _offline_probe_env():
    """auto 探测 local 后端期间禁止联网下载模型权重。

    "选哪个嵌入器"不该有下载 100MB 模型的副作用：模型已在本地缓存就用，
    没缓存就快速失败并回落 hash。实测 huggingface.co 不可达时，
    `get_embedder("auto")` 会在 SentenceTransformer 构造里重试数分钟，
    把流水线节点变成静默挂死。
    """
    keys = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ[k] = "1"
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def get_embedder(name: str = "auto", **kwargs) -> Embedder:
    """按名称取嵌入后端。

    name="auto" 时按 api → local → hash 顺序**逐个尝试构造**，
    失败即回落到下一个。只看 import 就选定候选会导致"选了却建不起来"
    （实测：sentence-transformers 装着但 huggingface.co 不可达），
    因此 auto 必须真正构造成功才算数。hash 是内置的，保证兜底。

    探测 local 时处于离线模式（不下载权重）；确实需要新下载模型的，
    请显式 `embedder: local`，那条路径允许联网拉取。
    """
    name = (name or "auto").strip().lower()

    if name in ("auto", ""):
        _AUTO_FALLBACK_REASONS.clear()
        for candidate in _AUTO_ORDER:
            if candidate not in available_embedders():
                continue
            if candidate == "local" and model_is_cached(
                    str(kwargs.get("model") or DEFAULT_LOCAL_MODEL)) is False:
                # 连缓存都没有就别去问 Hub：那次询问在断网机器上要退避重试 5 次
                _AUTO_FALLBACK_REASONS[candidate] = (
                    f"本地未缓存模型 {DEFAULT_LOCAL_MODEL}"
                    "（auto 不下载权重；需要语义嵌入请先联网下载一次模型，"
                    "或显式设 embedder: local / api）")
                continue
            try:
                if candidate == "local":
                    with _offline_probe_env():
                        return get_embedder(candidate, **kwargs)
                return get_embedder(candidate, **kwargs)
            except Exception as e:
                reason = str(e)[:300]
                if candidate == "local":
                    reason += "（auto 不下载模型权重；需要语义嵌入请先离线备好模型，" \
                              "或显式设 embedder: local）"
                _AUTO_FALLBACK_REASONS[candidate] = reason
                continue
        return HashEmbedder(dim=int(kwargs.get("dim", DEFAULT_HASH_DIM)))

    if name == "hash":
        return HashEmbedder(dim=int(kwargs.get("dim", DEFAULT_HASH_DIM)))
    if name == "local":
        return LocalEmbedder(model=kwargs.get("model", "") or
                             "paraphrase-multilingual-MiniLM-L12-v2")
    if name == "api":
        return APIEmbedder(model=kwargs.get("model", ""),
                           api_key=kwargs.get("api_key", ""),
                           base_url=kwargs.get("base_url", ""))
    raise ValueError(
        f"未知嵌入后端: {name}（候选: {', '.join(available_embedders())}）")


# ─────────────────────── 向量序列化（无 numpy 依赖）───────────────────────

def pack_vector(vec: list[float]) -> bytes:
    """float32 打包：体积约为 JSON 的 1/4，且免 numpy。"""
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_vector(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度。向量若已归一化，等价于点积（这里仍做完整计算以稳健）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)
