"""doc-pipeline 性能基准测试 —— 量化各项优化收益。

运行方式:
    python benchmark.py              # 全部基准
    python benchmark.py --quick      # 快速模式（减少迭代次数）

基准项:
    1. HTML 正文提取: selectolax vs 正则
    2. 缓存层: CacheManager get/set 吞吐
    3. 并行执行: ThreadPool vs ProcessPool vs 串行
    4. SSE 流式: chunk 传播延迟
    5. TF-IDF 语义匹配: 大规模段落评分
"""
from __future__ import annotations

import time
import json
import sys
import hashlib
import re
from pathlib import Path

# 确保 import 路径
sys.path.insert(0, str(Path(__file__).parent))

QUICK = "--quick" in sys.argv
ITERATIONS = 3 if QUICK else 10
LARGE_HTML_SIZE = 500_000  # 模拟大页面


def _gen_mock_html(size: int = 100_000) -> str:
    """生成模拟 HTML 页面"""
    para = "<p>This is a paragraph about Apache Kafka, a distributed event streaming platform. " \
           "It is used for high-throughput, low-latency data pipelines. " \
           "Kafka publishes records to topics, which are partitioned for scalability.</p>"
    nav = '<nav><a href="/">Home</a><a href="/about">About</a></nav>'
    footer = '<footer><p>Copyright 2024. All rights reserved.</p></footer>'
    body = para * (size // len(para) + 1)
    return f"<html><head><title>Test</title></head><body>{nav}{body[:size]}{footer}</body></html>"


def bench_html_extraction():
    """基准 1: HTML 正文提取 —— selectolax vs 正则"""
    html = _gen_mock_html(LARGE_HTML_SIZE)
    results = {}

    # selectolax
    try:
        from selectolax.parser import HTMLParser

        def _selectolax_extract(html_str):
            tree = HTMLParser(html_str)
            for tag in ("nav", "footer", "aside", "script", "style"):
                for node in tree.css(tag):
                    node.decompose()
            return tree.body.text(separator=" ", strip=True) if tree.body else ""

        t0 = time.perf_counter()
        for _ in range(ITERATIONS):
            _selectolax_extract(html)
        results["selectolax"] = (time.perf_counter() - t0) / ITERATIONS
    except ImportError:
        results["selectolax"] = None

    # 正则
    def _regex_extract(html_str):
        text = re.sub(r"<script[^>]*>.*?</script>", "", html_str, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<nav[^>]*>.*?</nav>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<footer[^>]*>.*?</footer>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;|&amp;|&lt;|&gt;", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    t0 = time.perf_counter()
    for _ in range(ITERATIONS):
        _regex_extract(html)
    results["regex"] = (time.perf_counter() - t0) / ITERATIONS

    if results["selectolax"] and results["regex"]:
        results["speedup"] = results["regex"] / results["selectolax"]

    return results


def bench_cache_throughput():
    """基准 2: CacheManager get/set 吞吐"""
    from pipeline_core.cache_manager import CacheManager

    cache = CacheManager(name="bench", max_size=10000, ttl=3600)
    n = 5000

    # SET
    t0 = time.perf_counter()
    for i in range(n):
        cache.set(f"key_{i}", f"value_{i}" * 100)
    set_time = time.perf_counter() - t0

    # GET (hit)
    t0 = time.perf_counter()
    for i in range(n):
        cache.get(f"key_{i}")
    get_hit_time = time.perf_counter() - t0

    # GET (miss)
    t0 = time.perf_counter()
    for i in range(n):
        cache.get(f"miss_{i}")
    get_miss_time = time.perf_counter() - t0

    return {
        "set_ops_per_sec": n / set_time,
        "get_hit_ops_per_sec": n / get_hit_time,
        "get_miss_ops_per_sec": n / get_miss_time,
        "set_ms_per_op": set_time / n * 1000,
        "get_hit_ms_per_op": get_hit_time / n * 1000,
    }


def _cpu_work(x):
    """CPU 密集型工作（模块级，可 pickle）"""
    total = 0
    for i in range(100_000):
        total += i * x
    return total


def bench_parallel_execution():
    """基准 3: 并行执行 —— ThreadPool vs ProcessPool vs 串行"""
    from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
    from pipeline_core.executor_factory import create_executor

    n = 8
    results = {}

    # 串行
    t0 = time.perf_counter()
    [_cpu_work(i) for i in range(n)]
    results["serial"] = time.perf_counter() - t0

    # ThreadPool
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(_cpu_work, range(n)))
    results["thread_pool"] = time.perf_counter() - t0

    # ProcessPool
    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=4) as pool:
        list(pool.map(_cpu_work, range(n)))
    results["process_pool"] = time.perf_counter() - t0

    results["thread_speedup"] = results["serial"] / results["thread_pool"]
    results["process_speedup"] = results["serial"] / results["process_pool"]

    return results


def bench_streaming_overhead():
    """基准 4: SSE 流式 chunk 传播开销"""
    from pipeline_core.streaming import StreamCallback

    callback = StreamCallback()
    n = 1000
    chunk = "Hello World, this is a test chunk. "

    t0 = time.perf_counter()
    for i in range(n):
        callback.on_chunk(chunk, section_index=0)
    emit_time = time.perf_counter() - t0

    # 消费
    t0 = time.perf_counter()
    events = callback.get_events()
    consume_time = time.perf_counter() - t0

    return {
        "emit_ops_per_sec": n / emit_time,
        "emit_ms_per_op": emit_time / n * 1000,
        "consume_ms": consume_time * 1000,
        "events_buffered": len(events),
    }


