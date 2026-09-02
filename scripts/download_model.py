import os
import sys

# Tell fastembed to download the model weights to this specific folder
os.environ["FASTEMBED_CACHE_PATH"] = "./offline_model_cache"

try:
    from fastembed import TextEmbedding
    print("Downloading fastembed default model (bge-small-en-v1.5) to ./offline_model_cache...")
    # Initializing it forces the download
    model = TextEmbedding()
    print("Download complete!")
except ImportError:
    print("fastembed not installed locally.")
    sys.exit(1)
