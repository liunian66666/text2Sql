"""OpenAI-compatible embedding with bounded provider batches and usage logging."""
from app.llm.usage import ThreadPoolExecutor
import time
import numpy as np
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
from app.llm.usage import record_usage


class MeteredOpenAIEmbeddingFunction(OpenAIEmbeddingFunction):
    def __call__(self, input):
        if not input:
            return []
        # DashScope embedding endpoints accept at most 10 texts per request.
        # Smaller batches also work for other OpenAI-compatible providers.
        chunks = [list(input[i:i + 10]) for i in range(0, len(input), 10)]

        def embed(chunk):
            started = time.perf_counter()
            response = self.client.embeddings.create(
                model=self.model_name, input=chunk, encoding_format="float", timeout=60)
            record_usage(response, model=self.model_name,
                         elapsed_seconds=time.perf_counter() - started, kind="embedding")
            return [np.asarray(row.embedding, dtype=np.float32)
                    for row in sorted(response.data, key=lambda row: row.index)]

        with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as executor:
            return [vector for batch in executor.map(embed, chunks) for vector in batch]