def bench_tfidf():
    """基准 5: TF-IDF 语义匹配"""
    import numpy as np

    # 模拟 500 段落，每段 200 词
    n_paragraphs = 500
    n_keywords = 10
    paragraphs = []
    for i in range(n_paragraphs):
        words = [f"word_{(i + j) % 1000}" for j in range(200)]
        paragraphs.append({"text": " ".join(words), "title": f"Doc {i}", "url": f"http://ex{i}.com"})
    keywords = [f"word_{i}" for i in range(n_keywords)]

    t0 = time.perf_counter()

    # 构建词汇表
    all_docs = []
    for p in paragraphs:
        words = re.findall(r'[\w]{2,}', p["text"].lower())
        all_docs.append(words)

    vocab_set = set()
    for doc in all_docs:
        vocab_set.update(doc)
    vocab = sorted(vocab_set)
    vocab_idx = {w: i for i, w in enumerate(vocab)}
    n_docs = len(all_docs)
    n_terms = len(vocab)

    tfidf = np.zeros((n_docs, n_terms), dtype=np.float32)
    for i, doc in enumerate(all_docs):
        for w in doc:
            if w in vocab_idx:
                tfidf[i, vocab_idx[w]] += 1

    df = np.zeros(n_terms, dtype=np.float32)
    for i in range(n_docs):
        df += (tfidf[i] > 0).astype(np.float32)
    idf = np.log((n_docs + 1) / (df + 1)) + 1
    tfidf *= idf.reshape(1, -1)

    query_vec = np.zeros(n_terms, dtype=np.float32)
    for w in keywords:
        if w in vocab_idx:
            query_vec[vocab_idx[w]] = 1.0
    norm = np.linalg.norm(query_vec)
    if norm > 0:
        query_vec = query_vec / norm

    doc_norms = np.linalg.norm(tfidf, axis=1, keepdims=True)
    doc_norms[doc_norms == 0] = 1
    similarities = (tfidf / doc_norms) @ query_vec
    sorted_indices = np.argsort(-similarities)

    elapsed = time.perf_counter() - t0

    return {
        "n_paragraphs": n_paragraphs,
        "n_terms": n_terms,
        "elapsed_ms": elapsed * 1000,
        "top_score": float(similarities[sorted_indices[0]]),
    }


# ═══════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("doc-pipeline 性能基准测试")
    print(f"模式: {'快速' if QUICK else '完整'} | 迭代: {ITERATIONS}x")
    print("=" * 70)

    benchmarks = [
        ("HTML 正文提取 (selectolax vs regex)", bench_html_extraction),
        ("CacheManager 吞吐", bench_cache_throughput),
        ("并行执行 (Thread vs Process)", bench_parallel_execution),
        ("SSE 流式 chunk 传播", bench_streaming_overhead),
        ("TF-IDF 语义匹配", bench_tfidf),
    ]

    all_results = {}
    for name, fn in benchmarks:
        print(f"\n{'─' * 50}")
        print(f"  {name}")
        print(f"{'─' * 50}")
        try:
            result = fn()
            all_results[name] = result
            for k, v in result.items():
                if v is None:
                    print(f"    {k:.<30} N/A")
                elif isinstance(v, float):
                    if v > 100:
                        print(f"    {k:.<30} {v:,.1f}")
                    elif v < 0.01:
                        print(f"    {k:.<30} {v*1000:.3f} us")
                    else:
                        print(f"    {k:.<30} {v:.3f}")
                else:
                    print(f"    {k:.<30} {v}")
        except Exception as e:
            print(f"    ERROR: {e}")
            all_results[name] = {"error": str(e)}

    # 汇总
    print(f"\n{'=' * 70}")
    print("汇总")
    print(f"{'=' * 70}")

    # selectolax 加速比
    html_r = all_results.get("HTML 正文提取 (selectolax vs regex)", {})
    if html_r.get("speedup"):
        print(f"  selectolax 加速比: {html_r['speedup']:.1f}x")

    # 并行加速比
    par_r = all_results.get("并行执行 (Thread vs Process)", {})
    if par_r.get("thread_speedup"):
        print(f"  ThreadPool 加速比: {par_r['thread_speedup']:.2f}x")
    if par_r.get("process_speedup"):
        print(f"  ProcessPool 加速比: {par_r['process_speedup']:.2f}x")

    # 缓存吞吐
    cache_r = all_results.get("CacheManager 吞吐", {})
    if cache_r.get("set_ops_per_sec"):
        print(f"  Cache SET 吞吐: {cache_r['set_ops_per_sec']:,.0f} ops/s")
        print(f"  Cache GET(hit) 吞吐: {cache_r['get_hit_ops_per_sec']:,.0f} ops/s")

    # 流式开销
    stream_r = all_results.get("SSE 流式 chunk 传播", {})
    if stream_r.get("emit_ms_per_op"):
        print(f"  SSE chunk emit: {stream_r['emit_ms_per_op']:.4f} ms/op")

    print()

    # 导出 JSON
    output_path = Path(__file__).parent / "benchmark_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, default=str, ensure_ascii=False)
    print(f"结果已导出: {output_path}")


if __name__ == "__main__":
    main()
