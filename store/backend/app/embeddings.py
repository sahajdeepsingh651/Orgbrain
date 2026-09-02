from fastembed import TextEmbedding

_embedding_model: TextEmbedding | None = None


def get_embedding_model() -> TextEmbedding:
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = TextEmbedding()
    return _embedding_model
