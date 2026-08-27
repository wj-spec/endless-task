"""R5.8 嵌入层：Embedder 协议、本地 ONNX 小模型与 provider embedding 两种后端。

红线（演进文档 §6）：默认配置下不出机。嵌入整体默认关闭；
本地后端在设备内推理，模型文件一次性下载后本地缓存；
provider 后端是显式可选项，由用户在配置中声明其网关支持 /embeddings。
"""

from __future__ import annotations

import logging
import math
import os
import time
from pathlib import Path
from typing import List, Optional, Protocol, Sequence

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_REPO = "Xenova/bge-small-zh-v1.5"
DEFAULT_LOCAL_MODEL_NAME = "bge-small-zh-v1.5"
DEFAULT_URL_BASE = "https://huggingface.co"
_LOCAL_ONNX_PATH = "onnx/model.onnx"
_LOCAL_ONNX_FALLBACK_PATH = "onnx/model_quantized.onnx"
_LOCAL_TOKENIZER_PATH = "tokenizer.json"
_LOCAL_VOCAB_PATH = "vocab.txt"
_MAX_SEQUENCE_TOKENS = 512


class EmbeddingError(RuntimeError):
    """嵌入后端错误（调用方需静默降级，不得阻断主链路）。"""


class Embedder(Protocol):
    model_name: str

    def ensure_ready(self) -> None:
        """确保后端可用（本地后端可能触发一次性模型下载）。"""
        ...

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        ...


def cls_pool_and_normalize(
    hidden_states: Sequence[Sequence[Sequence[float]]],
) -> List[List[float]]:
    """CLS 池化 + L2 归一化（bge 系列的标准用法）。"""
    vectors: List[List[float]] = []
    for matrix in hidden_states:
        if not matrix:
            raise EmbeddingError("Empty hidden state.")
        vector = [float(value) for value in matrix[0]]
        norm = math.sqrt(math.fsum(value * value for value in vector))
        if norm == 0.0:
            norm = 1.0
        vectors.append([value / norm for value in vector])
    return vectors


class ProviderEmbedder:
    """OpenAI 兼容 /embeddings 后端（用户显式启用）。"""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: Optional[str] = None,
        timeout_seconds: float = 20.0,
        client: Optional[object] = None,
    ) -> None:
        self.model_name = model
        self._api_key = api_key
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._client = client

    def ensure_ready(self) -> None:
        return None

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        payload = [(text or " ").strip() or " " for text in texts]
        if not payload:
            return []
        try:
            client = self._get_client()
            response = client.embeddings.create(
                model=self.model_name, input=payload
            )
        except EmbeddingError:
            raise
        except Exception as error:  # noqa: BLE001 网络/协议错误统一降级
            raise EmbeddingError(f"Provider embedding failed: {error}") from error
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors = [[float(value) for value in item.embedding] for item in ordered]
        if len(vectors) != len(payload):
            raise EmbeddingError("Provider returned a vector count mismatch.")
        return vectors

    def _get_client(self) -> object:
        if self._client is None:
            from openai import OpenAI

            options = {
                "api_key": self._api_key,
                "timeout": self._timeout_seconds,
                "max_retries": 1,
            }
            if self._base_url:
                options["base_url"] = self._base_url
            self._client = OpenAI(**options)
        return self._client


class LocalOnnxEmbedder:
    """本地 ONNX 小模型后端（bge-small-zh-v1.5，CPU 推理，模型本地缓存）。"""

    def __init__(
        self,
        cache_dir: Path,
        *,
        repo_id: str = DEFAULT_LOCAL_REPO,
        url_base: str = DEFAULT_URL_BASE,
        max_tokens: int = _MAX_SEQUENCE_TOKENS,
        session_factory=None,
        tokenizer_factory=None,
        download_attempts: int = 6,
    ) -> None:
        self.model_name = DEFAULT_LOCAL_MODEL_NAME
        self._model_dir = Path(cache_dir) / "models" / self.model_name
        self._repo_id = repo_id
        self._url_base = url_base.rstrip("/")
        self._max_tokens = max_tokens
        self._session_factory = session_factory
        self._tokenizer_factory = tokenizer_factory
        self._download_attempts = max(1, int(download_attempts))
        self._session = None
        self._tokenizer = None
        self._input_names: tuple = ()
        self._output_name: Optional[str] = None

    def ensure_ready(self) -> None:
        if self._session is not None and self._tokenizer is not None:
            return
        self._load_tokenizer()
        self._load_session()

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        self.ensure_ready()
        payload = [(text or " ").strip() or " " for text in texts]
        encodings = [self._tokenizer.encode(text) for text in payload]
        truncated = [
            encoding.ids[: self._max_tokens] for encoding in encodings
        ]
        width = max(len(ids) for ids in truncated)
        input_ids: List[List[int]] = []
        attention_mask: List[List[int]] = []
        token_type_ids: List[List[int]] = []
        for ids in truncated:
            padding = width - len(ids)
            input_ids.append(ids + [0] * padding)
            attention_mask.append([1] * len(ids) + [0] * padding)
            token_type_ids.append([0] * width)
        try:
            import numpy

            feed = {
                "input_ids": numpy.asarray(input_ids, dtype=numpy.int64),
                "attention_mask": numpy.asarray(attention_mask, dtype=numpy.int64),
                "token_type_ids": numpy.asarray(token_type_ids, dtype=numpy.int64),
            }
            available = {name: feed[name] for name in self._input_names if name in feed}
            outputs = self._session.run([self._output_name], available)
        except EmbeddingError:
            raise
        except Exception as error:  # noqa: BLE001 推理错误统一降级
            raise EmbeddingError(f"Local embedding inference failed: {error}") from error
        hidden = outputs[0].tolist()
        return cls_pool_and_normalize(hidden)

    # ---------- 加载 ----------

    def _load_tokenizer(self) -> None:
        if self._tokenizer_factory is not None:
            self._tokenizer = self._tokenizer_factory()
            return
        try:
            from tokenizers import BertWordPieceTokenizer, Tokenizer
        except ImportError as error:  # noqa: BLE001
            raise EmbeddingError(
                "tokenizers is not installed; local embeddings unavailable."
            ) from error
        tokenizer_path = self._model_dir / _LOCAL_TOKENIZER_PATH
        vocab_path = self._model_dir / _LOCAL_VOCAB_PATH
        if not tokenizer_path.exists() and not vocab_path.exists():
            self._download(_LOCAL_TOKENIZER_PATH, tokenizer_path, required=False)
            if not tokenizer_path.exists():
                self._download(_LOCAL_VOCAB_PATH, vocab_path)
        if tokenizer_path.exists():
            self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        else:
            self._tokenizer = BertWordPieceTokenizer(
                str(vocab_path), lowercase=True
            )

    def _load_session(self) -> None:
        if self._session_factory is not None:
            self._session, self._input_names, self._output_name = (
                self._session_factory()
            )
            return
        try:
            import onnxruntime
        except ImportError as error:  # noqa: BLE001
            raise EmbeddingError(
                "onnxruntime is not installed; local embeddings unavailable."
            ) from error
        onnx_path = self._model_dir / _LOCAL_ONNX_PATH
        if not onnx_path.exists():
            self._download(_LOCAL_ONNX_PATH, onnx_path, required=False)
        if not onnx_path.exists():
            onnx_path = self._model_dir / _LOCAL_ONNX_FALLBACK_PATH
            self._download(_LOCAL_ONNX_FALLBACK_PATH, onnx_path)
        try:
            session = onnxruntime.InferenceSession(
                str(onnx_path), providers=["CPUExecutionProvider"]
            )
        except Exception as error:  # noqa: BLE001
            raise EmbeddingError(f"Failed to load ONNX model: {error}") from error
        self._session = session
        self._input_names = tuple(item.name for item in session.get_inputs())
        outputs = session.get_outputs()
        if not outputs:
            raise EmbeddingError("ONNX model exposes no outputs.")
        self._output_name = outputs[0].name

    def _download(
        self,
        remote_path: str,
        target: Path,
        *,
        required: bool = True,
    ) -> None:
        """下载模型文件：断点续传（Range）+ 指数退避重试。"""
        if target.exists():
            return
        try:
            import httpx
        except ImportError as error:  # noqa: BLE001
            raise EmbeddingError("httpx is unavailable for model download.") from error
        url = f"{self._url_base}/{self._repo_id}/resolve/main/{remote_path}"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".part")
        last_error: Optional[Exception] = None
        max_attempts = self._download_attempts
        for attempt in range(1, max_attempts + 1):
            resume_position = temporary.stat().st_size if temporary.exists() else 0
            headers = {"Range": f"bytes={resume_position}-"} if resume_position else {}
            try:
                with httpx.stream(
                    "GET",
                    url,
                    headers=headers,
                    follow_redirects=True,
                    timeout=120.0,
                ) as response:
                    if response.status_code == 404 and not required:
                        logger.info("Optional model file missing upstream: %s", url)
                        temporary.unlink(missing_ok=True)
                        return
                    if response.status_code == 416:  # 已下载完整
                        os.replace(temporary, target)
                        return
                    response.raise_for_status()
                    resumed = (
                        resume_position > 0 and response.status_code == 206
                    )
                    if not resumed:
                        temporary.unlink(missing_ok=True)
                    logger.info(
                        "Downloading embedding model file (%s attempt %d/%d): %s",
                        "resume" if resumed else "full",
                        attempt,
                        max_attempts,
                        url,
                    )
                    with open(temporary, "ab" if resumed else "wb") as handle:
                        for chunk in response.iter_bytes(chunk_size=1 << 16):
                            handle.write(chunk)
                os.replace(temporary, target)
                return
            except Exception as error:  # noqa: BLE001 网络错误重试
                last_error = error
                logger.warning(
                    "Model download interrupted (attempt %d/%d): %s",
                    attempt,
                    max_attempts,
                    error,
                )
                if attempt < max_attempts:
                    time.sleep(min(2.0 ** attempt, 10.0))
        raise EmbeddingError(f"Model download failed: {last_error}")


def build_embedder(
    *,
    backend: str,
    embedding_model: Optional[str],
    provider_name: str,
    api_key: Optional[str],
    base_url: Optional[str],
    cache_dir: Path,
    local_repo: str = DEFAULT_LOCAL_REPO,
    local_url_base: str = DEFAULT_URL_BASE,
) -> Optional[Embedder]:
    """按配置构造嵌入后端；配置不完整时返回 None（调用方退回纯字面检索）。"""
    normalized = (backend or "").strip().lower()
    if normalized == "provider":
        if not embedding_model or not api_key:
            logger.warning(
                "Embedding backend 'provider' requires ENDLESS_TASK_EMBEDDING_MODEL "
                "and a provider API key; falling back to literal search."
            )
            return None
        if provider_name == "openai-compatible" and not base_url:
            logger.warning(
                "Provider embeddings require ENDLESS_TASK_BASE_URL; "
                "falling back to literal search."
            )
            return None
        return ProviderEmbedder(
            model=embedding_model, api_key=api_key, base_url=base_url
        )
    if normalized == "local":
        return LocalOnnxEmbedder(
            cache_dir, repo_id=local_repo, url_base=local_url_base
        )
    logger.warning("Unknown embedding backend %r; literal search only.", backend)
    return None
